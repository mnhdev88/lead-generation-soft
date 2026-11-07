"""Lead score (0-100): how promising a company or a person is to contact.

The score is transparent: every point comes with a reason such as "Verified email +25",
stored in score_reasons and shown in the UI. Change the weights below to tune it.

Company score
    Best email verified +25 / catch-all +8 to +20 (see below) / unverified +8
    Phone +10 (+5 more for a mobile number: WhatsApp / SMS). A number that can't exist counts 0.
    Decision-maker with email +20 (decision-maker without email +8)
    Website crawled +5
    Rating >= 4.5 with 50+ reviews +10 (rating >= 4.0 +5)
    Website-sales opportunities: no website +10, no SSL +8, outdated copyright +5, no contact form +3
    Not operating (closed) -30
    Chain or franchise location -10: decisions are usually made at head office
    Outside your ideal customer (app/fit.py) -20
    Still active: a Google review in the last 3 months +5
    Gone quiet: no Google review in 18+ months (and 5+ reviews in all) -10: may be winding down
    Status Lost / Not interested -> score 0

Person score
    Email verified +40 / catch-all +12 to +35 (see below) / unverified +10 / invalid 0
    Decision-maker +30
    Seniority: owner or C-level +15, director +10, manager +5
    Phone +5 (not for a number that can't exist), LinkedIn +5
    Confirmed by 2+ independent sources (website, LinkedIn search, business register...) +10
    Plus a tenth of the company score (company 70 -> +7)
    Status Lost / Not interested -> score 0

Catch-all emails: the mail server accepts every address, so it can't say whether this one
exists. The points depend on the other evidence for the address:
    published by the business (website / listing) .... company +20, person +35
    known to Hunter.io / Apollo.io .................... company +18, person +30
    guessed from a proven email pattern (80%+) ........ person +25
    only a guess ...................................... company +8,  person +12 ("risky")
"""
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from .fit import apply_fit
from .intent import BROKEN_SITE_LABELS, apply_intent, broken_site
from .models import Company, Contact
from .people import classify
from .phones import is_usable
from .sizing import apply_size, branch_counts

# --- weights -------------------------------------------------------------------------

COMPANY_EMAIL_POINTS = {"valid": 25, "catch_all": 15, "unverified": 8}
# Catch-all emails by evidence (see the module docstring): {evidence: (company, person)}
CATCH_ALL_POINTS = {"published": (20, 35), "provider": (18, 30), "pattern": (15, 25), "guess": (8, 12)}
CATCH_ALL_LABELS = {
    "published": "Catch-all email, published by the business",
    "provider": "Catch-all email, known to Hunter/Apollo",
    "pattern": "Catch-all email, matches their proven email pattern",
    "guess": "Catch-all email, only a guess (risky)",
}
PROVEN_PATTERN_CONFIDENCE = 80
COMPANY_PHONE = 10
COMPANY_MOBILE = 5  # extra for a mobile number
COMPANY_DECISION_MAKER_WITH_EMAIL = 20
COMPANY_DECISION_MAKER_NO_EMAIL = 8
COMPANY_CRAWLED = 5
COMPANY_GREAT_RATING = 10  # rating >= 4.5 and 50+ reviews
COMPANY_GOOD_RATING = 5  # rating >= 4.0
COMPANY_NO_WEBSITE = 10
COMPANY_NO_SSL = 8
COMPANY_OLD_COPYRIGHT = 5
COMPANY_NO_CONTACT_FORM = 3
COMPANY_NOT_OPERATING = -30
COMPANY_ACTIVE = 5
COMPANY_CHAIN = -10
COMPANY_POOR_FIT = -20
COMPANY_QUIET = -10
ACTIVE_DAYS = 90  # newest Google review at most this old = still active
QUIET_DAYS = 548  # newest review older than this (18 months) = gone quiet
QUIET_MIN_REVIEWS = 5  # (a business with 2 reviews in all is just small, not quiet)

