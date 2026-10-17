"""Background worker: picks up queued jobs and runs them one at a time.

Three kinds of job:
  - "search": ask a source (OpenStreetMap, Google...) for businesses and save them.
  - "enrich": crawl the websites of companies to find emails, phones, socials and people.
  - "emails": find + verify emails of companies and their people (Step 4), optionally
              crawling the websites first ("Find decision-makers").

Chain: search (enrich=True) -> enrich job -> (params.find_emails) -> emails job.

It runs as a thread inside the API process to keep things simple. When the app
becomes a SaaS with many users, this moves to a separate process + Redis queue.
"""
import asyncio
import logging
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from . import config, ppp
from .config import CRAWL_CONCURRENCY
from .db import SessionLocal
from .emails import find_people_emails, learn_pattern, normalize_email, verify_emails
from .emails.patterns import is_valid_pattern
from .enrich import crawl_website, make_client
from .dedupe import merge_if_duplicate
from .intent import apply_intent
from .models import Activity, Company, CompanySource, Contact, DomainInfo, JobResult, ListItem, SearchJob
from .people import classify
from .phones import apply_company_phone, apply_contact_phone, best_phone, normalize_saved_phones
from .providers import hunter
from .scoring import recompute_scores, rescore
from .sizing import branch_counts, refresh_branch_sizes, size_saved_companies
from .sources import SOURCES, CompanyResult, SourceError
from .usage import UsageLimitError, get_usage
from .utils import extract_domain, normalize_url, own_domain
from .utils import merge_emails as _merge_emails

log = logging.getLogger("worker")
POLL_SECONDS = 2
MESSAGE_MAX = 300  # progress_message column size

