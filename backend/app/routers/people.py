"""People search: every contact found at every company, with filters like a B2B database
("owners and managers of dentists in Miami with a verified email")."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, String, case, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import exporting
from ..db import get_session
from ..models import Activity, Company, Contact, JobResult, ListItem
from ..people import SENIORITY_RANK
from ..schemas import ContactUpdate, PersonOut, PersonPage
from ..scoring import score_contact

router = APIRouter(prefix="/api/people", tags=["people"])

STATUS_LABELS = {
    "new": "New", "contacted": "Contacted", "replied": "Replied", "interested": "Interested",
    "won": "Won", "lost": "Lost", "not_interested": "Not interested",
}


def _split(text: str | None) -> list[str]:
    """'owner, manager,' -> ['owner', 'manager']"""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _title_words():
    """SQL for ' ' + title in lower case with punctuation turned into spaces, so a keyword
    can be matched at the start of a word: 'cto' matches 'CTO/Founder' but not 'Director'."""
    text = func.lower(func.coalesce(Contact.title, ""))
    for ch in "/,&-.()|:;•+":
        text = func.replace(text, ch, " ", type_=String)
    return literal(" ", String) + text + " "  # "+" on text columns becomes SQL "||"


def people_filters(
    job_id: int | None = None,
    list_id: int | None = None,
    company_id: int | None = None,
    q: str | None = None,
    title: str | None = None,  # comma-separated keywords, any of them: "owner,manager"
    seniority: str | None = None,  # comma-separated: "owner,c_level"
    decision_makers: bool | None = None,
    has_email: bool | None = None,
    email_status: str | None = None,  # comma-separated: "valid,catch_all"
    lead_status: str | None = None,
    city: str | None = None,
    category: str | None = None,
    min_score: int | None = None,
) -> Select:
    """Shared filters for the people list, exports and the Google Sheets export.
    Also usable as a plain function: people_filters(job_id=4, has_email=True)."""
    stmt = select(Contact).outerjoin(Company, Contact.company_id == Company.id)
    if job_id is not None:
        stmt = stmt.where(Contact.company_id.in_(select(JobResult.company_id).where(JobResult.job_id == job_id)))
    if list_id is not None:
        stmt = stmt.where(Contact.id.in_(select(ListItem.contact_id).where(ListItem.list_id == list_id)))
    if company_id is not None:
        stmt = stmt.where(Contact.company_id == company_id)
    if q and q.strip():
        pattern = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(or_(
            Contact.full_name.ilike(pattern, escape="\\"),
            Contact.title.ilike(pattern, escape="\\"),
            Contact.email.ilike(pattern, escape="\\"),
            Company.name.ilike(pattern, escape="\\"),
        ))
    keywords = _split(title)
    if keywords:
        words = _title_words()
        stmt = stmt.where(or_(*[
            words.like(f"% {_escape_like(k.lower())}%", escape="\\") for k in keywords
        ]))
    if _split(seniority):
        stmt = stmt.where(Contact.seniority.in_(_split(seniority)))
    if decision_makers is True:
        stmt = stmt.where(Contact.is_decision_maker.is_(True))
    elif decision_makers is False:
        stmt = stmt.where(or_(Contact.is_decision_maker.is_(False), Contact.is_decision_maker.is_(None)))
    if has_email is True:
        stmt = stmt.where(Contact.email.is_not(None), Contact.email != "")
    elif has_email is False:
        stmt = stmt.where(or_(Contact.email.is_(None), Contact.email == ""))
    if _split(email_status):
        stmt = stmt.where(Contact.email_status.in_(_split(email_status)))
    if lead_status:
        stmt = stmt.where(func.coalesce(Contact.lead_status, "new") == lead_status)
    if city and city.strip():
        stmt = stmt.where(Company.city.ilike(f"%{_escape_like(city.strip())}%", escape="\\"))
    if category and category.strip():
        stmt = stmt.where(Company.category.ilike(f"%{_escape_like(category.strip())}%", escape="\\"))
    if min_score is not None:
        stmt = stmt.where(Contact.score >= min_score)
    return stmt


def people_order(sort: str | None) -> list:
    """ORDER BY for 'seniority' (default: most senior first), 'score' or 'name'."""
    name = [Contact.full_name.is_(None), func.lower(Contact.full_name), Contact.id]
    if sort == "name":
        return name
    if sort == "score":
        return [Contact.score.is_(None), Contact.score.desc(), *name]
    rank = case(
        *[(Contact.seniority == level, n) for level, n in SENIORITY_RANK.items() if level is not None],
        else_=SENIORITY_RANK[None],
    )
    return [rank, Contact.is_decision_maker.desc(), Contact.score.is_(None), Contact.score.desc(), *name]


def _to_people(session: Session, contacts: list[Contact]) -> list[PersonOut]:
    """PersonOut list with list_ids filled (one query for the whole page)."""
    ids = [c.id for c in contacts]
    lists: dict[int, list[int]] = {}
    if ids:
        for contact_id, list_id in session.execute(
            select(ListItem.contact_id, ListItem.list_id).where(ListItem.contact_id.in_(ids)).order_by(ListItem.list_id)
        ):
            lists.setdefault(contact_id, []).append(list_id)
    people = []
    for c in contacts:
        person = PersonOut.model_validate(c)
        person.list_ids = lists.get(c.id, [])
        people.append(person)
    return people


def _ordered(stmt: Select, sort: str | None) -> Select:
    return stmt.options(selectinload(Contact.company)).order_by(*people_order(sort))


@router.get("", response_model=PersonPage)
def list_people(
    stmt: Select = Depends(people_filters),
    sort: str = Query("seniority", pattern="^(name|score|seniority)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    contacts = list(session.scalars(_ordered(stmt, sort).limit(limit).offset(offset)))
    return PersonPage(total=total, items=_to_people(session, contacts))


@router.get("/export.csv")
def export_people_csv(
    stmt: Select = Depends(people_filters),
    sort: str = Query("seniority", pattern="^(name|score|seniority)$"),
    session: Session = Depends(get_session),
):
    headers, rows = exporting.people_table(session.scalars(_ordered(stmt, sort)))
    return exporting.csv_response(headers, rows, "people.csv")


@router.get("/export.xlsx")
def export_people_xlsx(
    stmt: Select = Depends(people_filters),
    sort: str = Query("seniority", pattern="^(name|score|seniority)$"),
    session: Session = Depends(get_session),
):
    headers, rows = exporting.people_table(session.scalars(_ordered(stmt, sort)))
    return exporting.xlsx_response(headers, rows, "people.xlsx")


def change_contact_status(session: Session, contact: Contact, new_status: str) -> bool:
    """Set a person's lead status, log it on the company timeline and update the score.
    Returns False when nothing changed. The caller commits."""
    old = contact.lead_status or "new"
    if old == new_status:
        return False
    contact.lead_status = new_status
    if contact.company_id is not None:
        who = contact.full_name or contact.email or "Contact"
        session.add(Activity(
            company_id=contact.company_id,
            contact_id=contact.id,
            kind="status",
            text=f"{who} - Status: {STATUS_LABELS.get(old, old)} → {STATUS_LABELS.get(new_status, new_status)}",
        ))
    contact.score, contact.score_reasons = score_contact(contact, contact.company)
    return True


@router.patch("/{contact_id}", response_model=PersonOut)
def update_person(contact_id: int, body: ContactUpdate, session: Session = Depends(get_session)):
    contact = session.scalar(
        select(Contact).options(selectinload(Contact.company)).where(Contact.id == contact_id)
    )
    if contact is None:
        raise HTTPException(404, "Person not found")
    if body.lead_status is not None:
        change_contact_status(session, contact, body.lead_status)
        session.commit()
    return _to_people(session, [contact])[0]
