"""Saved lists of leads ("Miami dentists - call this week"). A list holds companies and/or
people; deleting a list never deletes the leads themselves."""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Company, Contact, LeadList, ListItem
from ..schemas import ListCreate, ListItemsRequest, ListItemsResult, ListOut, ListUpdate

router = APIRouter(prefix="/api/lists", tags=["lists"])


def _counts(session: Session, list_ids: list[int]) -> dict[int, tuple[int, int]]:
    """list id -> (companies, people) in one query."""
    if not list_ids:
        return {}
    rows = session.execute(
        select(ListItem.list_id, func.count(ListItem.company_id), func.count(ListItem.contact_id))
        .where(ListItem.list_id.in_(list_ids))
        .group_by(ListItem.list_id)
    )
    return {list_id: (companies, people) for list_id, companies, people in rows}


def _out(lead_list: LeadList, counts: tuple[int, int]) -> ListOut:
    return ListOut(
        id=lead_list.id,
        name=lead_list.name,
        description=lead_list.description,
        company_count=counts[0],
        contact_count=counts[1],
        created_at=lead_list.created_at,
    )


def _one_out(session: Session, lead_list: LeadList) -> ListOut:
    return _out(lead_list, _counts(session, [lead_list.id]).get(lead_list.id, (0, 0)))


def _get_list(session: Session, list_id: int) -> LeadList:
    lead_list = session.get(LeadList, list_id)
    if lead_list is None:
        raise HTTPException(404, "List not found")
    return lead_list


def _check_name_free(session: Session, name: str, except_id: int | None = None) -> None:
    stmt = select(LeadList.id).where(func.lower(LeadList.name) == name.lower())
    if except_id is not None:
        stmt = stmt.where(LeadList.id != except_id)
    if session.scalar(stmt) is not None:
        raise HTTPException(409, f'A list named "{name}" already exists.')


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise HTTPException(422, "List name can't be empty.")
    return name


@router.get("", response_model=list[ListOut])
def list_lists(session: Session = Depends(get_session)):
    lists = list(session.scalars(select(LeadList).order_by(func.lower(LeadList.name))))
    counts = _counts(session, [x.id for x in lists])
    return [_out(x, counts.get(x.id, (0, 0))) for x in lists]


@router.post("", response_model=ListOut, status_code=201)
def create_list(body: ListCreate, session: Session = Depends(get_session)):
    name = _clean_name(body.name)
    _check_name_free(session, name)
    lead_list = LeadList(name=name, description=(body.description or "").strip() or None)
    session.add(lead_list)
    session.commit()
    return _out(lead_list, (0, 0))


@router.patch("/{list_id}", response_model=ListOut)
def update_list(list_id: int, body: ListUpdate, session: Session = Depends(get_session)):
    lead_list = _get_list(session, list_id)
    if body.name is not None:
        name = _clean_name(body.name)
        _check_name_free(session, name, except_id=list_id)
        lead_list.name = name
    if "description" in body.model_fields_set:  # sending null clears it
        lead_list.description = (body.description or "").strip() or None
    session.commit()
    return _one_out(session, lead_list)


@router.delete("/{list_id}", status_code=204)
def delete_list(list_id: int, session: Session = Depends(get_session)):
    lead_list = _get_list(session, list_id)
    session.delete(lead_list)  # its items go too (cascade); the companies/people stay
    session.commit()
    return Response(status_code=204)


@router.post("/{list_id}/items", response_model=ListItemsResult)
def add_items(list_id: int, body: ListItemsRequest, session: Session = Depends(get_session)):
    """Add companies/people. Ones already in the list and unknown ids are skipped."""
    _get_list(session, list_id)
    added = 0
    for column, model, ids in (
        (ListItem.company_id, Company, body.company_ids),
        (ListItem.contact_id, Contact, body.contact_ids),
    ):
        wanted = set(ids)
        if not wanted:
            continue
        existing = set(session.scalars(select(model.id).where(model.id.in_(wanted))))
        already = set(session.scalars(select(column).where(ListItem.list_id == list_id, column.in_(wanted))))
        for item_id in sorted(existing - already):
            if model is Company:
                session.add(ListItem(list_id=list_id, company_id=item_id))
            else:
                session.add(ListItem(list_id=list_id, contact_id=item_id))
            added += 1
    session.commit()
    return ListItemsResult(changed=added)


@router.post("/{list_id}/items/remove", response_model=ListItemsResult)
def remove_items(list_id: int, body: ListItemsRequest, session: Session = Depends(get_session)):
    _get_list(session, list_id)
    removed = 0
    for column, ids in ((ListItem.company_id, body.company_ids), (ListItem.contact_id, body.contact_ids)):
        if ids:
            result = session.execute(
                delete(ListItem).where(ListItem.list_id == list_id, column.in_(set(ids)))
            )
            removed += result.rowcount or 0
    session.commit()
    return ListItemsResult(changed=removed)
