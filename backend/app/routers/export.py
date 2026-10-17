"""Bulk lead-status changes, score recomputation and the Google Sheets export."""
import inspect

from fastapi import APIRouter, Depends, HTTPException
from fastapi.params import Param
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import exporting, scoring, sheets
from ..db import get_session
from ..models import Activity, Company, Contact
from ..schemas import (
    BulkStatusRequest,
    ListItemsResult,
    ScoreRecomputeResult,
    SheetsExportRequest,
    SheetsExportResult,
    SheetsStatus,
)
from ..scoring import rescore
from . import companies as companies_router
from .people import STATUS_LABELS, change_contact_status, people_filters, people_order

router = APIRouter(prefix="/api", tags=["export"])


def change_company_status(session: Session, company: Company, new_status: str) -> bool:
    """Set a company's lead status, log it on its timeline and update its score.
    Returns False when nothing changed. The caller commits."""
    old = company.lead_status or "new"
    if old == new_status:
        return False
    company.lead_status = new_status
    session.add(Activity(
        company_id=company.id,
        kind="status",
        text=f"Status: {STATUS_LABELS.get(old, old)} → {STATUS_LABELS.get(new_status, new_status)}",
    ))
    rescore(company)
    return True


@router.post("/status/bulk", response_model=ListItemsResult)
def bulk_status(body: BulkStatusRequest, session: Session = Depends(get_session)):
    """Set the lead status of many companies and/or people at once."""
    changed = 0
    if body.company_ids:
        for company in session.scalars(
            select(Company).options(selectinload(Company.contacts)).where(Company.id.in_(set(body.company_ids)))
        ):
            changed += change_company_status(session, company, body.lead_status)
    if body.contact_ids:
        for contact in session.scalars(
            select(Contact).options(selectinload(Contact.company)).where(Contact.id.in_(set(body.contact_ids)))
        ):
            changed += change_contact_status(session, contact, body.lead_status)
    session.commit()
    return ListItemsResult(changed=changed)


@router.post("/scores/recompute", response_model=ScoreRecomputeResult)
def recompute_scores(session: Session = Depends(get_session)):
    """Recalculate every lead score (e.g. after changing the weights in scoring.py)."""
    n_companies, n_contacts = scoring.recompute_scores(session)
    return ScoreRecomputeResult(companies=n_companies, contacts=n_contacts)


@router.get("/export/sheets/status", response_model=SheetsStatus)
def sheets_status():
    return sheets.status()


def call_filters(func, given: dict):
    """Call a filters function (written as a FastAPI dependency) directly with the
    filter values from a JSON body. Unknown keys (like "sort") are ignored and missing
    ones get their default."""
    kwargs = {}
    for name, param in inspect.signature(func).parameters.items():
        if name in given and given[name] not in (None, ""):
            kwargs[name] = given[name]
        elif isinstance(param.default, Param):  # e.g. Query(None): use its plain default
            kwargs[name] = param.default.default
    return func(**kwargs)


@router.post("/export/sheets", response_model=SheetsExportResult)
def export_to_sheets(body: SheetsExportRequest, session: Session = Depends(get_session)):
    """Write the filtered companies or people into a new tab of a Google Sheet."""
    try:
        if body.kind == "companies":
            stmt = call_filters(companies_router.filters, body.filters)
            items = session.scalars(
                stmt.options(selectinload(Company.contacts))
                .order_by(*companies_router.company_order(body.filters.get("sort"), body.filters.get("looking_for")))
            )
            headers, rows = exporting.company_table(items)
        else:
            stmt = call_filters(people_filters, body.filters)
            items = session.scalars(
                stmt.options(selectinload(Contact.company)).order_by(*people_order(body.filters.get("sort")))
            )
            headers, rows = exporting.people_table(items)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Invalid filters: {e}")
    try:
        return sheets.export_rows(headers, rows, body.spreadsheet, body.tab_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
