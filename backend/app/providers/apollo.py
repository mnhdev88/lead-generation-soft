"""Apollo.io: a backup for finding a business's owner / decision-maker and their email.

Docs: https://docs.apollo.io/reference/people-api-search and .../people-enrichment

    find_decision_makers("smiledental.com")
        -> [{"full_name": "Jane Doe", "title": "Owner", "email": "jane@smiledental.com",
             "email_status": "verified", "linkedin_url": "...", "apollo_id": "64a..."}]

How it works (two steps, to spend as few credits as possible):
  1. People search at the company's domain, owners / founders / C-suite / partners / directors /
     managers only. FREE (0 credits), but gives no emails and only part of the last name.
  2. For the best `limit` people: "People Enrichment" by Apollo's id. 1 credit each when Apollo
     has data. Counted first with usage.consume("apollo"). Phone numbers are never asked for
     (they cost 8 credits and need a webhook).

- Only used when APOLLO_API_KEY is set in backend/.env.
- Problems raise SourceError with a message a person can act on; the monthly cap raises
  usage.UsageLimitError. When something goes wrong after some people were already paid for,
  those people are returned (the credits are not wasted).
"""
import logging

import httpx

from .. import config, usage
from ..people import classify
from ..people.linkedin import profile_url
from ..sources.base import SourceError
from ..usage import UsageLimitError
from ..utils import own_domain

log = logging.getLogger("providers.apollo")

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"
USAGE_PROVIDER = "apollo"
TIMEOUT = 30  # seconds
PER_PAGE = 10  # people looked at per company in the free search

# Most senior first. The search asks only for these.
SENIORITIES = ["owner", "founder", "c_suite", "partner", "director", "manager"]
# Our own job-title levels (app/people/seniority.py), for people Apollo gives no seniority.
_OUR_LEVELS = {"owner": "owner", "c_level": "c_suite", "director": "director", "manager": "manager"}

# Tests set this to an httpx.MockTransport so no real network is used.
mock_transport: httpx.BaseTransport | None = None


def available() -> bool:
    """True when an Apollo API key is configured (read at call time)."""
    return bool(config.APOLLO_API_KEY)


def _clean(value) -> str | None:
    value = " ".join(str(value).split()) if value is not None else ""
    return value or None


def _error_message(status: int) -> str:
    if status == 401:
        return "Apollo.io refused the API key (HTTP 401). Check APOLLO_API_KEY in backend/.env."
    if status == 403:
        return ("Apollo.io says this key or plan can't use the people API (HTTP 403). "
                "Use a master API key, or check your Apollo plan.")
    if status == 422:
        return "Apollo.io could not use the request (HTTP 422) - often no credits left on the Apollo account."
    if status == 429:
        return "Apollo.io says too many requests (HTTP 429). Wait a while and try again."
    return f"Apollo.io is not answering properly (HTTP {status}). Try again later."


def _post(client: httpx.Client, url: str, params: dict) -> dict:
    try:
        response = client.post(url, params=params)
    except httpx.HTTPError as e:
        log.warning("Apollo.io request failed: %s", type(e).__name__)
        raise SourceError(f"Could not reach Apollo.io ({type(e).__name__}). Check the internet connection.") from e
    if response.status_code != 200:
        log.warning("Apollo.io error: HTTP %s %s", response.status_code, response.text[:200])
        raise SourceError(_error_message(response.status_code))
    try:
        data = response.json()
    except ValueError as e:
        raise SourceError("Apollo.io sent an unreadable answer.") from e
    if not isinstance(data, dict):
        raise SourceError("Apollo.io sent an unreadable answer.")
    return data


def _rank(person: dict) -> tuple[int, int]:
    """Sort key: people Apollo has an email for first, then the most senior."""
    level = person.get("seniority")
    if level not in SENIORITIES:
        level = _OUR_LEVELS.get(classify(person.get("title")).seniority)
    seniority = SENIORITIES.index(level) if level in SENIORITIES else len(SENIORITIES)
    return (0 if person.get("has_email") else 1), seniority


def _picks(data: dict, limit: int) -> list[dict]:
    people = [p for p in data.get("people") or [] if isinstance(p, dict) and p.get("id")]
    return sorted(people, key=_rank)[:max(0, limit)]


def _person(data: dict, apollo_id: str) -> dict | None:
    p = data.get("person")
    if not isinstance(p, dict):
        return None
    name = _clean(p.get("name")) or _clean(f"{p.get('first_name') or ''} {p.get('last_name') or ''}")
    if not name:
        return None
    email = _clean(p.get("email"))
    email = email.lower() if email and "@" in email else None
    linkedin = _clean(p.get("linkedin_url"))
    if linkedin:
        linkedin = profile_url(linkedin.replace("http://", "https://", 1)) or linkedin
    return {
        "full_name": name,
        "title": _clean(p.get("title")),
        "email": email,
        "email_status": _clean(p.get("email_status")) if email else None,
        "linkedin_url": linkedin,
        "apollo_id": str(p.get("id") or apollo_id),
    }


def find_decision_makers(domain: str, limit: int = 2,
                         transport: httpx.BaseTransport | None = None) -> list[dict]:
    """The owner / decision-makers Apollo knows at `domain` (at most `limit`, each costing
    up to 1 credit). Returns [{"full_name", "title", "email", "email_status", "linkedin_url",
    "apollo_id"}]. Raises SourceError / UsageLimitError (see the module notes)."""
    if not available():
        log.info("Apollo.io skipped: no APOLLO_API_KEY")
        return []
    domain = own_domain((domain or "").strip().lower())
    if not domain:  # a Facebook page or a free website builder: Apollo would find the wrong people
        return []

    # The key goes in a header (not the URL) so it never shows up in request logs.
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json",
               "Cache-Control": "no-cache", "x-api-key": config.APOLLO_API_KEY}
    found: list[dict] = []
    with httpx.Client(transport=transport or mock_transport, timeout=TIMEOUT, headers=headers) as client:
        # 1. Free search: who works there in a senior role?
        search = _post(client, SEARCH_URL, {
            "q_organization_domains_list[]": [domain],
            "person_seniorities[]": SENIORITIES,
            "per_page": PER_PAGE,
            "page": 1,
        })
        # 2. One paid lookup per chosen person.
        for pick in _picks(search, limit):
            try:
                usage.consume(USAGE_PROVIDER)  # raises UsageLimitError at the monthly cap
                data = _post(client, MATCH_URL, {"id": pick["id"], "reveal_personal_emails": "false"})
            except SourceError:  # also UsageLimitError
                if found:  # keep the people already paid for; the next company sees the error
                    log.warning("Apollo.io stopped after %d people at %s", len(found), domain, exc_info=True)
                    return found
                raise
            person = _person(data, pick["id"])
            if person:
                found.append(person)
    return found


__all__ = ["available", "find_decision_makers", "UsageLimitError"]
