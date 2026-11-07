"""Do-not-contact list and bounced emails.

Do not contact: emails, domains and businesses that are never shown, contacted or exported
(current clients, people who said no, competitors...). Matched when the list is read, so a
business found tomorrow with a blocked domain is hidden too:

    company  hidden when its id, its website domain, its email or its email's domain is on the list
    person   hidden when their email or its domain is on the list, or their company is hidden

Bounced: an email you sent bounced. The address is marked invalid everywhere (and stays
invalid: a later re-check can't make it "valid" again), and when it was guessed from the
company's email pattern, that pattern is trusted less, so the other guesses are too.
"""
import re
from dataclasses import dataclass, field

from sqlalchemy import String, cast, exists, func, literal, or_, select
from sqlalchemy.orm import Session

from .emails.syntax import normalize_email, split_email
from .emails.verify import REASON_BOUNCED
from .models import Company, Contact, DomainInfo, DoNotContact, EmailCheck, utcnow
from .utils import extract_domain

REASONS = {"client": "Current client", "said_no": "Said no", "competitor": "Competitor", "other": "Other"}
# A bounced pattern guess makes the domain's pattern this much less trusted.
PATTERN_BOUNCE_PENALTY = 40
GUESS_BOUNCE_PENALTY = 40  # the same for the other guesses made with that pattern

SPLIT_RE = re.compile(r"[\s,;]+")
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


# ----- reading what was pasted ---------------------------------------------------------


@dataclass
class Parsed:
    entries: list[tuple[str, str]] = field(default_factory=list)  # (kind, value), no repeats
    invalid: list[str] = field(default_factory=list)


def parse_entries(text: str) -> Parsed:
    """Emails and domains (or website addresses) separated by new lines, commas or spaces."""
    out = Parsed()
    for token in SPLIT_RE.split(text or ""):
        token = token.strip().strip("<>\"'()")
        if not token:
            continue
        if "@" in token and "/" not in token:
            email = normalize_email(token)
            entry = ("email", email) if email else None
        else:
            domain = extract_domain(token)
            entry = ("domain", domain) if domain and _DOMAIN_RE.match(domain) else None
        if entry is None:
            out.invalid.append(token)
        elif entry not in out.entries:
            out.entries.append(entry)
    return out


def clean_reason(reason: str | None) -> str | None:
    reason = (reason or "").strip()[:100]
    return reason or None


def add_entries(session: Session, entries: list[tuple[str, str]], reason: str | None) -> int:
    """Add (kind, value) entries that aren't on the list yet. Returns how many were added.
    The caller commits."""
    if not entries:
        return 0
    have = set(session.execute(select(DoNotContact.kind, DoNotContact.value)).all())
    added = 0
    for kind, value in entries:
        if (kind, value) not in have:
            session.add(DoNotContact(kind=kind, value=value, reason=clean_reason(reason)))
            have.add((kind, value))
            added += 1
    return added


def add_company(session: Session, company: Company, reason: str | None) -> DoNotContact:
    """Put one business on the list (by id, so it stays hidden even without a website)."""
    value = str(company.id)
    row = session.scalar(select(DoNotContact).where(DoNotContact.kind == "company", DoNotContact.value == value))
    if row is None:
        row = DoNotContact(kind="company", value=value, company_id=company.id, label=company.name)
        session.add(row)
    row.reason = clean_reason(reason)
    return row


# ----- matching in the database --------------------------------------------------------


def _lower(column):
    """lower(column), with "" for an empty column: NOT (NULL IN ...) is NULL, which would hide
    every business without a domain or email."""
    return func.lower(func.coalesce(column, ""))


def _values(kind: str):
    return select(DoNotContact.value).where(DoNotContact.kind == kind)


def _email_domain_blocked(email_column):
    """The email's domain (after the @) is on the list."""
    return exists().where(
        DoNotContact.kind == "domain",
        _lower(email_column).like(literal("%@") + DoNotContact.value),
    )


def company_blocked():
    """SQL condition: this company is on the do-not-contact list."""
    return or_(
        Company.id.in_(select(DoNotContact.company_id).where(DoNotContact.kind == "company",
                                                             DoNotContact.company_id.is_not(None))),
        _lower(Company.domain).in_(_values("domain")),
        _lower(Company.email).in_(_values("email")),
        _email_domain_blocked(Company.email),
    )


def contact_blocked():
    """SQL condition: this person is on the list (or their company is). The query must
    join Company (outer join is fine)."""
    blocked_companies = select(Company.id).where(company_blocked())
    return or_(
        _lower(Contact.email).in_(_values("email")),
        _email_domain_blocked(Contact.email),
        func.coalesce(Contact.company_id, 0).in_(blocked_companies),
    )


# ----- matching in Python (to say *why* a lead is blocked) --------------------------------


