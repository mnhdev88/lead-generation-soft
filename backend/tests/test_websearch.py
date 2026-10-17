"""Web search provider (Brave / Serper). No real network: httpx.MockTransport fakes the APIs."""
import json

import httpx
import pytest

from app import config, usage
from app.providers import websearch
from app.sources import SourceError
from app.usage import UsageLimitError

BRAVE_BODY = {
    "web": {"results": [
        {"title": "Jane Doe - Owner - Smile Dental | LinkedIn", "url": "https://www.linkedin.com/in/janedoe",
         "description": "Owner at <strong>Smile Dental</strong> &amp; Co · Location: Austin"},
        {"title": "No link"},  # skipped
        {"title": "Bob", "url": "https://www.linkedin.com/in/bob"},  # no description
    ]},
}
SERPER_BODY = {
    "organic": [
        {"title": "Jane Doe - Owner - Smile Dental | LinkedIn", "link": "https://www.linkedin.com/in/janedoe",
         "snippet": "Owner at Smile Dental · Location: Austin", "position": 1},
        "junk",
    ],
}


class Fake:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.body, self.error = status, body, error
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handler(request):
            self.requests.append(request)
            if self.error:
                raise self.error
            if isinstance(self.body, str):
                return httpx.Response(self.status, text=self.body)
            return httpx.Response(self.status, json=self.body if self.body is not None else {})
        return httpx.MockTransport(handler)


@pytest.fixture
def brave(db, monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(config, "BRAVE_API_KEY", "brave-key")
    monkeypatch.setattr(config, "SERPER_API_KEY", "")
    monkeypatch.setitem(usage.PROVIDERS, "web_search", 3)


@pytest.fixture
def serper(db, monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "serper")
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    monkeypatch.setattr(config, "SERPER_API_KEY", "serper-key")
    monkeypatch.setitem(usage.PROVIDERS, "web_search", 3)


def test_available_follows_the_chosen_provider(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    monkeypatch.setattr(config, "SERPER_API_KEY", "x")
    assert websearch.available() is False
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "serper")
    assert websearch.available() is True
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "google")
    assert websearch.available() is False


def test_brave_request_and_results(brave):
    fake = Fake(body=BRAVE_BODY)
    out = websearch.search('site:linkedin.com/in "Smile Dental"', count=50, transport=fake.transport())
    assert out == [
        {"title": "Jane Doe - Owner - Smile Dental | LinkedIn", "url": "https://www.linkedin.com/in/janedoe",
         "snippet": "Owner at Smile Dental & Co · Location: Austin"},
        {"title": "Bob", "url": "https://www.linkedin.com/in/bob", "snippet": ""},
    ]
    req = fake.requests[0]
    assert req.method == "GET" and str(req.url).startswith(websearch.BRAVE_URL)
    assert req.url.params["q"] == 'site:linkedin.com/in "Smile Dental"'
    assert req.url.params["count"] == "20"  # capped
    assert req.headers["X-Subscription-Token"] == "brave-key"
    assert req.headers["Accept"] == "application/json"
    assert "brave-key" not in str(req.url)
    assert usage.get_usage("web_search")[0] == 1


def test_serper_request_and_results(serper):
    fake = Fake(body=SERPER_BODY)
    out = websearch.search("owner smile dental", transport=fake.transport())
    assert out == [{"title": "Jane Doe - Owner - Smile Dental | LinkedIn", "url": "https://www.linkedin.com/in/janedoe",
                    "snippet": "Owner at Smile Dental · Location: Austin"}]
    req = fake.requests[0]
    assert req.method == "POST" and str(req.url) == websearch.SERPER_URL
    assert json.loads(req.content) == {"q": "owner smile dental", "num": 10}
    assert req.headers["X-API-KEY"] == "serper-key"


def test_monthly_cap_stops_before_sending(brave):
    fake = Fake(body=BRAVE_BODY)
    for _ in range(3):
        websearch.search("q", transport=fake.transport())
    with pytest.raises(UsageLimitError, match="Web search"):
        websearch.search("q", transport=fake.transport())
    assert len(fake.requests) == 3


@pytest.mark.parametrize("status, words", [
    (401, "BRAVE_API_KEY"), (403, "refused"), (402, "credit"), (422, "could not read"),
    (429, "too many"), (500, "not answering"),
])
def test_http_errors_are_friendly(brave, status, words):
    with pytest.raises(SourceError, match=words) as e:
        websearch.search("q", transport=Fake(status=status, body={"error": "x"}).transport())
    assert not isinstance(e.value, UsageLimitError)


def test_network_error_and_bad_json(brave):
    with pytest.raises(SourceError, match="Could not reach Brave Search"):
        websearch.search("q", transport=Fake(error=httpx.ConnectError("down")).transport())
    with pytest.raises(SourceError, match="unreadable"):
        websearch.search("q", transport=Fake(body="<html>").transport())
    assert websearch.search("q", transport=Fake(body={"web": None}).transport()) == []


def test_no_key_and_unknown_provider(db, monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    with pytest.raises(SourceError, match="BRAVE_API_KEY"):
        websearch.search("q")
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "bing")
    with pytest.raises(SourceError, match="brave or serper"):
        websearch.search("q")


def test_empty_query_costs_nothing(brave):
    fake = Fake(body=BRAVE_BODY)
    assert websearch.search("   ", transport=fake.transport()) == []
    assert fake.requests == [] and usage.get_usage("web_search")[0] == 0
