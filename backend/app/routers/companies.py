from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from .. import exporting
from ..db import get_session
from ..models import Activity, Company, Contact, JobResult, ListItem, SearchJob
from ..people import SENIORITY_RANK
from ..schemas import (
    ActivityOut,
    CompanyOut,
    CompanyPage,
    CompanyUpdate,
    EnrichCompaniesRequest,
    FindEmailsRequest,
    JobOut,
    NoteCreate,
)

router = APIRouter(prefix="/api/companies", tags=["companies"])

SORT_PATTERN = "^(name|score|rating|employees)$"
# Size bands (app/sizing.py), plus "unknown" for companies without a size estimate.
SizeBandFilter = Literal["solo", "small", "medium", "large", "unknown"]
SIZE_BAND_FILTERS: tuple[str, ...] = SizeBandFilter.__args__  # type: ignore[attr-defined]


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def filters(
    job_id: int | None = None,
    list_id: int | None = None,
    q: str | None = None,
    has_website: bool | None = None,
    has_phone: bool | None = None,
    has_email: bool | None = None,
    # done | failed | no_website, or "none" for companies that were never crawled
    enrich_status: str | None = None,
    lead_status: str | None = None,
    min_score: int | None = None,
    size_band: SizeBandFilter | None = None,  # solo|small|medium|large, or "unknown" = no estimate
) -> Select:
    """Shared query filters for the list and export endpoints (and the Google Sheets
    export, which calls it as a plain function: filters(job_id=4, has_email=True))."""
    stmt = select(Company)
    if job_id is not None:
        stmt = stmt.where(Company.id.in_(select(JobResult.company_id).where(JobResult.job_id == job_id)))
    if list_id is not None:
        stmt = stmt.where(Company.id.in_(select(ListItem.company_id).where(ListItem.list_id == list_id)))
    if q and q.strip():
        stmt = stmt.where(Company.name.ilike(f"%{_escape_like(q.strip())}%", escape="\\"))
    for column, wanted in ((Company.website, has_website), (Company.phone, has_phone), (Company.email, has_email)):
        if wanted is True:
            stmt = stmt.where(column.is_not(None), column != "")
        elif wanted is False:
            stmt = stmt.where((column.is_(None)) | (column == ""))
    if enrich_status == "none":
        stmt = stmt.where(Company.enrich_status.is_(None))
    elif enrich_status:
        stmt = stmt.where(Company.enrich_status == enrich_status)
    if lead_status:
        stmt = stmt.where(func.coalesce(Company.lead_status, "new") == lead_status)
    if min_score is not None:
        stmt = stmt.where(Company.score >= min_score)
    if size_band:
        # (Checked here too: the Google Sheets export calls this without FastAPI's checks.)
        if size_band not in SIZE_BAND_FILTERS:
            raise ValueError(f"size_band must be one of {', '.join(SIZE_BAND_FILTERS)}")
        if size_band == "unknown":
            stmt = stmt.where(Company.size_band.is_(None))
        else:
            stmt = stmt.where(Company.size_band == size_band)
    return stmt


def company_order(sort: str | None) -> list:
    """ORDER BY for 'name' (default), 'score', 'rating' or 'employees' (high to low, empty last)."""
    name = [func.lower(Company.name), Company.id]
    if sort == "score":
        return [Company.score.is_(None), Company.score.desc(), *name]
    if sort == "employees":
        return [Company.employees.is_(None), Company.employees.desc(), *name]
    if sort == "rating":
        return [Company.rating.is_(None), Company.rating.desc(), Company.reviews_count.desc(), *name]
    return name


def _ordered(stmt: Select, sort: str | None) -> Select:
    return stmt.options(selectinload(Company.contacts)).order_by(*company_order(sort))


def _contact_order(contact: Contact):
    """Most senior first, decision-makers before others, then by name."""
    return (
        SENIORITY_RANK.get(contact.seniority, SENIORITY_RANK[None]),
        not contact.is_decision_maker,
        (contact.full_name or "~").lower(),
        contact.id or 0,
    )


def _to_out(session: Session, companies: list[Company]) -> list[CompanyOut]:
    """CompanyOut list with sorted contacts and list_ids filled (one query for the page)."""
    ids = [c.id for c in companies]
    lists: dict[int, list[int]] = {}
    if ids:
        for company_id, list_id in session.execute(
            select(ListItem.company_id, ListItem.list_id).where(ListItem.company_id.in_(ids)).order_by(ListItem.list_id)
        ):
            lists.setdefault(company_id, []).append(list_id)
    out = []
    for c in companies:
        item = CompanyOut.model_validate(c)
        item.contacts = sorted(item.contacts, key=_contact_order)
        item.list_ids = lists.get(c.id, [])
        out.append(item)
    return out