class Matcher:
    """The whole list in memory (it is small), to label leads in API answers."""

    def __init__(self, session: Session):
        self.emails: dict[str, str] = {}
        self.domains: dict[str, str] = {}
        self.companies: dict[int, str] = {}
        for row in session.scalars(select(DoNotContact)):
            why = describe(row)
            if row.kind == "email":
                self.emails[row.value] = why
            elif row.kind == "domain":
                self.domains[row.value] = why
            elif row.company_id is not None:
                self.companies[row.company_id] = why

    def __bool__(self) -> bool:
        return bool(self.emails or self.domains or self.companies)

    def _email(self, email: str | None) -> str | None:
        email = (email or "").strip().lower()
        if not email:
            return None
        return self.emails.get(email) or self.domains.get(split_email(email)[1])

    def company(self, c: Company) -> str | None:
        return (self.companies.get(c.id) or self.domains.get((c.domain or "").lower())
                or self._email(c.email))

    def contact(self, p: Contact, company: Company | None) -> str | None:
        return self._email(p.email) or (self.company(company) if company is not None else None)


def describe(row: DoNotContact) -> str:
    """'Do not contact: acme.com (Current client)'"""
    what = (row.label or row.value) if row.kind == "company" else row.value
    reason = REASONS.get(row.reason or "", row.reason)
    return f"{what} ({reason})" if reason else what


def matches(session: Session, row: DoNotContact) -> int:
    """How many businesses (plus people for an email) one entry hides."""
    if row.kind == "company":
        return 1 if row.company_id else 0
    if row.kind == "domain":
        return session.scalar(select(func.count()).select_from(Company).where(or_(
            func.lower(Company.domain) == row.value,
            func.lower(Company.email).like("%@" + row.value),
        ))) or 0
    return (session.scalar(select(func.count()).select_from(Company).where(func.lower(Company.email) == row.value)) or 0) + \
        (session.scalar(select(func.count()).select_from(Contact).where(func.lower(Contact.email) == row.value)) or 0)


# ----- bounced emails --------------------------------------------------------------------


@dataclass
class BounceResult:
    emails: int = 0  # valid addresses marked
    companies: int = 0
    people: int = 0
    patterns_weakened: int = 0
    invalid: list[str] = field(default_factory=list)  # not email addresses
    touched_company_ids: set[int] = field(default_factory=set)


def mark_bounced(session: Session, raw_emails: list[str]) -> BounceResult:
    """Mark these addresses invalid everywhere and learn from it. The caller rescores
    result.touched_company_ids and commits."""
    out = BounceResult()
    emails: list[str] = []
    for raw in raw_emails:
        e = normalize_email(raw)
        if e is None:
            if raw.strip():
                out.invalid.append(raw.strip())
        elif e not in emails:
            emails.append(e)
    if not emails:
        return out
    out.emails = len(emails)
    now = utcnow()

    # 1. The email-check cache: invalid for good (app/emails/verify.py keeps this answer).
    cached = {r.email: r for r in session.scalars(select(EmailCheck).where(EmailCheck.email.in_(emails)))}
    for e in emails:
        row = cached.get(e) or EmailCheck(email=e)
        if e not in cached:
            session.add(row)
        row.status, row.reason, row.checked_at = "invalid", REASON_BOUNCED, now

    # 2. Businesses that list it.
    wanted = set(emails)
    for company in session.scalars(select(Company).where(or_(
        func.lower(Company.email).in_(emails),
        # (the JSON list as text: "..." around each address; checked exactly below)
        *[cast(Company.emails, String).like(f'%"{e}"%') for e in emails],
    ))):
        found = [e for e in (company.emails or []) if e.lower() in wanted]
        if company.email and company.email.lower() in wanted:
            found.append(company.email.lower())
        if not found:
            continue
        company.email_statuses = {**(company.email_statuses or {}), **{e.lower(): "invalid" for e in found}}
        if company.email and company.email.lower() in wanted:
            statuses = company.email_statuses
            company.email = next((e for e in company.emails or [] if statuses.get(e.lower()) != "invalid"), None)
        out.companies += 1
        out.touched_company_ids.add(company.id)

    # 3. People with it; a bounced pattern guess makes the pattern less trusted.
    weakened: dict[str, str] = {}  # domain -> pattern
    for person in session.scalars(select(Contact).where(func.lower(Contact.email).in_(emails))):
        person.email_status, person.email_checked_at = "invalid", now
        person.email_confidence = 0
        out.people += 1
        if person.company_id:
            out.touched_company_ids.add(person.company_id)
        if person.email_source == "pattern":
            domain = split_email(person.email.lower())[1]
            info = session.scalar(select(DomainInfo).where(DomainInfo.domain == domain))
            if info is not None and info.email_pattern and domain not in weakened:
                info.pattern_confidence = max(0, (info.pattern_confidence or 0) - PATTERN_BOUNCE_PENALTY)
                if info.pattern_confidence <= 30:
                    info.pattern_source = "guess"  # the next pattern learned replaces it
                weakened[domain] = info.email_pattern
    out.patterns_weakened = len(weakened)

    # The other guesses made with a weakened pattern (not proven by a mail server) are
    # probably wrong too.
    if weakened:
        for person in session.scalars(select(Contact).where(
            Contact.email_source == "pattern",
            Contact.email_status != "valid",
            or_(*[func.lower(Contact.email).like("%@" + d) for d in weakened]),
        )):
            if person.email and person.email.lower() not in wanted:
                person.email_confidence = max(0, (person.email_confidence or 0) - GUESS_BOUNCE_PENALTY)
                if person.company_id:
                    out.touched_company_ids.add(person.company_id)
    return out
