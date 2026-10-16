"""Check a few email addresses by hand ("is this email real?")."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import EmailCheck
from ..schemas import EmailCheckOut, VerifyEmailsRequest

router = APIRouter(prefix="/api/emails", tags=["emails"])


# A plain "def" (not async): verification talks to mail servers and may take a few
# seconds, so FastAPI runs it in a worker thread.
@router.post("/verify", response_model=list[EmailCheckOut])
def verify(body: VerifyEmailsRequest, session: Session = Depends(get_session)):
    from ..emails import normalize_email, verify_emails  # imported here: keeps app start-up light

    # Keep the user's order and drop duplicates ("Info@x.com" and "info@x.com" are one address).
    typed_list: list[str] = []
    seen = set()
    for raw in body.emails:
        typed = raw.strip()
        key = normalize_email(typed) or typed.lower()
        if typed and key not in seen:
            seen.add(key)
            typed_list.append(typed)

    # Badly formatted addresses come back as status "invalid" with the reason.
    results = verify_emails(typed_list, force=body.force) if typed_list else {}
    emails = [r.email for r in results.values()]
    checked_at = dict(session.execute(
        select(EmailCheck.email, EmailCheck.checked_at).where(EmailCheck.email.in_(emails))
    ).all()) if emails else {}
    now = datetime.now(timezone.utc)

    out = []
    for typed in typed_list:
        r = results.get(typed)
        if r is None:  # should not happen; never fail the whole request for one address
            out.append(EmailCheckOut(
                email=typed, status="unknown", reason="Could not be checked", is_role=False, is_free=False,
                is_disposable=False, mx_found=None, smtp_code=None, checked_at=now,
            ))
            continue
        out.append(EmailCheckOut(
            email=r.email, status=r.status, reason=r.reason, is_role=r.is_role, is_free=r.is_free,
            is_disposable=r.is_disposable, mx_found=r.mx_found, smtp_code=r.smtp_code,
            checked_at=checked_at.get(r.email) or now,
        ))
    return out
