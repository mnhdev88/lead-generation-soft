"""Hunter.io "Domain Search": the email pattern and known people of a company domain.

Docs: https://hunter.io/api-documentation/v2#domain-search

    domain_search("acme.com") -> ("{first}.{last}", [HunterPerson("jane.doe@acme.com", "Jane", ...)])

- Optional backup: only used when HUNTER_API_KEY is set in backend/.env.
- Free plan = 25 searches a month and at most 10 emails per search. Every request is counted
  first with usage.consume("hunter"); at the monthly cap that raises usage.UsageLimitError.
- Any other problem (bad key, Hunter down, no internet) is logged and gives an empty result.
"""
import logging
import re
from dataclasses import dataclass

import httpx

from .. import config, usage

log = logging.getLogger("providers.hunter")

API_URL = "https://api.hunter.io/v2/domain-search"
USAGE_PROVIDER = "hunter"
TIMEOUT = 20  # seconds
MAX_LIMIT = 10  # free plan maximum per search (paid plans allow up to 100)

# Pattern made only of {first} {last} {f} {l} and . _ - (Hunter uses the same syntax as we do).
_PATTERN_RE = re.compile(r"^(?:\{(?:first|last|f|l)\}|[._-])+$")

# Tests set this to an httpx.MockTransport so no real network is used.
transport: httpx.BaseTransport | None = None


@dataclass
class HunterPerson:
    email: str
    first_name: str | None
    last_name: str | None
    position: str | None  # job title
    confidence: int | None  # Hunter's 0-100 score for the address
    linkedin_url: str | None
    email_type: str | None = None  # "personal" (a person) or "generic" (info@, sales@...)


def available() -> bool:
    """True when a Hunter API key is configured (read at call time)."""
    return bool(config.HUNTER_API_KEY)


def _clean(value) -> str | None:
    value = str(value).strip() if value is not None else ""
    return value or None


def _parse(data: dict) -> tuple[str | None, list[HunterPerson]]:
    pattern = _clean(data.get("pattern"))
    if pattern and not _PATTERN_RE.match(pattern.lower()):
        log.info("Ignoring unknown Hunter pattern %r", pattern)
        pattern = None
    people = []
    for item in data.get("emails") or []:
        email = _clean(item.get("value"))
        if not email or "@" not in email:
            continue
        confidence = item.get("confidence")
        people.append(HunterPerson(
            email=email.lower(),
            first_name=_clean(item.get("first_name")),
            last_name=_clean(item.get("last_name")),
            position=_clean(item.get("position")),
            confidence=int(confidence) if isinstance(confidence, (int, float)) else None,
            linkedin_url=_clean(item.get("linkedin")),
            email_type=_clean(item.get("type")),
        ))
    return (pattern.lower() if pattern else None), people


def domain_search(domain: str, limit: int = 10, *,
                  http_transport: httpx.BaseTransport | None = None) -> tuple[str | None, list[HunterPerson]]:
    """Ask Hunter for the email pattern and people of `domain`.
    Returns (pattern or None, people). Raises usage.UsageLimitError at the monthly cap."""
    if not available():
        log.info("Hunter.io skipped: no HUNTER_API_KEY")
        return None, []
    domain = (domain or "").strip().lower()
    if not domain:
        return None, []

    usage.consume(USAGE_PROVIDER)  # raises UsageLimitError when the monthly cap is reached
    params = {"domain": domain, "limit": max(1, min(limit, MAX_LIMIT))}
    # The key goes in a header (not the URL) so it never shows up in request logs.
    headers = {"User-Agent": config.USER_AGENT, "X-API-KEY": config.HUNTER_API_KEY}
    try:
        with httpx.Client(transport=http_transport or transport, timeout=TIMEOUT, headers=headers) as client:
            response = client.get(API_URL, params=params)
    except httpx.HTTPError as e:
        log.warning("Hunter.io request for %s failed: %s", domain, type(e).__name__)
        return None, []

    if response.status_code != 200:
        # e.g. 401 bad key, 429 Hunter's own monthly quota used up, 451 opted out, 5xx
        details = ""
        try:
            errors = response.json().get("errors") or []
            details = "; ".join(str(e.get("details") or e.get("id")) for e in errors)
        except (ValueError, AttributeError):
            pass
        log.warning("Hunter.io error for %s: HTTP %s %s", domain, response.status_code, details)
        return None, []
    try:
        data = response.json().get("data") or {}
    except (ValueError, AttributeError):
        log.warning("Hunter.io sent an unreadable answer for %s", domain)
        return None, []
    return _parse(data)
