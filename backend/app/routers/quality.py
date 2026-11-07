"""Lead quality tools: the do-not-contact list and bounced emails (app/blocklist.py)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import blocklist, fit, insights
from ..db import get_session
from ..models import Company, DoNotContact
from ..schemas import (
    BouncedRequest,
    BouncedResult,
    DoNotContactAdd,
    DoNotContactAddResult,
    DoNotContactCompany,
    DoNotContactOut,
    IdealCustomer,
    IdealCustomerSaved,
    InsightsOut,
)
from ..scoring import recompute_scores, rescore

router = APIRouter(prefix="/api/quality", tags=["quality"])


def _out(session: Session, row: DoNotContact) -> DoNotContactOut:
    item = DoNotContactOut.model_validate(row)
    item.matches = blocklist.matches(session, row)
    return item


@router.get("/do-not-contact", response_model=list[DoNotContactOut])
def list_do_not_contact(session: Session = Depends(get_session)):
    """The whole list, newest first, with how many leads each entry hides."""
    rows = session.scalars(select(DoNotContact).order_by(DoNotContact.created_at.desc(), DoNotContact.id.desc()))
    return [_out(session, r) for r in rows]


@router.post("/do-not-contact", response_model=DoNotContactAddResult, status_code=201)
def add_do_not_contact(body: DoNotContactAdd, session: Session = Depends(get_session)):
    """Add pasted emails and domains (website addresses work too)."""
    parsed = blocklist.parse_entries(body.text)
    if not parsed.entries and not parsed.invalid:
        raise HTTPException(422, "Paste at least one email or domain.")
    added = blocklist.add_entries(session, parsed.entries, body.reason)
    session.commit()
    return DoNotContactAddResult(added=added, already=len(parsed.entries) - added, invalid=parsed.invalid[:50])


@router.post("/do-not-contact/company/{company_id}", response_model=DoNotContactOut, status_code=201)
def block_company(company_id: int, body: DoNotContactCompany, session: Session = Depends(get_session)):
    """Put one business on the list (the "Do not contact" button on a business)."""
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Company not found")
    row = blocklist.add_company(session, company, body.reason)
    session.commit()
    return _out(session, row)


@router.delete("/do-not-contact/{entry_id}", status_code=204)
def remove_do_not_contact(entry_id: int, session: Session = Depends(get_session)):
    row = session.get(DoNotContact, entry_id)
    if row is None:
        raise HTTPException(404, "Not on the list")
    session.delete(row)
    session.commit()


@router.delete("/do-not-contact/company/{company_id}", status_code=204)
def unblock_company(company_id: int, session: Session = Depends(get_session)):
    row = session.scalar(select(DoNotContact).where(DoNotContact.kind == "company",
                                                    DoNotContact.company_id == company_id))
    if row is None:
        raise HTTPException(404, "This business is not on the list by itself (maybe its domain or email is)")
    session.delete(row)
    session.commit()


@router.post("/bounced", response_model=BouncedResult)
def report_bounced(body: BouncedRequest, session: Session = Depends(get_session)):
    """Emails that bounced: invalid everywhere from now on, and their pattern trusted less."""
    raw = [t for t in blocklist.SPLIT_RE.split(body.text) if t.strip()]
    result = blocklist.mark_bounced(session, raw)
    if result.touched_company_ids:
        for company in session.scalars(select(Company).options(selectinload(Company.contacts))
                                       .where(Company.id.in_(result.touched_company_ids))):
            rescore(company)
    session.commit()
    return BouncedResult(emails=result.emails, companies=result.companies, people=result.people,
                         patterns_weakened=result.patterns_weakened, invalid=result.invalid[:50])


# ----- your ideal customer ------------------------------------------------------------


def _fit_counts(session: Session) -> tuple[int, int]:
    counts = dict(session.execute(select(Company.fit, func.count()).group_by(Company.fit)).all())
    return counts.get("good", 0), counts.get("poor", 0)


@router.get("/ideal-customer", response_model=IdealCustomerSaved)
def get_ideal_customer(session: Session = Depends(get_session)):
    good, poor = _fit_counts(session)
    return IdealCustomerSaved(profile=IdealCustomer(**fit.current()), good=good, poor=poor)


@router.put("/ideal-customer", response_model=IdealCustomerSaved)
def save_ideal_customer(body: IdealCustomer, session: Session = Depends(get_session)):
    """Save the profile and re-score every business with it."""
    profile = fit.save(session, body.model_dump())
    session.commit()
    recompute_scores(session)
    good, poor = _fit_counts(session)
    return IdealCustomerSaved(profile=IdealCustomer(**profile), good=good, poor=poor)


# ----- what wins ------------------------------------------------------------------------


@router.get("/insights", response_model=InsightsOut)
def what_wins(session: Session = Depends(get_session)):
    """Which signals come with replies and wins, from the lead statuses you set."""
    r = insights.build(session)
    return InsightsOut(
        worked=r.worked, good=r.good, open=r.open, baseline=r.baseline,
        no_reply_days=insights.NO_REPLY_DAYS, min_leads=insights.MIN_LEADS,
        signals=[vars(s) for s in r.signals], by_score=r.by_score,
    )
