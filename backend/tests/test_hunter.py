"""Hunter.io Domain Search provider. No real network: httpx.MockTransport fakes Hunter."""
import httpx
import pytest

from app import config, usage
from app.providers import hunter

HUNTER_RESPONSE = {
    "data": {
        "domain": "acme.com",
        "disposable": False,
        "webmail": False,
        "accept_all": False,
        "pattern": "{first}.{last}",
        "organization": "Acme",
        "emails": [
            {
                "value": "Jane.Doe@acme.com",
                "type": "personal",
                "confidence": 94,
                "sources": [],
                "first_name": "Jane",
                "last_name": "Doe",
                "position": "Chief Executive Officer",
                "seniority": "executive",
                "department": "executive",
                "linkedin": "https://www.linkedin.com/in/janedoe",
                "twitter": None,
                "phone_number": None,
                "verification": {"date": "2026-09-01", "status": "valid"},
            },
            {"value": "info@acme.com", "type": "generic", "confidence": 80, "first_name": None,
             "last_name": None, "position": None, "linkedin": None},
            {"value": None, "confidence": 10},  # broken entry is skipped
        ],
    },
    "meta": {"results": 3, "limit": 10, "offset": 0},
}


class FakeHunter:
    def __init__(self, status=200, body=None, error=None):
        self.status = status
        self.body = HUNTER_RESPONSE if body is None else body
        self.error = error
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error:
                raise self.error
            return httpx.Response(self.status, json=self.body)
        return httpx.MockTransport(handler)


@pytest.fixture
def hunter_on(db, monkeypatch):
    monkeypatch.setattr(config, "HUNTER_API_KEY", "test-key-123")
    monkeypatch.setitem(usage.PROVIDERS, "hunter", 3)


def test_available_reads_key_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "HUNTER_API_KEY", "")
    assert hunter.available() is False
    monkeypatch.setattr(config, "HUNTER_API_KEY", "abc")
    assert hunter.available() is True


def test_domain_search_maps_response(hunter_on):
    fake = FakeHunter()
    pattern, people = hunter.domain_search("Acme.com", limit=50, http_transport=fake.transport())
    assert pattern == "{first}.{last}"
    assert len(people) == 2
    jane = people[0]
    assert (jane.email, jane.first_name, jane.last_name, jane.position, jane.confidence, jane.linkedin_url) == (
        "jane.doe@acme.com", "Jane", "Doe", "Chief Executive Officer", 94, "https://www.linkedin.com/in/janedoe")
    assert people[1].email == "info@acme.com" and people[1].first_name is None
    assert people[1].email_type == "generic"  # the worker keeps it off the people list

    request = fake.requests[0]
    assert request.url.path == "/v2/domain-search"
    assert request.url.params["domain"] == "acme.com"
    assert request.url.params["limit"] == "10"  # capped to the free-plan maximum
    assert "api_key" not in request.url.params  # key goes in a header, never in the URL/logs
    assert request.headers["X-API-KEY"] == "test-key-123"
    assert usage.get_usage("hunter")[0] == 1


def test_domain_search_counts_usage_and_stops_at_cap(hunter_on):
    fake = FakeHunter()
    for _ in range(3):
        hunter.domain_search("acme.com", http_transport=fake.transport())
    with pytest.raises(usage.UsageLimitError):
        hunter.domain_search("acme.com", http_transport=fake.transport())
    assert len(fake.requests) == 3  # the 4th request was never sent


def test_domain_search_without_key_does_nothing(db, monkeypatch):
    monkeypatch.setattr(config, "HUNTER_API_KEY", "")
    fake = FakeHunter()
    assert hunter.domain_search("acme.com", http_transport=fake.transport()) == (None, [])
    assert fake.requests == []
    assert usage.get_usage("hunter")[0] == 0


@pytest.mark.parametrize("status, body", [
    (401, {"errors": [{"id": "authentication_failed", "code": 401, "details": "No user found for the API key"}]}),
    (429, {"errors": [{"id": "too_many_requests", "code": 429, "details": "You have reached your monthly quota"}]}),
    (500, {"oops": True}),
])
def test_domain_search_http_errors_give_empty_result(hunter_on, status, body):
    assert hunter.domain_search("acme.com", http_transport=FakeHunter(status, body).transport()) == (None, [])


def test_domain_search_network_error_gives_empty_result(hunter_on):
    fake = FakeHunter(error=httpx.ConnectError("no internet"))
    assert hunter.domain_search("acme.com", http_transport=fake.transport()) == (None, [])


def test_domain_search_unknown_domain_and_odd_pattern(hunter_on):
    empty = {"data": {"domain": None, "pattern": None, "emails": []}, "meta": {"results": 0}}
    assert hunter.domain_search("nothing.xyz", http_transport=FakeHunter(body=empty).transport()) == (None, [])
    odd = {"data": {"pattern": "{first}{middle}", "emails": []}}
    assert hunter.domain_search("acme.com", http_transport=FakeHunter(body=odd).transport()) == (None, [])


def test_module_level_transport_is_used(hunter_on, monkeypatch):
    fake = FakeHunter()
    monkeypatch.setattr(hunter, "transport", fake.transport())
    pattern, people = hunter.domain_search("acme.com")
    assert pattern == "{first}.{last}" and len(people) == 2