CONTACT_EMAIL_POINTS = {"valid": 40, "catch_all": 25, "unverified": 10}
CONTACT_DECISION_MAKER = 30
CONTACT_SENIORITY_POINTS = {"owner": 15, "c_level": 15, "director": 10, "manager": 5}
CONTACT_PHONE = 5
CONTACT_LINKEDIN = 5
CONTACT_CONFIRMED = 10
SOURCE_LABELS = {"website": "website", "linkedin_search": "LinkedIn", "apollo": "Apollo", "hunter": "Hunter",
                 "registry": "business register"}

# Statuses that mean "don't bother": the score drops to 0.
DEAD_STATUSES = {"lost": "Lost", "not_interested": "Not interested"}

EMAIL_LABELS = {"valid": "Verified email", "catch_all": "Catch-all email", "unverified": "Unverified email"}
SENIORITY_LABELS = {"owner": "Owner / Founder", "c_level": "C-level", "director": "Director", "manager": "Manager"}


def _reason(label: str, points: int) -> str:
    """'Phone', 10 -> 'Phone +10' ; 'Closed', -30 -> 'Closed -30'"""
    return f"{label} {points:+d}"


def _email_grade(email: str | None, status: str | None) -> str | None:
    """valid | catch_all | unverified | None (no email, or the email is invalid)."""
    if not email:
        return None
    if status in ("valid", "catch_all"):
        return status
    if status == "invalid":
        return None
    return "unverified"  # not checked yet, unknown or risky


def _best_company_grade(company: Company) -> str | None:
    """Best grade among the company's emails (valid beats catch-all beats unverified)."""
    return _best_company_email(company)[0]


def _best_company_email(company: Company) -> tuple[str | None, str | None]:
    """(grade, email) of the company's best email; a catch-all one with the most evidence."""
    statuses = company.email_statuses or {}
    emails = list(company.emails or [])
    if company.email:
        emails.insert(0, company.email)
    graded = [(_email_grade(e, statuses.get(e) or statuses.get(e.lower())), e) for e in emails]
    for grade in ("valid", "catch_all", "unverified"):
        matching = [e for g, e in graded if g == grade]
        if matching:
            if grade == "catch_all":
                order = list(CATCH_ALL_POINTS)
                matching.sort(key=lambda e: order.index(company_email_evidence(company, e)))
            return grade, matching[0]
    return None, None


def company_email_evidence(company: Company, email: str) -> str:
    """published | provider | guess: why we believe a business's email exists."""
    source = (company.email_sources or {}).get(email.lower())
    if source in ("website", "source"):
        return "published"
    if source in ("hunter", "apollo"):
        return "provider"
    return "guess"


def contact_email_evidence(contact: Contact) -> str:
    """published | provider | pattern | guess: why we believe a person's email exists."""
    if contact.email_source == "website":
        return "published"
    if contact.email_source in ("hunter", "apollo"):
        return "provider"
    if contact.email_source == "pattern" and (contact.email_confidence or 0) >= PROVEN_PATTERN_CONFIDENCE:
        return "pattern"
    return "guess"


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _contact_has_email(contact: Contact) -> bool:
    return _email_grade(contact.email, contact.email_status) is not None


def _finish(points: list[tuple[str, int]], lead_status: str | None) -> tuple[int, list[str]]:
    """Add up, clamp to 0-100 and handle dead statuses."""
    reasons = [_reason(label, n) for label, n in points]
    if lead_status in DEAD_STATUSES:
        return 0, [f"Status {DEAD_STATUSES[lead_status]} → 0"] + reasons
    total = sum(n for _, n in points)
    return max(0, min(100, total)), reasons


