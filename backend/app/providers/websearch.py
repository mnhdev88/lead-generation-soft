"""Web search API (Brave or Serper.dev): used to find LinkedIn profiles of business owners
without ever opening linkedin.com ourselves.

    search('site:linkedin.com/in "Smile Dental" owner')
        -> [{"title": "Jane Doe - Owner - Smile Dental | LinkedIn",
             "url": "https://www.linkedin.com/in/janedoe", "snippet": "Owner at Smile Dental ..."}]

- Which service: WEB_SEARCH_PROVIDER in backend/.env ("brave" or "serper"), with its key
  (BRAVE_API_KEY or SERPER_API_KEY). See .env.example for prices.
- Every request is counted first with usage.consume("web_search"); at the monthly cap that
  raises usage.UsageLimitError and nothing is sent.
- Any other problem (bad key, service down, no internet) raises SourceError with a message
  a person can act on.
"""
import html
import logging
import re

import httpx

from .. import config, usage
from ..sources.base import SourceError

log = logging.getLogger("providers.websearch")

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SERPER_URL = "https://google.serper.dev/search"
USAGE_PROVIDER = "web_search"
TIMEOUT = 20  # seconds
MAX_COUNT = 20  # Brave gives at most 20 results per request
LABELS = {"brave": "Brave Search", "serper": "Serper.dev"}
KEY_NAMES = {"brave": "BRAVE_API_KEY", "serper": "SERPER_API_KEY"}

# Tests set this to an httpx.MockTransport so no real network is used.
mock_transport: httpx.BaseTransport | None = None

_TAG_RE = re.compile(r"<[^>]+>")


def _provider() -> str:
    return (config.WEB_SEARCH_PROVIDER or "brave").strip().lower()


def _key() -> str:
    provider = _provider()
    if provider == "brave":
        return config.BRAVE_API_KEY
    if provider == "serper":
        return config.SERPER_API_KEY
    return ""


def available() -> bool:
    """True when the chosen provider has a key (read at call time)."""
    return bool(_key())


def _text(value) -> str:
    """'Owner at <strong>Smile</strong> &amp; Co' -> 'Owner at Smile & Co'"""
    text = html.unescape(_TAG_RE.sub("", str(value or "")))
    return " ".join(text.split())


def _results(items, url_key: str, snippet_key: str) -> list[dict]:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get(url_key) or "").strip()
        if not url:
            continue
        out.append({"title": _text(item.get("title")), "url": url, "snippet": _text(item.get(snippet_key))})
    return out


def _error_message(label: str, key_name: str, status: int) -> str:
    if status in (401, 403):
        return f"{label} refused the API key (HTTP {status}). Check {key_name} in backend/.env."
    if status == 402:
        return f"{label} says the account has no search credit left (HTTP 402). Check your {label} plan."
    if status == 422:
        return f"{label} could not read the search request (HTTP 422)."
    if status == 429:
        return f"{label} says too many searches (HTTP 429). Wait a while and try again, or check your plan's limits."
    return f"{label} is not answering properly (HTTP {status}). Try again later."


def search(query: str, count: int = 10, transport: httpx.BaseTransport | None = None) -> list[dict]:
    """Run one web search. Returns [{"title", "url", "snippet"}] (plain text, no HTML).
    Raises UsageLimitError at the monthly cap and SourceError for any other problem."""
    provider = _provider()
    label = LABELS.get(provider, provider)
    if provider not in LABELS:
        raise SourceError(f"Unknown WEB_SEARCH_PROVIDER '{provider}' in backend/.env: use brave or serper.")
    key_name = KEY_NAMES[provider]
    if not available():
        raise SourceError(f"No web search key: add {key_name} to backend/.env and restart the backend.")
    query = " ".join((query or "").split())
    if not query:
        return []
    count = max(1, min(int(count), MAX_COUNT))

    usage.consume(USAGE_PROVIDER)  # raises UsageLimitError when the monthly cap is reached
    # The key goes in a header (not the URL) so it never shows up in request logs.
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    try:
        with httpx.Client(transport=transport or mock_transport, timeout=TIMEOUT) as client:
            if provider == "brave":
                headers["X-Subscription-Token"] = _key()
                response = client.get(BRAVE_URL, params={"q": query, "count": count}, headers=headers)
            else:
                headers["X-API-KEY"] = _key()
                response = client.post(SERPER_URL, json={"q": query, "num": count}, headers=headers)
    except httpx.HTTPError as e:
        log.warning("%s request failed: %s", label, type(e).__name__)
        raise SourceError(f"Could not reach {label} ({type(e).__name__}). Check the internet connection.") from e

    if response.status_code != 200:
        log.warning("%s error: HTTP %s %s", label, response.status_code, response.text[:200])
        raise SourceError(_error_message(label, key_name, response.status_code))
    try:
        data = response.json()
    except ValueError as e:
        raise SourceError(f"{label} sent an unreadable answer.") from e
    if not isinstance(data, dict):
        raise SourceError(f"{label} sent an unreadable answer.")

    if provider == "brave":
        web = data.get("web") if isinstance(data.get("web"), dict) else {}
        return _results(web.get("results"), "url", "description")[:count]
    return _results(data.get("organic"), "link", "snippet")[:count]