# Words dropped from the front of a name before splitting it into first/last name.
NAME_PREFIXES = {"dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "miss", "prof", "prof.", "eng", "eng.", "engr", "engr."}

# Source progress messages that are warnings the user must still see after the job ends
# (e.g. Google: "Monthly Google Places limit reached ... Keeping the 40 businesses found so far.").
WARNING_MARKERS = ("Keeping the", "Could not map")

# Emails jobs: companies checked at the same time (mail-server checks are slow, mostly waiting).
EMAIL_JOB_THREADS = 4

# Order of emails in Company.emails after checking: deliverable first, invalid last.
EMAIL_STATUS_RANK = {"valid": 0, "catch_all": 1, "unknown": 2, "risky": 2, "invalid": 3}
WEBSITE_EMAIL_CONFIDENCE = 70  # a person's email listed on their own company website
HUNTER_PATTERN_CONFIDENCE = 75  # Hunter's own guess of a domain's email pattern

# While no job is waiting, older companies get a size estimate and a PPP lookup, this many
# at a time (a job the user starts then waits a few seconds at most).
BACKFILL_LIMIT = 200
BACKFILL_RECHECK_SECONDS = 300  # nothing left to fill in: look again after this long
# Google PageSpeed back-fill (app/pagespeed.py): runs in its own thread, so a search the
# user starts never waits for a test (one can take a minute). Websites per round, the pause
# between rounds while there is work, and the pause when there is none (or Google said stop).
PAGESPEED_BACKFILL_LIMIT = 1
PAGESPEED_PAUSE_SECONDS = 5
PAGESPEED_IDLE_SECONDS = BACKFILL_RECHECK_SECONDS
# The typical review count of a search (Company.peer_reviews_median) is worked out per kind of
# business (Google's primaryType, else the category) and needs this many results of that kind
# with a review count, else it says nothing (a solo doctor is not compared with hospitals).
PEER_MEDIAN_MIN_RESULTS = 5
# Google search position (Company.search_rank) is only saved from searches asking for at most
# this many results, and never from a deep (map tiles) search: in a big city-wide list,
# position 35 says little about a business's SEO, and positions across tiles are meaningless.
RANK_MAX_RESULTS = 20

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short(text: str) -> str:
    return text if len(text) <= MESSAGE_MAX else text[: MESSAGE_MAX - 1] + "…"


def _company_domain(company: Company) -> str | None:
    """The company's own email domain, or None for e.g. a Facebook page as website."""
    return own_domain(company.domain)


def _classify_contact(contact: Contact) -> None:
    result = classify(contact.title)
    contact.seniority = result.seniority
    contact.department = result.department
    contact.is_decision_maker = bool(result.is_decision_maker)


# ----- search jobs ---------------------------------------------------------------


def _source_emails(raw: str | None) -> list[str]:
    """'Info@A.com; sales@a.com' -> ['info@a.com', 'sales@a.com'] (OSM allows several)."""
    found = []
    for part in re.split(r"[;,\s]+", raw or ""):
        email = normalize_email(part) if part else None
        if email and email not in found:
            found.append(email)
    return found


def _upsert_company(session, source: str, r: CompanyResult) -> Company:
    company = session.scalar(
        select(Company).where(Company.source == source, Company.source_ref == r.source_ref)
    )
    if company is None:
        # Merged earlier into a company from another source (app/dedupe.py)?
        alias = session.scalar(
            select(CompanySource).where(CompanySource.source == source, CompanySource.source_ref == r.source_ref)
        )
        company = session.get(Company, alias.company_id) if alias else None
    is_new = company is None
    if is_new:
        company = Company(source=source, source_ref=r.source_ref)
        session.add(company)
    website = normalize_url(r.website)
    source_emails = _source_emails(r.email)
    old_website = company.website
    fields = {
        "name": r.name,
        "category": r.category,
        "phone": r.phone,
        "website": website,
        "domain": extract_domain(website),
        "address": r.address,
        "city": r.city,
        "country": r.country,
        "lat": r.lat,
        "lon": r.lon,
        "rating": r.rating,
        "reviews_count": r.reviews_count,
        "opening_hours": r.opening_hours,
        "google_maps_url": r.google_maps_url,
        "business_status": r.business_status,
        "socials": {**(company.socials or {}), **r.socials},
        "raw": r.raw,
    }
    if is_new:
        fields["email"] = source_emails[0] if source_emails else None
        fields["emails"] = source_emails
    else:
        # A new search must not wipe what the website crawler / email check found earlier
        # (the source usually has no email; the crawler may have filled email/phone).
        if not fields["phone"]:
            fields["phone"] = company.phone
        fields["emails"] = _merge_emails(company.emails or [], source_emails)
        if not company.email:
            statuses = company.email_statuses or {}
            usable = [e for e in source_emails if statuses.get(e) != "invalid"]
            fields["email"] = usable[0] if usable else None
    if not is_new and company.source != source:
        # The merged company's main source is a better one (Google over OpenStreetMap):
        # this one only fills the gaps.
        fields.pop("raw")
        fields = {k: v for k, v in fields.items() if k in ("emails", "socials") or not getattr(company, k)}
        fields["socials"] = {**r.socials, **(company.socials or {})}
    for name, value in fields.items():
        setattr(company, name, value)
    if not is_new and company.website != old_website:
        # The PageSpeed result was for the old website: the idle back-fill tests the new one.
        company.pagespeed, company.pagespeed_checked_at = {}, None
    _save_google_profile(company, r)
    apply_company_phone(company)
    _note_email_origin(company, source_emails, "source")
    session.flush()
    return company


def _save_google_profile(company: Company, r: CompanyResult) -> None:
    """Google's photo count and reviews, and what the reviews say (app/reviews.py). Only
    when the result has them: a source without them (OpenStreetMap) never wipes them."""
    photos = getattr(r, "photos_count", None)
    found_reviews = getattr(r, "reviews", None) or []
    if photos is not None:
        company.google_photos_count = photos
    if found_reviews:
        company.google_reviews = list(found_reviews)
        try:
            from . import reviews  # here: written alongside this feature

            company.review_signals = reviews.analyze_reviews(company.google_reviews, r.reviews_count)
        except Exception:
            log.exception("Could not read the reviews of %s", r.name)


def _peer_kind(r: CompanyResult) -> str | None:
    """Google's primaryType ("dentist"), else the category, to compare like with like."""
    raw = r.raw if isinstance(r.raw, dict) else {}
    kind = raw.get("primaryType") if isinstance(raw.get("primaryType"), str) else None
    kind = kind or r.category
    return kind.strip().lower() if isinstance(kind, str) and kind.strip() else None


def _peer_reviews_median(results: list[CompanyResult]) -> int | None:
    """Typical review count of a list of results (None with too few counts)."""
    counts = [r.reviews_count for r in results if r.reviews_count is not None]
    if len(counts) < PEER_MEDIAN_MIN_RESULTS:
        return None
    return int(statistics.median(counts))


def _peer_medians(results: list[CompanyResult]) -> dict[str, int]:
    """{kind of business: typical review count} for the kinds with enough results."""
    groups: dict[str, list[CompanyResult]] = {}
    for r in results:
        kind = _peer_kind(r)
        if kind:
            groups.setdefault(kind, []).append(r)
    medians = {kind: _peer_reviews_median(group) for kind, group in groups.items()}
    return {kind: m for kind, m in medians.items() if m is not None}


def _search_rank(r: CompanyResult, position: int) -> int | None:
    """Google's own position of the result in its query when the source gives it (r.rank),
    else its place in the list."""
    rank = getattr(r, "rank", None)
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        return position
    return rank


def _look_up_ppp(company: Company) -> None:
    """Find the company's US PPP loan (its reported headcount). Never stops the search."""
    try:
        ppp.apply_ppp(company)
    except Exception:
        log.exception("PPP lookup for company %s failed", company.id)


def run_search_job(session, job: SearchJob) -> None:
    warnings: list[str] = []

    def progress(msg: str) -> None:
        if any(marker in msg for marker in WARNING_MARKERS):
            warnings.append(msg)
        job.progress_message = _short(msg)
        session.commit()

    source = SOURCES.get(job.source)
    if source is None:
        raise SourceError(f"Unknown source '{job.source}'")
    reason = source.unavailable_reason()
    if reason:
        raise SourceError(reason)

    results = source.search(job.query, job.location, job.limit, progress)
    progress(f"Saving {len(results)} businesses...")
    # Load the companies we already have (with their people, which rescore() reads) in a
    # few queries, instead of one extra query per company.
    refs = [r.source_ref for r in results]
    known = []  # keeps them in the session while we work
    for i in range(0, len(refs), 500):
        known += session.scalars(
            select(Company).options(selectinload(Company.contacts))
            .where(Company.source == source.name, Company.source_ref.in_(refs[i:i + 500]))
        ).all()
    seen = set()
    domains = set()  # websites of the saved companies (to size their branches again below)
    merged = 0
    check_ppp = ppp.available()  # the US PPP loan data is downloaded (app/ppp.py)
    peers = _peer_medians(results)  # for "fewer reviews than competitors" (app/intent.py)
    # Positions only mean something in one small Google query (see RANK_MAX_RESULTS).
    save_rank = source.name == "google" and job.limit is not None and job.limit <= RANK_MAX_RESULTS
    for position, r in enumerate(results, start=1):
        company = _upsert_company(session, source.name, r)
        # The same business saved before (e.g. found on OpenStreetMap, now on Google)?
        company, removed_id = merge_if_duplicate(session, company)
        if check_ppp and company.ppp_checked_at is None:
            _look_up_ppp(company)
        if save_rank:  # Google ranks its results: 1 = shown first. The latest search wins.
            company.search_rank = _search_rank(r, position)
        peer = peers.get(_peer_kind(r) or "")
        if peer is not None:
            company.peer_reviews_median = peer
        rescore(company)
        if company.domain:
            domains.add(company.domain)
        if removed_id is not None:
            merged += 1
            seen.discard(removed_id)
        if company.id not in seen:
            seen.add(company.id)
            session.merge(JobResult(job_id=job.id, company_id=company.id))
    # Branches of one business were saved one by one (the first saw no others yet): size
    # them all again together, so each shows the same number of locations.
    if domains:
        refresh_branch_sizes(session, domains)
        for company_id in seen:  # their size may have changed, and the intent reads it
            company = session.get(Company, company_id)  # already loaded: no query
            if company is not None:
                apply_intent(company)
    job.result_count = job.total = job.processed = len(seen)
    job.status = "done"
    message = f"Found {len(seen)} businesses."
    if merged:
        message += f" Merged {merged} duplicate{'s' if merged != 1 else ''} with businesses already saved."

    # "Also find emails": queue a website crawl of these results.
    if job.enrich and seen:
        session.add(
            SearchJob(
                kind="enrich",
                query=job.query,
                location=job.location,
                source="crawler",
                limit=job.limit,
                parent_job_id=job.id,
                params={"find_emails": bool((job.params or {}).get("find_emails"))},
                progress_message="Waiting in queue...",
            )
        )
        message += " Website crawl queued."
    # Keep e.g. "Monthly Google Places limit reached..." visible (it explains a short result).
    for warning in warnings:
        message += " " + warning
    job.progress_message = _short(message)


# ----- enrich (website crawl) jobs -----------------------------------------------


def _note_email_origin(company: Company, emails: list[str], origin: str) -> None:
    """Remember where emails came from ("source", "website" or "hunter").
    The first origin recorded for an address wins."""
    sources = dict(company.email_sources or {})
    for e in emails or []:
        e = (e or "").strip().lower()
        if e:
            sources.setdefault(e, origin)
    company.email_sources = sources  # reassign: SQLAlchemy doesn't see in-place JSON changes


def _email_origin(company: Company, email: str) -> str:
    origin = (company.email_sources or {}).get(email)
    if origin:
        return origin
    # Rows saved before origins were recorded: an address the business listing itself
    # gives (OpenStreetMap email tags) is "source"; anything else came from the website.
    raw = company.raw or {}
    listed = _source_emails(" ".join(str(raw.get(k) or "") for k in ("email", "contact:email")))
    return "source" if email in listed else "website"


def _drop_stale_contacts(session, company: Company, still_there: list[Contact]) -> int:
    """After a good crawl, remove people an earlier crawl found that the website no longer
    shows (often junk from an older crawler version, e.g. "Oral Surgery" as a name).
    Only untouched ones: no email, status still "new", not in any list, no notes."""
    keep = {id(c) for c in still_there}
    stale = [
        c for c in company.contacts
        if id(c) not in keep and c.id is not None and c.source == "website"
        and not c.email and (c.lead_status or "new") == "new"
    ]
    if not stale:
        return 0
    ids = [c.id for c in stale]
    in_use = set(session.scalars(select(ListItem.contact_id).where(ListItem.contact_id.in_(ids))))
    in_use |= set(session.scalars(select(Activity.contact_id).where(Activity.contact_id.in_(ids))))
    removed = 0
    for c in stale:
        if c.id not in in_use:
            company.contacts.remove(c)  # delete-orphan: the row is deleted on flush
            removed += 1
    return removed


def _split_name(full_name: str) -> tuple[str | None, str | None]:
    """'Dr. Ayesha Khan Ali' -> ('Ayesha', 'Khan Ali')."""
    parts = full_name.split()
    while len(parts) > 1 and parts[0].lower() in NAME_PREFIXES:
        parts = parts[1:]
    if not parts:
        return None, None
    return parts[0], (" ".join(parts[1:]) or None)


def _find_contact(company: Company, name: str | None, email: str | None) -> Contact | None:
    """An existing contact with the same email or the same full name (case-insensitive)."""
    for c in company.contacts:
        if (email and (c.email or "").lower() == email) or (name and (c.full_name or "").lower() == name.lower()):
            return c
    return None


def _save_people(session, company: Company, people, matched: list[Contact] | None = None) -> int:
    """Add the people found on the website as contacts of `company`.
    A person already saved (same email, or same name) is updated instead of duplicated.
    Every contact saved or updated is appended to `matched` (if given).
    Returns how many people were saved or updated."""
    count = 0
    for p in people or []:
        name = " ".join((p.full_name or "").split())
        email = (p.email or "").strip().lower() or None
        if not name and not email:
            continue
        match = _find_contact(company, name, email)
        if match is None:
            match = Contact(full_name=name or None, source="website")
            match.first_name, match.last_name = _split_name(name) if name else (None, None)
            company.contacts.append(match)
        # Fill only the empty fields, so re-crawling never overwrites good data.
        for name_, value in (("title", p.title), ("phone", p.phone), ("linkedin_url", p.linkedin_url),
                             ("source_url", p.source_url)):
            if value and not getattr(match, name_):
                setattr(match, name_, value)
        if email and not match.email:
            match.email = email
            match.email_source = "website"
        apply_contact_phone(match, company)
        _classify_contact(match)
        if matched is not None:
            matched.append(match)
        count += 1
    return count


def _save_crawl(session, company: Company, data) -> int:
    """Store one website crawl result (a WebsiteData) on the company.
    Returns the number of people saved."""
    # Emails: what we already had first (the source's email is reliable), then the
    # crawler's list, which comes ranked best-first.
    existing = _merge_emails([company.email] if company.email else [], company.emails or [])
    crawled = _merge_emails(data.emails)
    if data.ok:
        # A good crawl replaces what earlier crawls found on the website: addresses that are
        # gone (or came from an older, buggier crawler) are dropped. Emails from the business
        # listing or Hunter.io stay.
        existing = [e for e in existing if e in crawled or _email_origin(company, e) != "website"]
    company.emails = _merge_emails(existing, crawled)
    _note_email_origin(company, crawled, "website")
    company.email_sources = {e: o for e, o in (company.email_sources or {}).items() if e in company.emails}
    company.email_statuses = {e: s for e, s in (company.email_statuses or {}).items() if e in company.emails}
    if company.email and company.email.strip().lower() not in company.emails:
        company.email = None
    if not company.email and company.emails:
        statuses = company.email_statuses or {}
        company.email = next((e for e in company.emails if statuses.get(e) != "invalid"), None)
    if data.phones and (not company.phone or company.phone_type == "invalid"):
        found = best_phone(data.phones, company.country, company.lat, company.lon)
        if found:
            company.phone = found
            apply_company_phone(company)
    # Socials: links we already had win over the crawled ones.
    company.socials = {**(data.socials or {}), **(company.socials or {})}
    # Website facts only from a good crawl: a timed-out or blocked site says nothing about
    # SSL/platform, and must not wipe what an earlier crawl found.
    if data.ok and data.signals:
        company.website_signals = dict(data.signals)
    company.enrich_status = "done" if data.ok else "failed"
    company.enrich_error = None if data.ok else (data.error or "Could not crawl the website")[:300]
    company.enriched_at = _now()
    matched: list[Contact] = []
    saved = _save_people(session, company, data.people, matched)
    if data.ok:
        _drop_stale_contacts(session, company, matched)
    rescore(company)
    return saved


def _job_targets(session, job: SearchJob) -> list[Company]:
    """The companies an enrich/emails job works on: params.company_ids, or the results
    of the parent search."""
    ids = (job.params or {}).get("company_ids")
    if ids:
        stmt = select(Company).where(Company.id.in_(ids))
    elif job.parent_job_id is not None:
        stmt = (
            select(Company)
            .join(JobResult, JobResult.company_id == Company.id)
            .where(JobResult.job_id == job.parent_job_id)
        )
    else:
        return []
    return list(session.scalars(stmt.order_by(Company.id)))


async def _crawl_all(session, companies: list[Company], stats: dict, on_progress) -> None:
    """Crawl the websites of `companies`, several at a time, saving each result as soon
    as it arrives. Database writes happen here in the loop, one after another (never
    from two tasks at once), with a commit per company so the UI sees live progress.
    on_progress(done, total) is called after each website (before the commit)."""
    semaphore = asyncio.Semaphore(max(1, CRAWL_CONCURRENCY))
    total = len(companies)

    # make_client() only connects to public IP addresses (a website's DNS can't send the
    # crawler to 127.0.0.1 or the office network).
    async with make_client() as client:

        async def crawl_one(company_id: int, url: str):
            async with semaphore:
                try:
                    return company_id, await crawl_website(url, client=client), None
                except Exception as e:  # crawl_website should not raise, but never let one site stop the job
                    log.exception("Crawling %s failed", url)
                    return company_id, None, f"{type(e).__name__}: {e}"

        tasks = [crawl_one(c.id, c.website) for c in companies]
        done = 0
        for next_result in asyncio.as_completed(tasks):
            company_id, data, crash = await next_result
            done += 1
            try:
                company = session.get(Company, company_id)
                if data is None:
                    company.enrich_status, company.enrich_error = "failed", crash[:300]
                    company.enriched_at = _now()
                    stats["failed"] += 1
                else:
                    stats["people"] += _save_crawl(session, company, data)
                    stats["emails"] += len(data.emails or [])
                    if data.emails:
                        stats["with_emails"] += 1
                    if not data.ok:
                        stats["failed"] += 1
                session.commit()
            except Exception as e:
                log.exception("Saving crawl result for company %s failed", company_id)
                session.rollback()
                stats["failed"] += 1
                company = session.get(Company, company_id)
                if company is not None:  # keep a note of the problem on the company
                    company.enrich_status = "failed"
                    company.enrich_error = f"Could not save the result: {type(e).__name__}"
                    company.enriched_at = _now()
            on_progress(done, total)
            session.commit()


def _new_crawl_stats() -> dict:
    return {"with_emails": 0, "emails": 0, "people": 0, "failed": 0}


def run_enrich_job(session, job: SearchJob) -> None:
    """Crawl company websites. job.result_count = number of companies whose website
    gave at least one email in this run."""
    params = job.params or {}
    force = bool(params.get("force"))
    candidates = _job_targets(session, job)
    # Already crawled ones are skipped, except crawls from before the size hints existed
    # (no "employees_stated" in the website facts): crawled again once to fill them in.
    targets = [c for c in candidates
               if force or c.enrich_status != "done" or "employees_stated" not in (c.website_signals or {})]
    skipped = len(candidates) - len(targets)

    no_website = [c for c in targets if not c.website]
    with_website = [c for c in targets if c.website]
    for c in no_website:
        c.enrich_status, c.enrich_error, c.enriched_at = "no_website", None, _now()
        rescore(c)
    job.total = len(targets)
    job.processed = len(no_website)
    job.result_count = 0
    job.progress_message = f"Crawling {len(with_website)} websites..."
    session.commit()

    stats = _new_crawl_stats()

    def on_progress(done: int, total: int) -> None:
        job.processed += 1
        job.result_count = stats["with_emails"]
        job.progress_message = f"Crawled {done}/{total} websites — {stats['emails']} emails found"

    if with_website:
        asyncio.run(_crawl_all(session, with_website, stats, on_progress))

    message = (
        f"Crawled {len(with_website)} websites: {stats['with_emails']} with emails, "
        f"{stats['people']} people found, {stats['failed']} failed"
    )
    extras = []
    if no_website:
        extras.append(f"{len(no_website)} without a website")
    if skipped:
        extras.append(f"{skipped} already crawled, skipped")
    if extras:
        message += f" ({'; '.join(extras)})"
    message += "."

    # Search with "find emails": check the emails once the crawl is done.
    if params.get("find_emails") and candidates:
        email_params = {"force": False, "use_hunter": True}
        if params.get("company_ids"):
            email_params["company_ids"] = list(params["company_ids"])
        session.add(
            SearchJob(
                kind="emails",
                query=job.query,
                location=job.location,
                source="emails",
                limit=job.limit,
                parent_job_id=job.parent_job_id,  # the search, so the UI can nest it
                params=email_params,
                progress_message="Waiting in queue...",
            )
        )
        message += " Email check queued."

    job.result_count = stats["with_emails"]
    job.status = "done"
    job.progress_message = _short(message)


# ----- emails (find + verify) jobs ----------------------------------------------


@dataclass
class _EmailWork:
    """What one company needs from the email engine. Plain data (no database objects),
    so it can be done in a helper thread."""

    company_id: int
    domain: str | None
    website_known: list[tuple[str, str, str]]  # (first, last, email) of people at the domain
    hunter_known: list[tuple[str, str, str]]  # same, from Hunter.io
    to_find: list[tuple[int, str, str | None]]  # (contact id, first name, last name) without email
    to_verify: list[str]


@dataclass
class _EmailFindings:
    pattern_confidence: int = 0
    found: dict = field(default_factory=dict)  # contact id -> FoundEmail
    checks: dict = field(default_factory=dict)  # email -> CheckResult
    error: str | None = None


def _find_and_verify(work: _EmailWork, force: bool) -> _EmailFindings:
    """Runs in a helper thread: learn the domain's pattern, guess missing people emails,
    verify the known emails with ONE verify_emails call. Never touches the worker's session."""
    out = _EmailFindings()
    try:
        if work.domain:
            if work.hunter_known:
                learn_pattern(work.domain, work.hunter_known, source="hunter")
            _, out.pattern_confidence = learn_pattern(work.domain, work.website_known, source="website")
            if work.to_find:
                # All people of the company in one go (their guesses are checked together).
                people = [(first, last) for _, first, last in work.to_find]
                guesses = find_people_emails(people, work.domain, force=force)
                for (contact_id, _, _), found in zip(work.to_find, guesses):
                    if found is not None:
                        out.found[contact_id] = found
        # Guessed emails were already checked by the finder: only verify the others.
        if work.to_verify:
            out.checks = verify_emails(work.to_verify, force=force)
    except Exception as e:  # one bad company never stops the job
        log.exception("Email check for company %s failed", work.company_id)
        out.error = f"{type(e).__name__}: {e}"
    return out


def _save_hunter_people(company: Company, people) -> int:
    """Add Hunter.io people as contacts (merging with existing ones). Returns new emails added.
    Generic addresses (info@, sales@: no person's name) only go to the company's emails."""
    added = 0
    for p in people:
        email = (p.email or "").strip().lower() or None
        if not email:
            continue
        name = " ".join(f"{p.first_name or ''} {p.last_name or ''}".split()) or None
        if not name or getattr(p, "email_type", None) == "generic":
            continue  # not a person: no contact row
        contact = _find_contact(company, name, email)
        if contact is None:
            contact = Contact(full_name=name, first_name=p.first_name, last_name=p.last_name, source="hunter")
            company.contacts.append(contact)
        for name_, value in (("title", p.position), ("linkedin_url", p.linkedin_url)):
            if value and not getattr(contact, name_):
                setattr(contact, name_, value)
        if not contact.email:
            contact.email = email
            contact.email_source = "hunter"
            contact.email_confidence = p.confidence
            added += 1
        _classify_contact(contact)
    hunter_emails = [p.email for p in people if p.email]
    company.emails = _merge_emails(company.emails or [], hunter_emails)
    _note_email_origin(company, hunter_emails, "hunter")
    return added


def _needs_hunter(company: Company, domain: str | None) -> bool:
    """Hunter.io is a backup: only for a company where we found nothing ourselves
    (no email at all, or the website couldn't be crawled)."""
    if not domain:
        return False
    no_emails = not company.email and not company.emails and not any(c.email for c in company.contacts)
    return no_emails or company.enrich_status == "failed"


def _hunter_asked_recently(domain: str) -> bool:
    """Hunter.io was already asked about this domain within EMAIL_CHECK_MAX_AGE_DAYS
    (each search costs a credit, even when it finds no one)."""
    with SessionLocal() as session:
        asked = session.scalar(select(DomainInfo.hunter_checked_at).where(DomainInfo.domain == domain))
    if asked is None:
        return False
    if asked.tzinfo is None:  # SQLite gives back naive datetimes; they are UTC
        asked = asked.replace(tzinfo=timezone.utc)
    return _now() - asked < timedelta(days=config.EMAIL_CHECK_MAX_AGE_DAYS)


def _remember_hunter_lookup(domain: str, pattern: str | None) -> None:
    """Note that Hunter was asked about `domain`, and keep its email pattern when we
    know nothing better."""
    for attempt in range(2):  # retry once if an email-check thread created the row meanwhile
        try:
            with SessionLocal() as session:
                info = session.scalar(select(DomainInfo).where(DomainInfo.domain == domain))
                if info is None:
                    info = DomainInfo(domain=domain)
                    session.add(info)
                info.hunter_checked_at = _now()
                if is_valid_pattern(pattern) and (not info.email_pattern or info.pattern_source == "guess"):
                    info.email_pattern, info.pattern_confidence, info.pattern_source = (
                        pattern, HUNTER_PATTERN_CONFIDENCE, "hunter")
                session.commit()
            return
        except IntegrityError:
            if attempt:
                log.warning("Could not save the Hunter.io lookup of %s", domain, exc_info=True)


def _prepare_email_work(company: Company) -> _EmailWork:
    domain = _company_domain(company)
    website_known, hunter_known, to_find = [], [], []
    for c in company.contacts:
        # Only real evidence teaches the pattern: emails from the website or Hunter. Our own
        # guesses (email_source "pattern") and invalid emails would just confirm themselves.
        if (c.email and c.first_name and c.last_name and domain and c.email.lower().endswith("@" + domain)
                and c.email_status != "invalid"):
            example = (c.first_name, c.last_name, c.email.lower())
            if c.email_source == "hunter":
                hunter_known.append(example)
            elif c.email_source in (None, "website"):
                website_known.append(example)
        if not c.email and c.first_name and domain:
            to_find.append((c.id, c.first_name, c.last_name))
    company_emails = _merge_emails([company.email] if company.email else [], company.emails or [])
    contact_emails = [c.email for c in company.contacts if c.email]
    return _EmailWork(
        company_id=company.id,
        domain=domain,
        website_known=website_known,
        hunter_known=hunter_known,
        to_find=to_find,
        to_verify=_merge_emails(company_emails, contact_emails),
    )


def _email_confidence(status: str, source: str | None, existing: int | None, pattern_conf: int) -> int:
    """How sure we are that a person's email reaches them (0-100)."""
    if status == "invalid":
        return 0
    confidence = {"valid": 95, "catch_all": pattern_conf or 60}.get(status, 40)
    if source == "website":  # listed on the company's own website
        confidence = max(confidence, WEBSITE_EMAIL_CONFIDENCE)
    elif source == "hunter" and existing:  # keep Hunter's own score when it is higher
        confidence = max(confidence, existing)
    return confidence


def _apply_findings(company: Company, findings: _EmailFindings, stats: dict) -> None:
    """Write the email engine's results onto the company and its people (worker thread)."""
    now = _now()
    checks = findings.checks

    # People emails guessed from the domain's pattern
    for contact in company.contacts:
        found = findings.found.get(contact.id)
        if found is not None and not contact.email:
            contact.email = found.email
            contact.email_source = "pattern"
            contact.email_status = found.status
            contact.email_confidence = found.confidence
            contact.email_checked_at = now
            stats["people_emails"] += 1

    # Company emails: status of each, deliverable ones first, best usable one as `email`
    emails = _merge_emails([company.email] if company.email else [], company.emails or [])
    statuses = {e: s for e, s in (company.email_statuses or {}).items() if e in emails}
    for e in emails:
        if e in checks:
            statuses[e] = checks[e].status
    emails.sort(key=lambda e: EMAIL_STATUS_RANK.get(statuses.get(e), 2))  # stable: keeps best-first order
    company.emails = emails
    company.email_statuses = statuses
    company.email = next((e for e in emails if statuses.get(e) != "invalid"), None)
    if any(e in checks for e in emails):
        company.emails_checked_at = now

    # People emails
    for contact in company.contacts:
        result = checks.get(contact.email.strip().lower()) if contact.email else None
        if result is None:
            continue
        found = findings.found.get(contact.id)
        pattern_conf = found.confidence if found is not None else findings.pattern_confidence
        contact.email_status = result.status
        contact.email_checked_at = now
        contact.email_confidence = _email_confidence(
            result.status, contact.email_source, contact.email_confidence, pattern_conf
        )

    for contact in company.contacts:
        _classify_contact(contact)
    rescore(company)

    all_statuses = list(statuses.values()) + [c.email_status for c in company.contacts if c.email]
    if "valid" in all_statuses:
        stats["valid"] += 1
    elif "catch_all" in all_statuses:
        stats["catch_all"] += 1


def run_emails_job(session, job: SearchJob) -> None:
    """Find + verify emails for companies and their people.
    job.result_count = companies with at least one valid or catch-all email."""
    params = job.params or {}
    force = bool(params.get("force"))
    use_hunter = params.get("use_hunter", True) is not False
    companies = _job_targets(session, job)
    job.total = len(companies)
    job.processed = 0
    job.result_count = 0
    session.commit()

    # a. "Find decision-makers": crawl websites not crawled yet, so we know the people.
    to_crawl = [c for c in companies if c.website and c.enrich_status != "done"] if params.get("crawl") else []
    if to_crawl:
        job.progress_message = f"Crawling {len(to_crawl)} websites first..."
        session.commit()

        def on_crawl_progress(done: int, total: int) -> None:
            job.progress_message = f"Crawling websites first: {done}/{total}"

        asyncio.run(_crawl_all(session, to_crawl, _new_crawl_stats(), on_crawl_progress))

    stats = {"valid": 0, "catch_all": 0, "people_emails": 0, "hunter_used": 0, "failed": 0}
    hunter_note = None  # e.g. the monthly limit message
    hunter_ok = use_hunter and hunter.available()
    hunter_domains: set[str] = set()  # asked in this job (branches of a chain often share a domain)

    def finish_company(company_id: int, findings: _EmailFindings) -> None:
        try:
            company = session.get(Company, company_id)
            _apply_findings(company, findings, stats)
            if findings.error:
                stats["failed"] += 1
            session.commit()
        except Exception:
            log.exception("Saving email results for company %s failed", company_id)
            session.rollback()
            stats["failed"] += 1
        job.processed += 1
        job.result_count = stats["valid"] + stats["catch_all"]
        job.progress_message = (
            f"Checking emails: {job.processed}/{job.total} companies — "
            f"{job.result_count} with a deliverable email"
        )
        session.commit()

    job.progress_message = f"Checking emails of {len(companies)} companies..."
    session.commit()
    with ThreadPoolExecutor(max_workers=EMAIL_JOB_THREADS, thread_name_prefix="email-job") as pool:
        futures = {}
        for company in companies:
            try:
                # c. Hunter.io as a backup when we have nothing for this company.
                # Never twice for one domain (not even with force, which means "re-verify").
                domain = _company_domain(company)
                if (hunter_ok and _needs_hunter(company, domain) and domain not in hunter_domains
                        and not _hunter_asked_recently(domain)):
                    hunter_domains.add(domain)
                    try:
                        pattern, people = hunter.domain_search(domain)
                        _remember_hunter_lookup(domain, pattern)
                        stats["hunter_used"] += 1
                        stats["people_emails"] += _save_hunter_people(company, people)
                        session.commit()
                    except UsageLimitError as e:
                        session.rollback()
                        hunter_ok, hunter_note = False, str(e)
                # b, d, e: pattern, guesses and verification run in a helper thread.
                work = _prepare_email_work(company)
            except Exception:
                log.exception("Preparing the email check for company %s failed", company.id)
                session.rollback()
                work = _EmailWork(company.id, None, [], [], [], [])
            futures[pool.submit(_find_and_verify, work, force)] = company.id

        # All database writes happen here, on the worker thread, one company at a time.
        for future in as_completed(futures):
            finish_company(futures[future], future.result())

    message = (
        f"Checked {len(companies)} companies: {stats['valid'] + stats['catch_all']} with a deliverable email "
        f"({stats['valid']} verified, {stats['catch_all']} catch-all), {stats['people_emails']} people emails found"
    )
    if stats["hunter_used"]:
        used, limit = get_usage("hunter")
        message += f", Hunter used {used}/{limit} this month"
    if stats["failed"]:
        message += f", {stats['failed']} failed"
    message += "."
    if hunter_note:
        message += " Hunter.io skipped: monthly limit reached."
    job.result_count = stats["valid"] + stats["catch_all"]
    job.status = "done"
    job.progress_message = _short(message)


# ----- the job loop --------------------------------------------------------------


def run_job(job_id: int) -> None:
    with SessionLocal() as session:
        job = session.get(SearchJob, job_id)
        if job is None:
            return
        job.status = "running"
        job.error = None
        session.commit()

        try:
            if job.kind == "enrich":
                run_enrich_job(session, job)
            elif job.kind == "emails":
                run_emails_job(session, job)
            else:
                run_search_job(session, job)
        except SourceError as e:
            session.rollback()
            job.status, job.error, job.progress_message = "failed", str(e), None
        except Exception as e:  # unexpected bug or network error
            log.exception("Job %s failed", job_id)
            session.rollback()
            job.status, job.error, job.progress_message = "failed", f"{type(e).__name__}: {e}", None
        job.finished_at = _now()
        session.commit()


def _mark_failed(job_id: int, error: str) -> None:
    """Last resort when run_job itself crashed (e.g. the database was locked)."""
    try:
        with SessionLocal() as session:
            job = session.get(SearchJob, job_id)
            if job is not None and job.status in ("queued", "running"):
                job.status, job.error, job.finished_at = "failed", error[:1000], _now()
                session.commit()
    except Exception:
        log.exception("Could not mark job %s as failed", job_id)


INTERRUPTED_PAID_SEARCH = "Interrupted by a restart — run the search again (to avoid paying twice)"


def recover_jobs() -> None:
    """Jobs left "running" by a crash/restart get another try - except Google searches:
    re-running them would spend paid requests a second time, so they are marked failed."""
    with SessionLocal() as session:
        for job in session.scalars(select(SearchJob).where(SearchJob.status == "running")):
            if job.kind == "search" and job.source == "google":
                job.status, job.error, job.finished_at = "failed", INTERRUPTED_PAID_SEARCH, _now()
                job.progress_message = None
            else:
                job.status = "queued"
        session.commit()


def _backfill_slice() -> bool:
    """Run while no job is waiting: give up to BACKFILL_LIMIT companies saved before sizing
    existed a size estimate, look up to BACKFILL_LIMIT US companies up in the PPP loan
    data (app/ppp.py) once it is downloaded, and give companies saved before buying intent
    existed their intent. Small slices, so a job the user starts never waits long. (Google
    PageSpeed runs in its own thread: _pagespeed_loop.) Returns True when there was
    something to do (call again soon)."""
    busy = False
    try:
        with SessionLocal() as session:
            n_sized = size_saved_companies(session, limit=BACKFILL_LIMIT)
        if n_sized:
            busy = True
            log.info("Estimated the size of %d companies saved earlier", n_sized)
    except Exception:
        log.exception("Could not estimate the size of the existing companies")
    try:
        if ppp.available():
            with SessionLocal() as session:
                waiting = session.scalar(select(Company.id).where(Company.ppp_checked_at.is_(None)).limit(1))
                if waiting is not None:
                    checked, matched = ppp.match_saved_companies(session, only_unchecked=True, limit=BACKFILL_LIMIT)
                    busy = True
                    if checked:
                        log.info("Looked up %d US companies in the PPP data: %d loans found", checked, matched)
    except Exception:
        log.exception("Could not look up the saved companies in the PPP data")
    try:
        with SessionLocal() as session:
            n_intent = intent_saved_companies(session, limit=BACKFILL_LIMIT)
        if n_intent:
            busy = True
            log.info("Scored the buying intent of %d companies saved earlier", n_intent)
    except Exception:
        log.exception("Could not score the buying intent of the existing companies")
    return busy


def pagespeed_round() -> int:
    """Test up to PAGESPEED_BACKFILL_LIMIT saved websites with Google PageSpeed
    (app/pagespeed.py), in a session of its own. Returns how many results were saved."""
    if not config.PAGESPEED_ENABLED:
        return 0
    from . import pagespeed  # here: it imports the scoring code, which imports a lot

    with SessionLocal() as session:
        return pagespeed.check_saved_companies(session, limit=PAGESPEED_BACKFILL_LIMIT)


def _pagespeed_loop(stop: threading.Event) -> None:
    """Own thread: PageSpeed tests take up to a minute each, and the job thread must stay
    free to start a search the user queues. Pauses briefly between tests while there is
    work, and longer when there is none or Google said stop (quota, no internet)."""
    while not stop.is_set():
        pause = PAGESPEED_IDLE_SECONDS
        try:
            n_checked = pagespeed_round()
            if n_checked:
                log.info("Checked %d websites with Google PageSpeed", n_checked)
                pause = PAGESPEED_PAUSE_SECONDS
        except Exception:
            log.exception("Could not check the saved websites with Google PageSpeed")
        stop.wait(pause)


def intent_saved_companies(session, limit: int = BACKFILL_LIMIT) -> int:
    """Companies saved before buying intent existed (intent_best_score NULL) are rescored,
    which also gives them their intent (app/intent.py). Afterwards intent_best_score is set
    (0 when nothing fits), so the next call skips them. Returns how many were rescored."""
    batch = list(session.scalars(
        select(Company).options(selectinload(Company.contacts))
        .where(Company.intent_best_score.is_(None)).order_by(Company.id).limit(limit)
    ))
    if not batch:
        return 0
    branches = branch_counts(session, batch)  # one query for the batch, not one per company
    for company in batch:
        rescore(company, branches[company.id])
    session.commit()
    return len(batch)


def _loop(stop: threading.Event) -> None:
    try:
        recover_jobs()
    except Exception:
        log.exception("Could not re-queue interrupted jobs")
    try:
        # Phones saved before they were standardised (app/phones.py) are read now (once).
        with SessionLocal() as session:
            n_companies, n_contacts = normalize_saved_phones(session)
        if n_companies or n_contacts:
            log.info("Standardised the phones of %d companies and %d people", n_companies, n_contacts)
    except Exception:
        log.exception("Could not standardise the saved phone numbers")
    try:
        # Leads saved before scores / job-title levels existed get them now (once).
        with SessionLocal() as session:
            n_companies, n_contacts = recompute_scores(session, only_missing=True)
        if n_companies or n_contacts:
            log.info("Scored %d companies and %d people saved earlier", n_companies, n_contacts)
    except Exception:
        log.exception("Could not score the existing leads")

    # Catch everything: one bad job or a locked database must never kill this thread.
    next_backfill = 0.0  # time.monotonic() when the next idle back-fill may run
    while not stop.is_set():
        job_id = None
        try:
            with SessionLocal() as session:
                job_id = session.scalar(
                    select(SearchJob.id).where(SearchJob.status == "queued").order_by(SearchJob.id).limit(1)
                )
            if job_id is None:
                # Nothing to do: fill in a few older companies, then check for jobs again.
                if time.monotonic() >= next_backfill:
                    busy = _backfill_slice()
                    next_backfill = time.monotonic() + (0 if busy else BACKFILL_RECHECK_SECONDS)
                    if busy:
                        continue
                stop.wait(POLL_SECONDS)
                continue
            log.info("Running job %s", job_id)
            run_job(job_id)
        except Exception as e:
            log.exception("Worker error (job %s)", job_id)
            if job_id is not None:
                _mark_failed(job_id, f"{type(e).__name__}: {e}")
            stop.wait(POLL_SECONDS)
            continue
        time.sleep(1)  # be polite to free data services


def start_worker() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), daemon=True, name="job-worker").start()
    threading.Thread(target=_pagespeed_loop, args=(stop,), daemon=True, name="pagespeed").start()
    return stop
