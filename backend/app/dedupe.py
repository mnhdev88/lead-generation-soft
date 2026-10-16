"""Spot the same business found twice and merge the two into one company.

OpenStreetMap and Google often list the same dentist; OpenStreetMap sometimes has it twice
(a point and a building outline). Each copy holds half the facts, so we merge them.

Two companies are "the same" when (see same_business):
  1. same phone number and similar name, within 1 km
  2. same website domain (the company's own, not facebook.com) and within 300 m,
     or in the same city when one of them has no map position
     (chains like mcdonalds.com share a domain but have many locations: distance matters)
  3. (almost) the same name within 150 m, unless they have different phone numbers
     ("Home Slice Pizza" and "More Home Slice Pizza" next door are two shops)
Google keeps its own duplicates apart, so two Google places only merge when within 150 m.

The merge keeps the older company (its id, lists, notes and status stay valid) and moves
everything from the newer one into it: people, emails, list entries, notes, search results.
Facts from Google win over OpenStreetMap; empty fields are filled from the other copy.
The other copy's (source, source_ref) is saved in company_sources, so the next search of
either source updates the merged company instead of creating the copy again.

Merge the duplicates already in the database:
    python -m app.dedupe            show what would be merged
    python -m app.dedupe --apply    merge them
"""
import math
import re
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .models import Activity, Company, CompanySource, Contact, JobResult, ListItem
from .phones import apply_company_phone, is_usable
from .scoring import rescore
from .utils import merge_emails, own_domain

# Which source's facts win when both copies have a value (higher wins).
SOURCE_PRIORITY = {"google": 2, "osm": 1}

PHONE_MATCH_MAX_M = 1000
DOMAIN_MATCH_MAX_M = 300
NAME_MATCH_MAX_M = 150
SAME_SOURCE_MAX_M = 150  # two records of one source (except OpenStreetMap) must be this close
PHONE_NAME_MIN = 0.5
DOMAIN_NAME_MIN = 0.6  # domain match without map positions: names must be this similar
NAME_ONLY_MIN = 0.92  # names alone: only spelling differences ("Smile Dental" / "Smile-Dental")
NEAR_DEG = 0.003  # ~330 m: the box searched for nearby companies

# Words that don't help tell businesses apart.
NAME_NOISE = {
    "the", "and", "llc", "inc", "ltd", "limited", "pvt", "private", "co", "company", "corp",
    "corporation", "pllc", "pc", "plc", "llp", "dba", "of",
}

# Facts that come from the business listing (the higher-priority source wins these).
LISTING_FIELDS = (
    "name", "category", "address", "city", "country", "lat", "lon", "rating", "reviews_count",
    "opening_hours", "google_maps_url", "business_status",
)
CONTACT_FIELDS = (
    "first_name", "last_name", "title", "seniority", "department", "email", "email_status", "email_source",
    "email_confidence", "email_checked_at", "phone", "phone_type", "linkedin_url", "source_url",
)


# ----- telling whether two companies are the same ---------------------------------------


def _name_words(name: str | None) -> list[str]:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    return [w for w in re.split(r"[^a-z0-9]+", text) if w and w not in NAME_NOISE]


def name_similarity(a: str | None, b: str | None) -> float:
    """0..1. 'Smile Dental Clinic' vs 'Smile Dental Clinic, LLC' = 1.0"""
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return 0.0
    sa, sb = set(wa), set(wb)
    common = len(sa & sb)
    ratio = SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio()
    jaccard = common / len(sa | sb)
    # "Smile Dental" inside "Smile Dental - Dr. Khan": the shorter name is fully contained.
    # A single shared word ("Dental") is not enough.
    shorter = min(len(sa), len(sb))
    contained = 0.9 if shorter >= 2 and common == shorter else 0.0
    return max(ratio, jaccard, contained)


def same_name(a: str | None, b: str | None) -> bool:
    """Strict: the same words, or only small spelling differences."""
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return False
    return wa == wb or SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio() >= NAME_ONLY_MIN


def distance_m(a: Company, b: Company) -> float | None:
    """Metres between two companies, None when one has no map position."""
    if None in (a.lat, a.lon, b.lat, b.lon):
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(h))


