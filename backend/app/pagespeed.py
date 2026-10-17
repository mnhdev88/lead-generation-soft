"""Google PageSpeed Insights (free): how fast and how SEO-friendly a website is on a phone.

Docs: https://developers.google.com/speed/docs/insights/v5/get-started

check(url) runs one mobile test and returns
    {"performance": 38, "seo": 82, "url": "https://...", "checked_at": "2026-10-17T12:00:00+00:00"}
or  {"error": "short reason", "url": "https://...", "checked_at": "..."}       (scores 0-100)
"url" is the address that was tested, so a result can be ignored once the website changes.
A slow website scores low on "performance" -> the business may need a new website; a low
"seo" score -> SEO work. Used by the intent score (app/intent.py).

The worker checks a few saved companies at a time while it is idle
(check_saved_companies). One test takes 10-60 seconds at Google.
- Get a free PAGESPEED_API_KEY (see backend/app/config.py). Without one, all callers share
  one small Google quota and almost every test is refused with "quota exceeded".
- The key goes in a request header, never in the web address, so it never shows up in logs.
- When Google says "too many requests" (quota) we stop and try again in a later slice; the
  companies stay unchecked. The same for a bad API key or no internet.
- A passing problem (timeout, a Google server error, an odd answer) is saved with a try
  counter, but the company stays unchecked: it is tried again after RETRY_AFTER_HOURS, and
  only after MAX_ATTEMPTS failed tries is the error kept for good. The slice stops at such an
  error, so a Google outage can't burn through many companies in a row.
- A problem with the website itself (Google could not load it: "Lighthouse returned error:
  FAILED_DOCUMENT_REQUEST", NO_FCP, ...) is saved for good, so it is not tested every slice.
- Facebook / Instagram / Yelp / Linktree ... pages are not the business's own website: they
  are marked "shared page" without asking Google (their speed is Facebook's, not the
  business's). Free builder sites (joes.wixsite.com) are the business's own, so they are tested.
Switch it off with PAGESPEED_ENABLED=0 in backend/.env.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import Company
from .utils import SHARED_DOMAINS, extract_domain

log = logging.getLogger("pagespeed")

API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
TIMEOUT = 60  # seconds; Google loads the whole website, which can take a while
ERROR_MAX = 120  # characters of an error message we keep

# Errors that are about us (quota, key, internet), not about the website: not saved, the
# company is tried again later, and check_saved_companies stops for now.
STOP_ERRORS = ("quota", "api_key", "network")
# Errors that may go away by themselves (Google trouble, a slow moment): tried again later.
# "server" = a Google 5xx answer that is not about the website.
RETRY_ERRORS = ("timeout", "server", "unexpected error", "unreadable answer", "no scores")
MAX_ATTEMPTS = 3  # after this many passing errors in a row the error is kept for good
RETRY_AFTER_HOURS = 6  # wait at least this long before trying such a company again
CANDIDATE_ROWS = 200  # companies looked at per slice (shared pages and waiting ones are skipped fast)
SHARED_PAGE = "shared page"  # the error saved for a Facebook / Yelp / ... page
# Google's 5xx message when the website itself failed to load, e.g.
# "Lighthouse returned error: FAILED_DOCUMENT_REQUEST. Lighthouse was unable to ..."
_LIGHTHOUSE_ERROR = re.compile(r"lighthouse returned error:?\s*([A-Za-z_]+)", re.I)

_warned_no_key = False  # the "add a key" warning is logged once per run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error(message: str, checked_at: datetime, detail: str | None = None, url: str | None = None) -> dict:
    out = {"error": message[:ERROR_MAX], "checked_at": checked_at.isoformat()}
    if detail:
        out["detail"] = detail[:ERROR_MAX]
    if isinstance(url, str) and url.strip():
        out["url"] = url.strip()
    return out


def _score(categories: dict, name: str) -> int | None:
    """Google gives 0..1 (e.g. 0.38) -> 38. None when the category is missing."""
    cat = categories.get(name)
    value = cat.get("score") if isinstance(cat, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, round(value * 100)))


def _google_error(r: httpx.Response) -> tuple[str, str]:
    """(our error code, Google's message) for a failed request."""
    try:
        err = r.json().get("error", {})
    except (ValueError, AttributeError):
        err = {}
    if not isinstance(err, dict):
        err = {}
    message = str(err.get("message") or r.reason_phrase or "").strip()
    reasons = " ".join(str(e.get("reason", "")) for e in err.get("errors") or [] if isinstance(e, dict))
    text = (message + " " + reasons + " " + str(err.get("status") or "")).lower()
    if r.status_code == 429 or "quota" in text or "ratelimit" in text or "resource_exhausted" in text:
        return "quota", message
    if "api key" in text or "api_key" in text or "keyinvalid" in text or r.status_code == 403:
        return "api_key", message
    if r.status_code >= 500:
        lighthouse = _LIGHTHOUSE_ERROR.search(message)
        if lighthouse:  # the website failed to load: a real result about the website
            return lighthouse.group(1).upper(), message
        return "server", message  # Google's own trouble: try again later
    return f"HTTP {r.status_code}", message


def is_web_url(url: str | None) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith(("http://", "https://")) and " " not in url.strip()


def is_shared_page(website: str | None, domain: str | None = None) -> bool:
    """True for a Facebook / Instagram / Yelp / Linktree ... page (also m.facebook.com).
    Free builder sites like joes.wixsite.com are the business's own site: False."""
    host = (domain or extract_domain(website) or "").lower()
    return bool(host) and any(host == d or host.endswith("." + d) for d in SHARED_DOMAINS)


# ----- one test ---------------------------------------------------------------

def check(url: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Run one mobile PageSpeed test. Never raises. Tests pass an httpx.MockTransport."""
    checked_at = _now()
    try:
        if not is_web_url(url):
            return _error("not a web address", checked_at)
        url = url.strip()
        params = [("url", url), ("strategy", "mobile"), ("category", "performance"), ("category", "seo")]
        headers = {"User-Agent": config.USER_AGENT}
        if config.PAGESPEED_API_KEY:
            # In a header, not "?key=": web addresses are written to the server logs.
            headers["X-Goog-Api-Key"] = config.PAGESPEED_API_KEY
        try:
            with httpx.Client(transport=transport, timeout=TIMEOUT, headers=headers) as client:
                r = client.get(API_URL, params=params)
        except httpx.TimeoutException:
            return _error("timeout", checked_at, url=url)
        except httpx.HTTPError as e:
            return _error("network", checked_at, type(e).__name__, url=url)
        if r.status_code != 200:
            code, message = _google_error(r)
            return _error(code, checked_at, message, url=url)
        try:
            data = r.json()
        except ValueError:
            return _error("unreadable answer", checked_at, url=url)
        lighthouse = data.get("lighthouseResult") if isinstance(data, dict) else None
        categories = lighthouse.get("categories") if isinstance(lighthouse, dict) else None
        if not isinstance(categories, dict):
            return _error("no scores", checked_at, url=url)
        performance, seo = _score(categories, "performance"), _score(categories, "seo")
        if performance is None and seo is None:
            runtime = lighthouse.get("runtimeError") if isinstance(lighthouse, dict) else None
            reason = runtime.get("code") if isinstance(runtime, dict) else None
            return _error(str(reason or "no scores"), checked_at, url=url)
        return {"performance": performance, "seo": seo, "url": url, "checked_at": checked_at.isoformat()}
    except Exception as e:  # never let one odd answer stop the worker
        log.warning("PageSpeed check of %s failed: %r", url, e)
        return _error("unexpected error", checked_at, type(e).__name__, url=url if isinstance(url, str) else None)


# ----- saving on companies -----------------------------------------------------

def _parse_time(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _waiting_to_retry(company: Company) -> bool:
    """True when the last try ended in a passing error less than RETRY_AFTER_HOURS ago."""
    prev = company.pagespeed if isinstance(company.pagespeed, dict) else {}
    if prev.get("error") not in RETRY_ERRORS:
        return False
    tried = _parse_time(prev.get("checked_at"))
    return tried is not None and _now() - tried < timedelta(hours=RETRY_AFTER_HOURS)


def _save(company: Company, result: dict) -> bool:
    """Store a result on the company. True when it is final (pagespeed_checked_at set).
    - a "stop" error (quota, key, internet): nothing stored;
    - a passing error: stored with a try counter ("attempts"); the company stays unchecked
      until the MAX_ATTEMPTS-th try. Good scores we already have are never replaced by one;
    - anything else (scores or a website error): stored for good."""
    error = result.get("error")
    if error in STOP_ERRORS:
        return False
    prev = company.pagespeed if isinstance(company.pagespeed, dict) else {}
    if error in RETRY_ERRORS:
        if prev and not prev.get("error"):
            return False  # keep the good scores we already have
        before = int(prev.get("attempts") or 0) if prev.get("error") in RETRY_ERRORS else 0
        company.pagespeed = {**result, "attempts": before + 1}  # a new dict, so SQLAlchemy sees it
        if before + 1 < MAX_ATTEMPTS:
            return False
    else:
        company.pagespeed = dict(result)
    company.pagespeed_checked_at = _now()
    return True


def _mark_shared_page(company: Company) -> None:
    company.pagespeed = _error(SHARED_PAGE, _now(), url=company.website)
    company.pagespeed_checked_at = _now()


def _log_stop(result: dict) -> None:
    """Log why PageSpeed paused; the first quota stop without a key as a clear warning."""
    global _warned_no_key
    if result.get("error") == "quota" and not config.PAGESPEED_API_KEY and not _warned_no_key:
        _warned_no_key = True
        log.warning("Google PageSpeed refused the test (quota exceeded) and PAGESPEED_API_KEY is empty. "
                    "Add a free key to backend/.env (how: see backend/app/config.py) to get speed scores.")
        return
    log.info("PageSpeed paused (%s): %s", result["error"], result.get("detail", ""))


def apply_pagespeed(company: Company, transport: httpx.BaseTransport | None = None) -> bool:
    """Test the company's website and store the result (see _save). Returns True when a
    final result was stored. Does nothing without a website or when switched off. A
    Facebook / Yelp / ... page is marked "shared page" without asking Google."""
    if not config.PAGESPEED_ENABLED or not company.website:
        return False
    if is_shared_page(company.website, company.domain):
        _mark_shared_page(company)
        return True
    return _save(company, check(company.website, transport))


def check_saved_companies(session: Session, limit: int = 5,
                          transport: httpx.BaseTransport | None = None) -> int:
    """Test up to `limit` saved companies that have a website and no PageSpeed result yet,
    oldest first, rescoring and committing each one.
    - Facebook / Yelp / ... pages are marked "shared page" on the way, without a test and
      without using up the limit.
    - Companies whose last try failed less than RETRY_AFTER_HOURS ago are skipped.
    - Stops early on a quota / key / internet problem and on a passing error (timeout,
      Google server error), so the worker waits before the next slice.
    Returns how many final results were stored. Never raises."""
    from .scoring import rescore  # here, not at the top: scoring may import modules that import us

    if not config.PAGESPEED_ENABLED or limit <= 0:
        return 0
    checked = 0
    tests = 0
    try:
        companies = session.scalars(
            select(Company)
            .where(Company.website.is_not(None), Company.website != "", Company.pagespeed_checked_at.is_(None))
            .order_by(Company.created_at, Company.id)
            .limit(max(limit, CANDIDATE_ROWS))
        ).all()
        for company in companies:
            if is_shared_page(company.website, company.domain):
                _mark_shared_page(company)  # no Google call, so it doesn't count toward `limit`
                session.commit()
                checked += 1
                continue
            if _waiting_to_retry(company):
                continue
            if tests >= limit:
                break
            tests += 1
            result = check(company.website, transport)
            if result.get("error") in STOP_ERRORS:
                _log_stop(result)
                break
            if _save(company, result):
                try:
                    rescore(company)
                except Exception as e:  # the PageSpeed result is still worth keeping
                    log.warning("Rescoring company %s after PageSpeed failed: %r", company.id, e)
                checked += 1
            session.commit()
            if result.get("error") in RETRY_ERRORS:
                log.info("PageSpeed paused (%s for %s)", result["error"], company.website)
                break  # Google may be in trouble: wait for the next slice
    except Exception as e:
        log.warning("PageSpeed back-fill failed: %r", e)
        try:
            session.rollback()
        except Exception:
            pass
    return checked
