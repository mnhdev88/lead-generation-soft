"""Tests for app/pagespeed.py. No real network: Google is faked with httpx.MockTransport."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import config, pagespeed
from app.models import Company


def psi_answer(performance=0.38, seo=0.82) -> dict:
    return {"lighthouseResult": {"categories": {
        "performance": {"id": "performance", "score": performance},
        "seo": {"id": "seo", "score": seo},
    }}}


class FakePSI:
    """Answers PageSpeed requests with the given (status, json) pairs in turn (the last repeats)."""

    def __init__(self, *answers):
        self.answers = list(answers) or [(200, psi_answer())]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(pagespeed.API_URL)
        self.requests.append(request)
        status, body = self.answers[min(len(self.requests) - 1, len(self.answers) - 1)]
        if isinstance(body, Exception):
            raise body
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    @property
    def transport(self):
        return httpx.MockTransport(self)


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(config, "PAGESPEED_ENABLED", True)
    monkeypatch.setattr(config, "PAGESPEED_API_KEY", "")


# ----- check() -----------------------------------------------------------------

def test_check_scores_and_request(monkeypatch):
    fake = FakePSI()
    out = pagespeed.check("https://smile.example.com/", fake.transport)
    assert out["performance"] == 38 and out["seo"] == 82
    assert datetime.fromisoformat(out["checked_at"]).tzinfo is not None
    params = fake.requests[0].url.params
    assert params["url"] == "https://smile.example.com/"
    assert params["strategy"] == "mobile"
    assert params.get_list("category") == ["performance", "seo"]
    assert "key" not in params
    assert "x-goog-api-key" not in fake.requests[0].headers
    assert out["url"] == "https://smile.example.com/"

    monkeypatch.setattr(config, "PAGESPEED_API_KEY", "psi-key")
    pagespeed.check("http://smile.example.com", fake.transport)
    # Regression: the key goes in a header, never in the web address (which is logged).
    assert fake.requests[1].headers["x-goog-api-key"] == "psi-key"
    assert "key" not in fake.requests[1].url.params
    assert "psi-key" not in str(fake.requests[1].url)


def test_check_rounds_and_handles_missing_category():
    out = pagespeed.check("https://x.example.com", FakePSI((200, psi_answer(0.996, None))).transport)
    assert out["performance"] == 100 and out["seo"] is None


@pytest.mark.parametrize("url", [None, "", "ftp://x.example.com", "www.example.com", "mailto:a@b.com",
                                 "https://exa mple.com"])
def test_only_web_addresses(url):
    fake = FakePSI()
    out = pagespeed.check(url, fake.transport)
    assert out["error"] == "not a web address"
    assert fake.requests == []


@pytest.mark.parametrize("status, body, error", [
    (429, {"error": {"code": 429, "message": "Quota exceeded for quota metric 'Queries'",
                     "status": "RESOURCE_EXHAUSTED"}}, "quota"),
    (403, {"error": {"code": 403, "message": "Daily Limit Exceeded",
                     "errors": [{"reason": "dailyLimitExceeded"}, {"reason": "quotaExceeded"}]}}, "quota"),
    (400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}, "api_key"),
    (500, {"error": {"code": 500, "message": "Lighthouse returned error: FAILED_DOCUMENT_REQUEST. Lighthouse "
                                             "was unable to reliably load the page you requested."}},
     "FAILED_DOCUMENT_REQUEST"),
    (500, {"error": {"code": 500, "message": "Internal error encountered.", "status": "INTERNAL"}}, "server"),
    (503, {"error": {"code": 503, "message": "Backend Error", "status": "UNAVAILABLE"}}, "server"),
    (500, "<html>oops</html>", "server"),
    (400, {"error": {"code": 400, "message": "Invalid value"}}, "HTTP 400"),
    (200, "not json", "unreadable answer"),
    (200, {"kind": "pagespeedonline#result"}, "no scores"),
    (200, {"lighthouseResult": {"categories": {}, "runtimeError": {"code": "NO_FCP"}}}, "NO_FCP"),
    (200, [1, 2], "no scores"),
])
def test_check_errors(status, body, error):
    out = pagespeed.check("https://x.example.com", FakePSI((status, body)).transport)
    assert out["error"] == error
    assert "performance" not in out and "checked_at" in out


def test_check_timeout_and_network_errors():
    req = httpx.Request("GET", pagespeed.API_URL)
    out = pagespeed.check("https://x.example.com", FakePSI((0, httpx.ReadTimeout("slow", request=req))).transport)
    assert out["error"] == "timeout"
    out = pagespeed.check("https://x.example.com", FakePSI((0, httpx.ConnectError("down", request=req))).transport)
    assert out["error"] == "network"
    out = pagespeed.check("https://x.example.com", FakePSI((0, RuntimeError("weird"))).transport)
    assert out["error"] == "unexpected error"


# ----- saving on companies ------------------------------------------------------

def company(i, website="https://site{i}.example.com", age_days=0) -> Company:
    return Company(name=f"Clinic {i}", source="google", source_ref=f"p{i}", raw={},
                   website=website.format(i=i) if website else None,
                   created_at=datetime.now(timezone.utc) - timedelta(days=age_days))


def test_apply_pagespeed():
    c = company(1)
    assert pagespeed.apply_pagespeed(c, FakePSI().transport) is True
    assert c.pagespeed["performance"] == 38 and c.pagespeed_checked_at is not None

    c = company(2, website=None)
    assert pagespeed.apply_pagespeed(c, FakePSI().transport) is False
    assert c.pagespeed_checked_at is None

    c = company(3)
    assert pagespeed.apply_pagespeed(c, FakePSI((429, {"error": {"code": 429}})).transport) is False
    assert c.pagespeed_checked_at is None  # quota: try again later

    c = company(4)
    lighthouse = (500, {"error": {"code": 500, "message": "Lighthouse returned error: NO_FCP."}})
    assert pagespeed.apply_pagespeed(c, FakePSI(lighthouse).transport) is True
    assert c.pagespeed["error"] == "NO_FCP"  # the website's own problem: stored for good

    c = company(5)
    assert pagespeed.apply_pagespeed(c, FakePSI((503, {"error": {"code": 503}})).transport) is False
    assert c.pagespeed["error"] == "server" and c.pagespeed["attempts"] == 1
    assert c.pagespeed_checked_at is None  # Google's trouble: tried again later


def test_passing_errors_kept_only_after_max_attempts():
    # Regression: a timeout or Google 5xx used to be saved for good on the first try.
    req = httpx.Request("GET", pagespeed.API_URL)
    c = company(1)
    for attempt in range(1, pagespeed.MAX_ATTEMPTS):
        fake = FakePSI((0, httpx.ReadTimeout("slow", request=req)))
        assert pagespeed.apply_pagespeed(c, fake.transport) is False
        assert c.pagespeed["attempts"] == attempt and c.pagespeed_checked_at is None
    assert pagespeed.apply_pagespeed(c, FakePSI((503, {"error": {}})).transport) is True
    assert c.pagespeed["error"] == "server" and c.pagespeed["attempts"] == pagespeed.MAX_ATTEMPTS
    assert c.pagespeed_checked_at is not None  # given up for good

    # A passing error never replaces good scores we already have.
    c = company(2)
    c.pagespeed = {"performance": 70, "seo": 90}
    assert pagespeed.apply_pagespeed(c, FakePSI((503, {"error": {}})).transport) is False
    assert c.pagespeed == {"performance": 70, "seo": 90}


@pytest.mark.parametrize("website, shared", [
    ("https://www.facebook.com/joesplumbing", True),
    ("https://m.facebook.com/joesplumbing", True),
    ("https://linktr.ee/joes", True),
    ("https://www.yelp.com/biz/joes-plumbing", True),
    ("https://joes.wixsite.com/plumbing", False),  # a free builder site is still their own
    ("https://joesplumbing.com", False),
    ("https://notfacebook.com", False),
])
def test_is_shared_page(website, shared):
    assert pagespeed.is_shared_page(website) is shared


def test_apply_pagespeed_skips_shared_pages():
    fake = FakePSI()
    c = company(1, website="https://www.facebook.com/joesplumbing")
    assert pagespeed.apply_pagespeed(c, fake.transport) is True
    assert fake.requests == []  # Facebook's speed is not the business's speed
    assert c.pagespeed["error"] == pagespeed.SHARED_PAGE and c.pagespeed_checked_at is not None


def test_apply_pagespeed_switched_off(monkeypatch):
    monkeypatch.setattr(config, "PAGESPEED_ENABLED", False)
    fake = FakePSI()
    c = company(1)
    assert pagespeed.apply_pagespeed(c, fake.transport) is False
    assert fake.requests == []


def test_check_saved_companies(db):
    db.add_all([company(1, age_days=1), company(2, age_days=5), company(3, website=None, age_days=9),
                company(4, website="", age_days=8), company(5, age_days=3)])
    done = company(6, age_days=10)
    done.pagespeed, done.pagespeed_checked_at = {"performance": 90}, datetime.now(timezone.utc)
    db.add(done)
    db.commit()

    fake = FakePSI()
    assert pagespeed.check_saved_companies(db, limit=2, transport=fake.transport) == 2
    # Oldest first: Clinic 2 (5 days), then Clinic 5 (3 days).
    assert [r.url.params["url"] for r in fake.requests] == ["https://site2.example.com", "https://site5.example.com"]
    db.expire_all()
    c2 = db.query(Company).filter_by(name="Clinic 2").one()
    assert c2.pagespeed["seo"] == 82 and c2.pagespeed_checked_at is not None
    assert c2.score is not None  # rescored

    assert pagespeed.check_saved_companies(db, limit=5, transport=fake.transport) == 1  # only Clinic 1 left
    assert pagespeed.check_saved_companies(db, limit=5, transport=fake.transport) == 0
    assert len(fake.requests) == 3
    assert db.query(Company).filter_by(name="Clinic 6").one().pagespeed == {"performance": 90}


def test_check_saved_companies_stops_on_quota(db):
    db.add_all([company(i, age_days=10 - i) for i in range(1, 5)])
    db.commit()
    lighthouse = (500, {"error": {"code": 500, "message": "Lighthouse returned error: FAILED_DOCUMENT_REQUEST."}})
    fake = FakePSI((200, psi_answer()), lighthouse, (429, {"error": {"code": 429}}))
    assert pagespeed.check_saved_companies(db, limit=5, transport=fake.transport) == 2
    assert len(fake.requests) == 3  # stopped after the quota answer
    db.expire_all()
    by_name = {c.name: c for c in db.query(Company)}
    assert by_name["Clinic 1"].pagespeed["performance"] == 38
    assert by_name["Clinic 2"].pagespeed["error"] == "FAILED_DOCUMENT_REQUEST"
    assert by_name["Clinic 3"].pagespeed_checked_at is None  # quota: tried again later
    assert by_name["Clinic 4"].pagespeed_checked_at is None


def test_check_saved_companies_pauses_on_google_trouble(db):
    # Regression: during a Google outage every company tested got a permanent "HTTP 503".
    db.add_all([company(i, age_days=10 - i) for i in range(1, 5)])
    db.commit()
    fake = FakePSI((503, {"error": {"code": 503, "message": "Backend Error"}}))
    assert pagespeed.check_saved_companies(db, limit=5, transport=fake.transport) == 0  # not busy
    assert len(fake.requests) == 1  # stopped at the first passing error
    db.expire_all()
    c1 = db.query(Company).filter_by(name="Clinic 1").one()
    assert c1.pagespeed["error"] == "server" and c1.pagespeed["attempts"] == 1
    assert c1.pagespeed_checked_at is None  # still to be checked

    # The next slice skips Clinic 1 (tried less than RETRY_AFTER_HOURS ago) and tests Clinic 2.
    good = FakePSI()
    assert pagespeed.check_saved_companies(db, limit=1, transport=good.transport) == 1
    assert [r.url.params["url"] for r in good.requests] == ["https://site2.example.com"]

    # After RETRY_AFTER_HOURS Clinic 1 is tried again (it is the oldest).
    db.expire_all()
    c1 = db.query(Company).filter_by(name="Clinic 1").one()
    old = datetime.now(timezone.utc) - timedelta(hours=pagespeed.RETRY_AFTER_HOURS + 1)
    c1.pagespeed = {**c1.pagespeed, "checked_at": old.isoformat()}
    db.commit()
    assert pagespeed.check_saved_companies(db, limit=1, transport=good.transport) == 1
    assert good.requests[-1].url.params["url"] == "https://site1.example.com"
    db.expire_all()
    c1 = db.query(Company).filter_by(name="Clinic 1").one()
    assert c1.pagespeed["performance"] == 38 and "attempts" not in c1.pagespeed


def test_check_saved_companies_marks_shared_pages_without_testing(db):
    # Regression: Facebook / Linktree pages were tested and scored as the business's own site.
    db.add_all([company(1, website="https://www.facebook.com/clinic1", age_days=9),
                company(2, website="https://linktr.ee/clinic2", age_days=8),
                company(3, website="https://clinic3.wixsite.com/home", age_days=7),
                company(4, age_days=6)])
    db.commit()
    fake = FakePSI()
    # limit=1: the shared pages don't use it up, so the Wix site (their own) still gets tested.
    assert pagespeed.check_saved_companies(db, limit=1, transport=fake.transport) == 3
    assert [r.url.params["url"] for r in fake.requests] == ["https://clinic3.wixsite.com/home"]
    db.expire_all()
    by_name = {c.name: c for c in db.query(Company)}
    for name in ("Clinic 1", "Clinic 2"):
        assert by_name[name].pagespeed["error"] == pagespeed.SHARED_PAGE
        assert by_name[name].pagespeed_checked_at is not None  # not picked again every slice
    assert by_name["Clinic 3"].pagespeed["performance"] == 38
    assert by_name["Clinic 4"].pagespeed_checked_at is None


def test_quota_without_key_warns_once(db, monkeypatch, caplog):
    monkeypatch.setattr(pagespeed, "_warned_no_key", False)
    db.add(company(1))
    db.commit()
    fake = FakePSI((429, {"error": {"code": 429}}))
    with caplog.at_level("INFO", logger="pagespeed"):
        pagespeed.check_saved_companies(db, transport=fake.transport)
        pagespeed.check_saved_companies(db, transport=fake.transport)
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "PAGESPEED_API_KEY" in r.getMessage()]
    assert len(warnings) == 1


def test_check_saved_companies_marks_odd_websites(db):
    db.add(company(1, website="www.no-scheme.example.com"))
    db.commit()
    fake = FakePSI()
    assert pagespeed.check_saved_companies(db, transport=fake.transport) == 1
    assert fake.requests == []
    assert db.query(Company).one().pagespeed["error"] == "not a web address"


def test_check_saved_companies_switched_off(db, monkeypatch):
    db.add(company(1))
    db.commit()
    monkeypatch.setattr(config, "PAGESPEED_ENABLED", False)
    fake = FakePSI()
    assert pagespeed.check_saved_companies(db, transport=fake.transport) == 0
    assert fake.requests == []


def test_check_saved_companies_never_raises(db, monkeypatch):
    db.add(company(1))
    db.commit()

    def broken_check(url, transport=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(pagespeed, "check", broken_check)
    assert pagespeed.check_saved_companies(db) == 0