def _within(d: float | None, metres: float) -> bool:
    return d is not None and d <= metres


def same_business(a: Company, b: Company) -> str | None:
    """Why a and b are the same business ("same phone and name"), or None if they aren't."""
    if a.id is not None and a.id == b.id:
        return None
    d = distance_m(a, b)
    if a.source == b.source and a.source != "osm" and not _within(d, SAME_SOURCE_MAX_M):
        return None
    sim = name_similarity(a.name, b.name)

    if a.phone_e164 and a.phone_e164 == b.phone_e164 and sim >= PHONE_NAME_MIN:
        if d is None or d <= PHONE_MATCH_MAX_M:
            return "same phone and name"
    domain = own_domain(a.domain)
    if domain and domain == own_domain(b.domain):
        if _within(d, DOMAIN_MATCH_MAX_M):
            return "same website, same place"
        same_city = a.city and b.city and a.city.strip().lower() == b.city.strip().lower()
        if d is None and (same_city or sim >= DOMAIN_NAME_MIN):
            return "same website and " + ("city" if same_city else "name")
    different_phones = a.phone_e164 and b.phone_e164 and a.phone_e164 != b.phone_e164
    if not different_phones and same_name(a.name, b.name) and _within(d, NAME_MATCH_MAX_M):
        return "same name, same place"
    return None


def find_duplicate(session: Session, company: Company) -> tuple[Company, str] | None:
    """Another saved company that is the same business, with the reason. Oldest first."""
    if company.id is None:
        session.flush()
    conditions = []
    if company.phone_e164:
        conditions.append(Company.phone_e164 == company.phone_e164)
    domain = own_domain(company.domain)
    if domain:
        conditions.append(Company.domain == domain)
    if company.lat is not None and company.lon is not None:
        lon_deg = NEAR_DEG / max(0.2, math.cos(math.radians(company.lat)))
        conditions.append(and_(
            Company.lat.between(company.lat - NEAR_DEG, company.lat + NEAR_DEG),
            Company.lon.between(company.lon - lon_deg, company.lon + lon_deg),
        ))
    if not conditions:
        return None
    candidates = session.scalars(
        select(Company).where(Company.id != company.id, or_(*conditions)).order_by(Company.id)
    )
    for other in candidates:
        reason = same_business(company, other)
        if reason:
            return other, reason
    return None


# ----- merging ------------------------------------------------------------------------


def _priority(source: str | None) -> int:
    return SOURCE_PRIORITY.get(source or "", 0)


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _move_list_items(session: Session, column, old_id: int, new_id: int) -> None:
    """Point list entries of old_id at new_id; drop those that would be doubled."""
    other = ListItem.contact_id if column is ListItem.company_id else ListItem.company_id
    for item in list(session.scalars(select(ListItem).where(column == old_id))):
        twin = session.scalar(select(ListItem.id).where(
            ListItem.list_id == item.list_id, column == new_id,
            other.is_(None) if getattr(item, other.key) is None else other == getattr(item, other.key),
        ))
        if twin:
            session.delete(item)
        else:
            setattr(item, column.key, new_id)


def _find_twin(contacts: list[Contact], c: Contact) -> Contact | None:
    """The same person among `contacts`: same email or same full name."""
    email, name = (c.email or "").lower(), (c.full_name or "").strip().lower()
    for other in contacts:
        if (email and (other.email or "").lower() == email) or (name and (other.full_name or "").strip().lower() == name):
            return other
    return None


def _merge_contacts(session: Session, keep: Company, drop: Company) -> int:
    """Move drop's people to keep; a person both have becomes one. Returns people moved."""
    moved = 0
    for c in list(drop.contacts):
        twin = _find_twin(keep.contacts, c)
        if twin is None:
            c.company = keep  # back_populates moves it between the two lists
            moved += 1
            continue
        for field in CONTACT_FIELDS:
            if _empty(getattr(twin, field)) and not _empty(getattr(c, field)):
                setattr(twin, field, getattr(c, field))
        if twin.lead_status in (None, "new") and c.lead_status not in (None, "new"):
            twin.lead_status = c.lead_status
        twin.is_decision_maker = bool(twin.is_decision_maker or c.is_decision_maker)
        session.flush()
        _move_list_items(session, ListItem.contact_id, c.id, twin.id)
        for a in session.scalars(select(Activity).where(Activity.contact_id == c.id)):
            a.contact_id = twin.id
        drop.contacts.remove(c)  # delete-orphan: the row is deleted on flush
    return moved


