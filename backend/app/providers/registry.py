"""Official business registers via OpenCorporates: who is registered as running a business.

Docs: https://api.opencorporates.com/documentation/API-Reference

    find_officers(company) -> [Officer("Jane Doe", "Managing Member", "https://opencorporates.com/...")]

An independent source for the owner: when the register names the same person as the website
or LinkedIn, the owner is confirmed (app/worker.py keeps Contact.confirmed_by).

- Optional: only used when OPENCORPORATES_API_TOKEN is set in backend/.env.
- Two requests per business (search, then the company's officers), each counted first with
  usage.consume("opencorporates"); the monthly cap raises usage.UsageLimitError.
- Only where we can name the register: US businesses with a state in their address (each
  state has its own register), and the UK, Ireland and New Zealand.
- Registered agents (a service that receives the mail) and officers who left are skipped.
- The token goes in the URL (OpenCorporates' way), so URLs are never logged here.
"""
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx

from .. import config, usage
from ..sources.base import SourceError

log = logging.getLogger("providers.registry")

SEARCH_URL = "https://api.opencorporates.com/v0.4/companies/search"
COMPANY_URL = "https://api.opencorporates.com/v0.4/companies/{jurisdiction}/{number}"
USAGE_PROVIDER = "opencorporates"
TIMEOUT = 20
SAME_NAME_RATIO = 0.9  # "Smile Dental" vs "SMILE DENTAL PLLC" after legal words are dropped
MAX_OFFICERS = 5

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}
COUNTRY_REGISTERS = {
    "gb": "gb", "uk": "gb", "united kingdom": "gb", "england": "gb", "scotland": "gb", "wales": "gb",
    "ie": "ie", "ireland": "ie", "nz": "nz", "new zealand": "nz",
}
_STATE_ZIP_RE = re.compile(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
_NOT_A_PERSON_RE = re.compile(r"\b(?:llc|inc|corp|corporation|ltd|limited|company|services|agents?|trust|"
                              r"registered|holdings|partners|group|lp|llp|pllc|pc)\b", re.I)

# Tests set this to an httpx.MockTransport so no real network is used.
mock_transport: httpx.BaseTransport | None = None


@dataclass
class Officer:
    full_name: str
    position: str | None
    source_url: str | None  # the business's page on opencorporates.com


def available() -> bool:
    return bool(config.OPENCORPORATES_API_TOKEN)


def jurisdiction(company) -> str | None:
    """'us_tx', 'gb'... or None when we can't tell which register to ask."""
    country = (company.country or "").strip().lower()
    if country in COUNTRY_REGISTERS:
        return COUNTRY_REGISTERS[country]
    if country in ("", "us", "usa", "united states", "united states of america"):
        match = _STATE_ZIP_RE.search(company.address or "")
        if match and match.group(1) in US_STATES:
            return f"us_{match.group(1).lower()}"
    return None


def person_name(raw: str | None) -> str | None:
    """'SMITH, JOHN A' -> 'John A Smith'; companies (an LLC as a member) -> None."""
    raw = " ".join((raw or "").split())
    if not raw or _NOT_A_PERSON_RE.search(raw):
        return None
    if "," in raw:
        last, _, first = raw.partition(",")
        raw = f"{first.strip()} {last.strip()}"
    words = raw.split()
    if len(words) < 2:
        return None
    return " ".join(w if any(c.islower() for c in w) else w.capitalize() for w in words)


def _same_business(a: str, b: str) -> bool:
    from ..ppp import name_key  # drops LLC, PLLC, Inc...

    ka, kb = name_key(a), name_key(b)
    return bool(ka and kb) and (ka == kb or SequenceMatcher(None, ka, kb).ratio() >= SAME_NAME_RATIO)


def _get(client: httpx.Client, url: str, params: dict) -> dict:
    usage.consume(USAGE_PROVIDER)
    try:
        response = client.get(url, params={**params, "api_token": config.OPENCORPORATES_API_TOKEN})
    except httpx.HTTPError as e:
        raise SourceError(f"Couldn't reach OpenCorporates ({type(e).__name__})") from None
    if response.status_code in (401, 403):
        raise SourceError("OpenCorporates refused the token (check OPENCORPORATES_API_TOKEN)")
    if response.status_code == 429:
        raise SourceError("OpenCorporates' own limit is used up for now")
    if response.status_code == 404:
        return {}
    if response.status_code != 200:
        raise SourceError(f"OpenCorporates error (HTTP {response.status_code})")
    try:
        return response.json().get("results") or {}
    except (ValueError, AttributeError):
        raise SourceError("OpenCorporates sent an unreadable answer") from None


def find_officers(company, *, http_transport: httpx.BaseTransport | None = None) -> list[Officer]:
    """The people the register lists as running this business ([] when it isn't found)."""
    if not available():
        return []
    code = jurisdiction(company)
    if not code or not company.name:
        return []
    with httpx.Client(transport=http_transport or mock_transport, timeout=TIMEOUT,
                      headers={"User-Agent": config.USER_AGENT}) as client:
        found = _get(client, SEARCH_URL, {"q": company.name, "jurisdiction_code": code, "per_page": 5})
        candidates = [item.get("company") or {} for item in found.get("companies") or []]
        matches = [c for c in candidates if c.get("name") and _same_business(c["name"], company.name)]
        if not matches:
            return []
        best = sorted(matches, key=lambda c: bool(c.get("inactive")))[0]  # an active one first
        detail = _get(client, COMPANY_URL.format(jurisdiction=best.get("jurisdiction_code") or code,
                                                 number=best.get("company_number")), {})
    record = detail.get("company") or {}
    url = record.get("opencorporates_url") or best.get("opencorporates_url")
    officers: list[Officer] = []
    for item in record.get("officers") or []:
        o = item.get("officer") or {}
        position = " ".join(str(o.get("position") or "").split()) or None
        if o.get("inactive") or o.get("end_date") or (position and "agent" in position.lower()):
            continue
        name = person_name(o.get("name"))
        if name and all(name != x.full_name for x in officers):
            officers.append(Officer(name, position.title() if position and position.isupper() else position, url))
    return officers[:MAX_OFFICERS]