def score_company(company: Company) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons) for a company. Does not change the company."""
    points: list[tuple[str, int]] = []

    grade, email = _best_company_email(company)
    if grade == "catch_all":
        evidence = company_email_evidence(company, email)
        if evidence == "pattern":  # (businesses' own emails aren't pattern guesses)
            evidence = "guess"
        points.append((CATCH_ALL_LABELS[evidence], CATCH_ALL_POINTS[evidence][0]))
    elif grade:
        points.append((EMAIL_LABELS[grade], COMPANY_EMAIL_POINTS[grade]))
    if is_usable(company.phone, company.phone_type):
        points.append(("Phone", COMPANY_PHONE))
        if company.phone_type == "mobile":
            points.append(("Mobile number", COMPANY_MOBILE))

    deciders = [c for c in (company.contacts or []) if c.is_decision_maker]
    if any(_contact_has_email(c) for c in deciders):
        points.append(("Decision-maker with email", COMPANY_DECISION_MAKER_WITH_EMAIL))
    elif deciders:
        points.append(("Decision-maker (no email)", COMPANY_DECISION_MAKER_NO_EMAIL))

    rating, reviews = company.rating, company.reviews_count or 0
    if rating is not None and rating >= 4.5 and reviews >= 50:
        points.append((f"Rating {rating:g} ({reviews} reviews)", COMPANY_GREAT_RATING))
    elif rating is not None and rating >= 4.0:
        points.append((f"Rating {rating:g}", COMPANY_GOOD_RATING))

    # Opportunities for selling websites / marketing. Website facts only count when the
    # crawl succeeded: a blocked or timed-out site tells us nothing about SSL etc.
    signals = company.website_signals or {}
    broken = broken_site(company)
    if not company.website:
        points.append(("No website", COMPANY_NO_WEBSITE))
    elif broken:
        points.append((f"Website {BROKEN_SITE_LABELS[broken]}", COMPANY_NO_WEBSITE))
    elif company.enrich_status == "done":
        points.append(("Website crawled", COMPANY_CRAWLED))
        if signals.get("https") is False:
            points.append(("No SSL (http only)", COMPANY_NO_SSL))
        year = _as_int(signals.get("copyright_year"))
        if year and year < datetime.now(timezone.utc).year - 2:
            points.append((f"Outdated website (copyright {year})", COMPANY_OLD_COPYRIGHT))
        if signals.get("has_contact_form") is False:
            points.append(("No contact form", COMPANY_NO_CONTACT_FORM))

    if company.business_status and company.business_status.upper() != "OPERATIONAL":
        label = company.business_status.replace("_", " ").capitalize()  # "Closed temporarily"
        points.append((label, COMPANY_NOT_OPERATING))
    else:
        points += _activity_points(company)
    if company.chain:
        points.append((f"Chain or franchise location ({company.chain.lower()})", COMPANY_CHAIN))
    if company.fit == "poor":
        points.append((f"Outside your ideal customer: {'; '.join(company.fit_reasons or [])}", COMPANY_POOR_FIT))

    return _finish(points, company.lead_status)


def _newest_review(company: Company) -> datetime | None:
    raw = (company.review_signals or {}).get("newest_review") if isinstance(company.review_signals, dict) else None
    try:
        when = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _activity_points(company: Company) -> list[tuple[str, int]]:
    """Is the business still active? From its newest Google review (Google sends 5 reviews,
    mostly recent ones, so an old newest review means few or no new ones)."""
    newest = _newest_review(company)
    if newest is None:
        return []
    days = (datetime.now(timezone.utc) - newest).days
    if days <= ACTIVE_DAYS:
        return [("Active: a Google review in the last 3 months", COMPANY_ACTIVE)]
    if days >= QUIET_DAYS and (company.reviews_count or 0) >= QUIET_MIN_REVIEWS:
        return [(f"Gone quiet: no Google review in {days // 30} months", COMPANY_QUIET)]
    return []


def score_contact(contact: Contact, company: Company | None = None) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons) for a person. Uses company.score if given (call
    score_company / rescore first so it is up to date)."""
    points: list[tuple[str, int]] = []

    grade = _email_grade(contact.email, contact.email_status)
    if grade == "catch_all":
        evidence = contact_email_evidence(contact)
        points.append((CATCH_ALL_LABELS[evidence], CATCH_ALL_POINTS[evidence][1]))
    elif grade:
        points.append((EMAIL_LABELS[grade], CONTACT_EMAIL_POINTS[grade]))
    if contact.is_decision_maker:
        points.append(("Decision-maker", CONTACT_DECISION_MAKER))
    if contact.seniority in CONTACT_SENIORITY_POINTS:
        points.append((SENIORITY_LABELS[contact.seniority], CONTACT_SENIORITY_POINTS[contact.seniority]))
    if is_usable(contact.phone, contact.phone_type):
        points.append(("Phone", CONTACT_PHONE))
    if contact.linkedin_url:
        points.append(("LinkedIn", CONTACT_LINKEDIN))
    sources = person_sources(contact)
    if len(sources) >= 2:
        names = " + ".join(SOURCE_LABELS.get(s, s) for s in sources)
        points.append((f"Confirmed by {len(sources)} sources ({names})", CONTACT_CONFIRMED))
    if company is not None and company.score:
        bonus = round(company.score / 10)
        if bonus:
            points.append((f"Company score {company.score}", bonus))

    return _finish(points, contact.lead_status)