def merge_companies(session: Session, keep: Company, drop: Company, reason: str = "duplicate") -> Company:
    """Merge `drop` into `keep` and delete `drop`. Returns keep (the caller commits)."""
    session.flush()
    winner, loser = (drop, keep) if _priority(drop.source) > _priority(keep.source) else (keep, drop)

    # Listing facts: the better source's value, else the other one's.
    for field in LISTING_FIELDS:
        value = getattr(winner, field)
        setattr(keep, field, getattr(loser, field) if _empty(value) else value)
    if winner is drop:
        keep.raw = drop.raw
    # Website: the company's own domain beats a Facebook page.
    sites = sorted((winner, loser), key=lambda c: (not c.website, not own_domain(c.domain)))
    keep.website, keep.domain = sites[0].website, sites[0].domain
    # Phone: a usable one, the better source's first.
    phones = sorted((winner, loser), key=lambda c: not is_usable(c.phone, c.phone_type))
    keep.phone = phones[0].phone
    keep.socials = {**(loser.socials or {}), **(winner.socials or {})}

    # Emails: keep's first; per-email facts from both.
    keep.emails = merge_emails([keep.email] if keep.email else [], keep.emails or [],
                               [drop.email] if drop.email else [], drop.emails or [])
    keep.email_statuses = {**(drop.email_statuses or {}), **(keep.email_statuses or {})}
    keep.email_sources = {**(drop.email_sources or {}), **(keep.email_sources or {})}
    if not keep.email or keep.email_statuses.get(keep.email.lower()) == "invalid":
        keep.email = next((e for e in keep.emails if keep.email_statuses.get(e) != "invalid"), keep.email)
    checked = [t for t in (keep.emails_checked_at, drop.emails_checked_at) if t]
    keep.emails_checked_at = max(checked) if checked else None

    # Website crawl: a successful one beats a failed one; else the more recent one.
    def crawl_rank(c: Company):
        # (SQLite gives dates back without a time zone, new ones have one: compare without.)
        when = c.enriched_at.replace(tzinfo=None) if c.enriched_at else datetime.min
        return (c.enrich_status == "done", c.enrich_status is not None, when)
    if crawl_rank(drop) > crawl_rank(keep) and drop.website == keep.website:
        keep.website_signals, keep.enrich_status = drop.website_signals, drop.enrich_status
        keep.enrich_error, keep.enriched_at = drop.enrich_error, drop.enriched_at

    # Your work on the lead: a status beyond "new" wins.
    if (keep.lead_status or "new") == "new" and (drop.lead_status or "new") != "new":
        keep.lead_status = drop.lead_status

    # US PPP loan (app/ppp.py): a loan found for either copy is kept. When only the listing
    # facts changed (the better source won), keep is looked up again with its new name and
    # address. (rescore below then uses the loan's headcount for the size.)
    if not (keep.ppp or {}).get("jobs") and (drop.ppp or {}).get("jobs"):
        keep.ppp, keep.ppp_checked_at = dict(drop.ppp), drop.ppp_checked_at
    elif winner is drop and not (keep.ppp or {}).get("jobs"):
        keep.ppp_checked_at = None  # the worker and the idle back-fill look it up again

    # Everything that points at drop now points at keep.
    _merge_contacts(session, keep, drop)
    for a in list(drop.activities):
        a.company = keep
    _move_list_items(session, ListItem.company_id, drop.id, keep.id)
    job_ids = []
    for result in list(session.scalars(select(JobResult).where(JobResult.company_id == drop.id))):
        job_ids.append(result.job_id)
        session.delete(result)

    # Source records: drop's own and its aliases become keep's aliases.
    drop_ref = (drop.source, drop.source_ref)
    refs = [drop_ref]
    for alias in list(session.scalars(select(CompanySource).where(CompanySource.company_id == drop.id))):
        refs.append((alias.source, alias.source_ref))
        session.delete(alias)
    note = (
        f"Merged a duplicate ({reason}): \"{drop.name}\" from {drop.source}"
        + (f", phone {drop.phone}" if drop.phone else "")
        + (f", {drop.address}" if drop.address else "")
    )
    keep.activities.append(Activity(kind="merge", text=note[:1000]))
    session.delete(drop)
    session.flush()
    for job_id in job_ids:
        session.merge(JobResult(job_id=job_id, company_id=keep.id))

    # The better source becomes the main one, so its next search updates every fact.
    if winner is drop:
        refs.remove(drop_ref)
        refs.append((keep.source, keep.source_ref))
        keep.source, keep.source_ref = drop_ref
    for source, ref in refs:
        session.add(CompanySource(company_id=keep.id, source=source, source_ref=ref))

    apply_company_phone(keep)
    rescore(keep)
    session.flush()
    return keep


