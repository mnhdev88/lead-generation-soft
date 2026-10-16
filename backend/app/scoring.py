"""Lead score (0-100): how promising a company or a person is to contact.

The score is transparent: every point comes with a reason such as "Verified email +25",
stored in score_reasons and shown in the UI. Change the weights below to tune it.

Company score
    Best email verified +25 / catch-all +15 / unverified +8
    Phone +10 (+5 more for a mobile number: WhatsApp / SMS). A number that can't exist counts 0.
    Decision-maker with email +20 (decision-maker without email +8)
    Website crawled +5
    Rating >= 4.5 with 50+ reviews +10 (rating >= 4.0 +5)
    Website-sales opportunities: no website +10, no SSL +8, outdated copyright +5, no contact form +3
    Not operating (closed) -30
    Status Lost / Not interested -> score 0

Person score
    Email verified +40 / catch-all +25 / unverified +10 / invalid 0
    Decision-maker +30
    Seniority: owner or C-level +15, director +10, manager +5
    Phone +5 (not for a number that can't exist), LinkedIn +5
    Plus a tenth of the company score (company 70 -> +7)
    Status Lost / Not interested -> score 0
"""
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from .models import Company, Contact
from .people import classify
from .phones import is_usable
from .sizing import apply_size, branch_counts

# --- weights -------------------------------------------------------------------------

COMPANY_EMAIL_POINTS = {"valid": 25, "catch_all": 15, "unverified": 8}
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

CONTACT_EMAIL_POINTS = {"valid": 40, "catch_all": 25, "unverified": 10}
CONTACT_DECISION_MAKER = 30
CONTACT_SENIORITY_POINTS = {"owner": 15, "c_level": 15, "director": 10, "manager": 5}
CONTACT_PHONE = 5
CONTACT_LINKEDIN = 5

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
    statuses = company.email_statuses or {}
    emails = list(company.emails or [])
    if company.email:
        emails.insert(0, company.email)
    order = ["valid", "catch_all", "unverified"]
    grades = [_email_grade(e, statuses.get(e) or statuses.get(e.lower())) for e in emails]
    return next((g for g in order if g in grades), None)


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

    grade = _best_company_grade(company)
    if grade:
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
    if not company.website:
        points.append(("No website", COMPANY_NO_WEBSITE))
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

    return _finish(points, company.lead_status)


def score_contact(contact: Contact, company: Company | None = None) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons) for a person. Uses company.score if given (call
    score_company / rescore first so it is up to date)."""
    points: list[tuple[str, int]] = []

    grade = _email_grade(contact.email, contact.email_status)
    if grade:
        points.append((EMAIL_LABELS[grade], CONTACT_EMAIL_POINTS[grade]))
    if contact.is_decision_maker:
        points.append(("Decision-maker", CONTACT_DECISION_MAKER))
    if contact.seniority in CONTACT_SENIORITY_POINTS:
        points.append((SENIORITY_LABELS[contact.seniority], CONTACT_SENIORITY_POINTS[contact.seniority]))
    if is_usable(contact.phone, contact.phone_type):
        points.append(("Phone", CONTACT_PHONE))
    if contact.linkedin_url:
        points.append(("LinkedIn", CONTACT_LINKEDIN))
    if company is not None and company.score:
        bonus = round(company.score / 10)
        if bonus:
            points.append((f"Company score {company.score}", bonus))

    return _finish(points, contact.lead_status)


def rescore(company: Company, branches: int | None = None) -> None:
    """Update the scores of a company and all its people, and the company's size estimate
    (app/sizing.py). branches: the company's branches, when already counted for a whole
    batch (sizing.branch_counts); None = count them now. The caller commits."""
    company.score, company.score_reasons = score_company(company)
    for contact in company.contacts or []:
        contact.score, contact.score_reasons = score_contact(contact, company)
    apply_size(company, branches)


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