def person_sources(contact: Contact) -> list[str]:
    """The sources that found this person (people saved before confirmed_by existed: their source)."""
    sources = list(contact.confirmed_by or [])
    if contact.source and contact.source not in sources:
        sources.insert(0, contact.source)
    return sources


def rescore(company: Company, branches: int | None = None) -> None:
    """Update the scores of a company and all its people, the company's size estimate
    (app/sizing.py) and its buying intent (app/intent.py, which reads the size).
    branches: the company's branches, when already counted for a whole batch
    (sizing.branch_counts); None = count them now. The caller commits."""
    apply_size(company, branches)  # first: the score reads company.chain...
    apply_fit(company)  # ...and the fit (which reads the size)
    company.score, company.score_reasons = score_company(company)
    for contact in company.contacts or []:
        contact.score, contact.score_reasons = score_contact(contact, company)
    apply_intent(company)  # pure: no extra queries


RECOMPUTE_BATCH = 200  # companies per database commit


def _classify_if_missing(contact: Contact) -> None:
    """People saved before titles were classified get their seniority now."""
    if contact.title and contact.seniority is None:
        result = classify(contact.title)
        contact.seniority, contact.department = result.seniority, result.department
        contact.is_decision_maker = result.is_decision_maker


def recompute_scores(session: Session, *, only_missing: bool = False) -> tuple[int, int]:
    """Recalculate lead scores (and fill in missing job-title levels), committing every
    RECOMPUTE_BATCH companies. only_missing=True only touches leads that were never scored
    (run at startup, so leads saved before scoring existed get a score).
    Returns (companies, contacts) recalculated."""
    n_companies = n_contacts = 0
    stmt = select(Company).options(selectinload(Company.contacts))
    if only_missing:
        unscored_people = select(Contact.company_id).where(Contact.score.is_(None), Contact.company_id.is_not(None))
        stmt = stmt.where(or_(Company.score.is_(None), Company.id.in_(unscored_people)))
    last_id = 0
    while True:
        batch = list(session.scalars(stmt.where(Company.id > last_id).order_by(Company.id).limit(RECOMPUTE_BATCH)))
        if not batch:
            break
        branches = branch_counts(session, batch)  # one query for the batch, not one per company
        for company in batch:
            for contact in company.contacts:
                _classify_if_missing(contact)
            rescore(company, branches[company.id])
            n_companies += 1
            n_contacts += len(company.contacts)
        last_id = batch[-1].id
        session.commit()
    # People not linked to any company
    lonely = select(Contact).where(Contact.company_id.is_(None))
    if only_missing:
        lonely = lonely.where(Contact.score.is_(None))
    for contact in session.scalars(lonely):
        _classify_if_missing(contact)
        contact.score, contact.score_reasons = score_contact(contact, None)
        n_contacts += 1
    session.commit()
    return n_companies, n_contacts
