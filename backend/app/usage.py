"""Monthly usage counter for paid APIs, so we never go past the free allowance.

Each request to a paid API first calls consume("google_places"). The count is stored
per calendar month (UTC) in the api_usage table; when the monthly limit would be
exceeded, consume() raises UsageLimitError and the request is not sent.
"""
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import config
from .db import SessionLocal
from .models import ApiUsage
from .sources.base import SourceError

# provider -> monthly request limit
PROVIDERS: dict[str, int] = {
    "google_places": config.GOOGLE_PLACES_MONTHLY_LIMIT,
    "hunter": config.HUNTER_MONTHLY_LIMIT,
}

# Friendly names for error messages.
LABELS: dict[str, str] = {
    "google_places": "Google Places",
    "hunter": "Hunter.io",
}


class UsageLimitError(SourceError):
    """The monthly limit for a paid API has been reached."""


def current_period() -> str:
    """This month as "YYYY-MM" (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _limit(provider: str) -> int:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown usage provider '{provider}'")
    return PROVIDERS[provider]


def get_usage(provider: str) -> tuple[int, int]:
    """Return (requests used this month, monthly limit)."""
    limit = _limit(provider)
    with SessionLocal() as session:
        count = session.scalar(
            select(ApiUsage.count).where(ApiUsage.provider == provider, ApiUsage.period == current_period())
        )
    return (count or 0), limit


def consume(provider: str, n: int = 1) -> None:
    """Count `n` requests for `provider`. Raises UsageLimitError (a SourceError) instead
    if that would go over the monthly limit."""
    limit = _limit(provider)
    period = current_period()
    with SessionLocal() as session:
        # Make sure this month's row exists (another thread may create it at the same time).
        exists = session.scalar(
            select(ApiUsage.id).where(ApiUsage.provider == provider, ApiUsage.period == period)
        )
        if exists is None:
            session.add(ApiUsage(provider=provider, period=period, count=0))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()  # someone else created it first - fine

        # One UPDATE that only succeeds while under the limit, so two searches running
        # at once can never push the count past it.
        result = session.execute(
            update(ApiUsage)
            .where(
                ApiUsage.provider == provider,
                ApiUsage.period == period,
                ApiUsage.count + n <= limit,
            )
            .values(count=ApiUsage.count + n)
        )
        if result.rowcount == 0:
            session.rollback()
            used = session.scalar(
                select(ApiUsage.count).where(ApiUsage.provider == provider, ApiUsage.period == period)
            ) or 0
            label = LABELS.get(provider, provider)
            raise UsageLimitError(
                f"Monthly {label} limit reached ({used}/{limit}). It resets on the 1st of next month; "
                f"to allow more, raise the monthly limit in backend/.env (extra requests may cost money)."
            )
        session.commit()
