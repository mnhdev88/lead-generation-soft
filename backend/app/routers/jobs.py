from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import SearchJob
from ..schemas import EnrichJobRequest, FindEmailsJobRequest, JobCreate, JobOut, SourceOut
from ..sources import SOURCES

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/sources", response_model=list[SourceOut])
def list_sources():
    out = []
    for s in SOURCES.values():
        reason = s.unavailable_reason()
        out.append(
            SourceOut(
                name=s.name,
                label=s.label,
                description=s.description,
                available=reason is None,
                unavailable_reason=reason,
            )
        )
    return out


@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(body: JobCreate, session: Session = Depends(get_session)):
    source = SOURCES.get(body.source)
    if source is None:
        raise HTTPException(400, f"Unknown source '{body.source}'")
    reason = source.unavailable_reason()
    if reason:
        raise HTTPException(400, reason)
    # Job titles the user is looking for: "  Office Manager " -> "office manager"
    titles = []
    for t in body.titles:
        t = " ".join(t.split()).lower()
        if t and t not in titles:
            titles.append(t)
    job = SearchJob(
        kind="search",
        query=body.query.strip(),
        location=body.location.strip(),
        source=body.source,
        limit=body.limit,
        enrich=body.enrich or body.find_emails,  # finding emails needs the websites crawled first
        params={"find_emails": body.find_emails, "titles": titles},
        progress_message="Waiting in queue...",
    )
    session.add(job)
    session.commit()
    return job


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(session: Session = Depends(get_session)):
    return session.scalars(select(SearchJob).order_by(SearchJob.id.desc()).limit(100)).all()


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(SearchJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _finished_search(session: Session, job_id: int, error: str) -> SearchJob:
    """The search job `job_id`; 404 when missing, 400 (with `error`) unless it is a finished search."""
    parent = session.get(SearchJob, job_id)
    if parent is None:
        raise HTTPException(404, "Job not found")
    if parent.kind != "search" or parent.status != "done":
        raise HTTPException(400, error)
    return parent


@router.post("/jobs/{job_id}/enrich", response_model=JobOut, status_code=201)
def enrich_job_results(
    job_id: int,
    body: EnrichJobRequest | None = None,  # the body is optional
    session: Session = Depends(get_session),
):
    """Queue a website crawl for all the results of a finished search."""
    parent = _finished_search(session, job_id, "Only a finished search can be enriched.")
    force = body.force if body else False
    job = SearchJob(
        kind="enrich",
        query=parent.query,
        location=parent.location,
        source="crawler",
        limit=parent.limit,
        parent_job_id=parent.id,
        params={"force": force},
        progress_message="Waiting in queue...",
    )
    session.add(job)
    session.commit()
    return job


@router.post("/jobs/{job_id}/find-emails", response_model=JobOut, status_code=201)
def find_emails_for_job(
    job_id: int,
    body: FindEmailsJobRequest | None = None,  # the body is optional
    session: Session = Depends(get_session),
):
    """Queue an email check (find + verify) for all the results of a finished search."""
    parent = _finished_search(session, job_id, "Emails can only be checked for a finished search.")
    body = body or FindEmailsJobRequest()
    job = SearchJob(
        kind="emails",
        query=parent.query,
        location=parent.location,
        source="emails",
        limit=parent.limit,
        parent_job_id=parent.id,
        params={"force": body.force, "use_hunter": body.use_hunter},
        progress_message="Waiting in queue...",
    )
    session.add(job)
    session.commit()
    return job
