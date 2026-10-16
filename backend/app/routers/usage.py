from fastapi import APIRouter

from .. import config, usage
from ..providers import hunter
from ..schemas import UsageOut

router = APIRouter(prefix="/api", tags=["usage"])


@router.get("/usage", response_model=list[UsageOut])
def list_usage():
    """How many paid API requests were used this month, per provider."""
    period = usage.current_period()
    out = []
    configured = {"google_places": bool(config.GOOGLE_PLACES_API_KEY), "hunter": hunter.available()}
    for provider in usage.PROVIDERS:
        count, limit = usage.get_usage(provider)
        out.append(UsageOut(provider=provider, period=period, count=count, limit=limit,
                            configured=configured.get(provider, True)))
    return out