def merge_if_duplicate(session: Session, company: Company) -> tuple[Company, int | None]:
    """After saving a company: merge it with a saved copy of the same business, if any.
    Returns (the company that remains - the older one, id of the removed copy or None)."""
    found = find_duplicate(session, company)
    if not found:
        return company, None
    other, reason = found
    keep, drop = (other, company) if other.id < company.id else (company, other)
    drop_id = drop.id
    return merge_companies(session, keep, drop, reason), drop_id


# ----- cleaning up the whole database -----------------------------------------------------


def find_all_duplicates(session: Session) -> list[list[tuple[Company, str]]]:
    """Groups of the same business, oldest first: [[(company, ""), (copy, reason)], ...]."""
    parent: dict[int, int] = {}  # union-find: company id -> an id of the same group

    def root(cid: int) -> int:
        while parent.get(cid, cid) != cid:
            cid = parent[cid]
        return cid

    companies: dict[int, Company] = {}
    reasons: dict[int, str] = {}
    for company in session.scalars(select(Company).order_by(Company.id)).all():
        companies[company.id] = company
        found = find_duplicate(session, company)
        if not found:
            continue
        other, reason = found
        companies[other.id] = other
        newer = max(company.id, other.id)
        reasons.setdefault(newer, reason)
        a, b = root(company.id), root(other.id)
        if a != b:
            parent[max(a, b)] = min(a, b)

    groups: dict[int, list[int]] = {}
    for cid in sorted(set(parent) | set(parent.values())):
        groups.setdefault(root(cid), []).append(cid)
    return [
        [(companies[cid], reasons.get(cid, "") if i else "") for i, cid in enumerate(ids)]
        for ids in groups.values() if len(ids) > 1
    ]


def merge_all_duplicates(session: Session) -> int:
    """Merge every group found by find_all_duplicates. Returns how many copies were removed."""
    removed = 0
    for group in find_all_duplicates(session):
        keep = group[0][0]
        for copy, reason in group[1:]:
            keep = merge_companies(session, keep, copy, reason)
            removed += 1
        session.commit()
    return removed


def main(argv: list[str]) -> None:
    from .db import SessionLocal, init_db
    from .phones import normalize_saved_phones

    apply = "--apply" in argv
    init_db()
    with SessionLocal() as session:
        normalize_saved_phones(session)  # phone matching needs the standard phone format
        groups = find_all_duplicates(session)
        if not groups:
            print("No duplicates found.")
            return
        for group in groups:
            first = group[0][0]
            print(f"\n#{first.id} {first.name} ({first.source}) {first.phone or ''}")
            for copy, reason in group[1:]:
                print(f"   = #{copy.id} {copy.name} ({copy.source}) {copy.phone or ''}   [{reason}]")
        copies = sum(len(g) - 1 for g in groups)
        if not apply:
            print(f"\n{copies} duplicate(s) in {len(groups)} group(s). Nothing changed yet.")
            print("Run again with --apply to merge them (back up backend/data/leads.db first).")
            return
        removed = merge_all_duplicates(session)
        print(f"\nMerged {removed} duplicate(s).")


if __name__ == "__main__":
    main(sys.argv[1:])