def _get_company(session: Session, company_id: int) -> Company:
    company = session.scalar(
        select(Company).options(selectinload(Company.contacts)).where(Company.id == company_id)
    )
    if company is None:
        raise HTTPException(404, "Company not found")
    return company


@router.get("", response_model=CompanyPage)
def list_companies(
    stmt: Select = Depends(filters),
    sort: str = Query("name", pattern=SORT_PATTERN),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    items = list(session.scalars(_ordered(stmt, sort).limit(limit).offset(offset)))
    return CompanyPage(total=total, items=_to_out(session, items))


def _selected_ids(session: Session, company_ids: list[int]) -> list[int]:
    ids = sorted(set(company_ids))
    found = list(session.scalars(select(Company.id).where(Company.id.in_(ids)).order_by(Company.id)))
    if not found:
        raise HTTPException(400, "None of the selected companies exist.")
    return found


def _selected_label(n: int) -> str:
    return f"{n} selected compan{'y' if n == 1 else 'ies'}"


@router.post("/enrich", response_model=JobOut, status_code=201)
def enrich_companies(body: EnrichCompaniesRequest, session: Session = Depends(get_session)):
    """Queue a website crawl for hand-picked companies."""
    found = _selected_ids(session, body.company_ids)
    job = SearchJob(
        kind="enrich",
        query=_selected_label(len(found)),
        location="",
        source="crawler",
        limit=len(found),
        params={"company_ids": found, "force": body.force},
        progress_message="Waiting in queue...",
    )
    session.add(job)
    session.commit()
    return job


@router.post("/find-emails", response_model=JobOut, status_code=201)
def find_emails(body: FindEmailsRequest, session: Session = Depends(get_session)):
    """Queue an email check (find + verify) for hand-picked companies.
    crawl=true crawls websites not crawled yet first ("Find decision-makers")."""
    found = _selected_ids(session, body.company_ids)
    job = SearchJob(
        kind="emails",
        query=_selected_label(len(found)),
        location="",
        source="emails",
        limit=len(found),
        params={"company_ids": found, "force": body.force, "crawl": body.crawl, "use_hunter": body.use_hunter},
        progress_message="Waiting in queue...",
    )
    session.add(job)
    session.commit()
    return job


@router.get("/export.csv")
def export_csv(
    stmt: Select = Depends(filters),
    sort: str = Query("name", pattern=SORT_PATTERN),
    session: Session = Depends(get_session),
):
    headers, rows = exporting.company_table(session.scalars(_ordered(stmt, sort)))
    return exporting.csv_response(headers, rows, "leads.csv")


@router.get("/export.xlsx")
def export_xlsx(
    stmt: Select = Depends(filters),
    sort: str = Query("name", pattern=SORT_PATTERN),
    session: Session = Depends(get_session),
):
    headers, rows = exporting.company_table(session.scalars(_ordered(stmt, sort)))
    return exporting.xlsx_response(headers, rows, "leads.xlsx")


# Keep the "/{company_id}" routes last: they would otherwise also match "/export.csv".
@router.get("/{company_id}", response_model=CompanyOut)
def get_company(company_id: int, session: Session = Depends(get_session)):
    return _to_out(session, [_get_company(session, company_id)])[0]


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(company_id: int, body: CompanyUpdate, session: Session = Depends(get_session)):
    """Change the lead status (logged on the timeline as "Status: New → Contacted")."""
    # Imported here: export.py imports this module, so a top-level import would be circular.
    from .export import change_company_status

    company = _get_company(session, company_id)
    if body.lead_status is not None:
        change_company_status(session, company, body.lead_status)
        session.commit()
    return _to_out(session, [company])[0]


@router.get("/{company_id}/activities", response_model=list[ActivityOut])
def list_activities(company_id: int, session: Session = Depends(get_session)):
    """The company's timeline (notes and status changes), newest first."""
    _get_company(session, company_id)
    return session.scalars(
        select(Activity).where(Activity.company_id == company_id).order_by(Activity.created_at.desc(), Activity.id.desc())
    ).all()


@router.post("/{company_id}/notes", response_model=ActivityOut, status_code=201)
def add_note(company_id: int, body: NoteCreate, session: Session = Depends(get_session)):
    company = _get_company(session, company_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "The note is empty.")
    if body.contact_id is not None and body.contact_id not in {c.id for c in company.contacts}:
        raise HTTPException(400, "That person does not belong to this company.")
    note = Activity(company_id=company.id, contact_id=body.contact_id, kind="note", text=text)
    session.add(note)
    session.commit()
    return note
