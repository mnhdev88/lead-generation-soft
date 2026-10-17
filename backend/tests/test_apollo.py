"""Apollo.io provider. No real network: httpx.MockTransport fakes Apollo."""
import httpx
import pytest

from app import config, usage
from app.providers import apollo
from app.sources import SourceError
from app.usage import UsageLimitError

SEARCH_BODY = {
    "total_entries": 4,
    "people": [
        {"id": "m1", "first_name": "Mia", "last_name_obfuscated": "Ma***r", "title": "Office Manager",
         "has_email": True, "organization": {"name": "Smile Dental"}},
        {"id": "o1", "first_name": "Jane", "last_name_obfuscated": "Do***e", "title": "Owner",
         "has_email": True, "organization": {"name": "Smile Dental"}},
        {"id": "c1", "first_name": "Carl", "last_name_obfuscated": "Ce***o", "title": "CEO",
         "has_email": False, "organization": {"name": "Smile Dental"}},
        {"id": "d1", "first_name": "Dan", "title": "Director of Operations", "seniority": "director",
         "has_email": True},
        {"first_name": "No id"},
    ],
}
PEOPLE = {
    "o1": {"id": "o1", "name": "Jane Doe", "title": "Owner", "email": "Jane@SmileDental.com",
           "email_status": "verified", "linkedin_url": "http://www.linkedin.com/in/janedoe", "seniority": "owner"},
    "d1": {"id": "d1", "first_name": "Dan", "last_name": "Roe", "title": "Director of Operations",
           "email": None, "email_status": "unavailable", "linkedin_url": None},
    "m1": {"id": "m1", "name": "Mia Miller", "title": "Office Manager", "email": "mia@smiledental.com",
           "email_status": "extrapolated"},
}


class FakeApollo:
    def __init__(self, search_status=200, match_status=200, fail_match_after=None):
        self.search_status, self.match_status = search_status, match_status
        self.fail_match_after = fail_match_after
        self.requests: list[httpx.Request] = []
        self.matches = 0

    def transport(self):
        def handler(request):
            self.requests.append(request)
            if request.url.path.endswith("/mixed_people/api_search"):
                return httpx.Response(self.search_status, json=SEARCH_BODY)
            self.matches += 1
            if self.fail_match_after is not None and self.matches > self.fail_match_after:
                return httpx.Response(422, json={"error": "insufficient credits"})
            if self.match_status != 200:
                return httpx.Response(self.match_status, json={})
            return httpx.Response(200, json={"person": PEOPLE.get(request.url.params["id"])})
        return httpx.MockTransport(handler)


@pytest.fixture
def apollo_on(db, monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "apollo-key")
    monkeypatch.setitem(usage.PROVIDERS, "apollo", 5)


def test_available(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")
    assert apollo.available() is False
    monkeypatch.setattr(config, "APOLLO_API_KEY", "k")
    assert apollo.available() is True


def test_search_then_match_best_two(apollo_on):
    fake = FakeApollo()
    out = apollo.find_decision_makers("SmileDental.com", transport=fake.transport())
    # With an email first, then the most senior: Jane (owner), Dan (director); Carl has no email.
    assert out == [
        {"full_name": "Jane Doe", "title": "Owner", "email": "jane@smiledental.com", "email_status": "verified",
         "linkedin_url": "https://www.linkedin.com/in/janedoe", "apollo_id": "o1"},
        {"full_name": "Dan Roe", "title": "Director of Operations", "email": None, "email_status": None,
         "linkedin_url": None, "apollo_id": "d1"},
    ]
    search = fake.requests[0]
    assert search.method == "POST" and search.url.path == "/api/v1/mixed_people/api_search"
    assert search.url.params.get_list("q_organization_domains_list[]") == ["smiledental.com"]
    assert search.url.params.get_list("person_seniorities[]") == [
        "owner", "founder", "c_suite", "partner", "director", "manager"]
    assert search.url.params["per_page"] == "10"
    assert search.headers["x-api-key"] == "apollo-key" and "apollo-key" not in str(search.url)
    match = fake.requests[1]
    assert match.url.path == "/api/v1/people/match"
    assert match.url.params["id"] == "o1" and match.url.params["reveal_personal_emails"] == "false"
    assert "reveal_phone_number" not in match.url.params
    assert usage.get_usage("apollo")[0] == 2  # only the matches cost credits


def test_limit_and_no_key_and_shared_domains(apollo_on, monkeypatch):
    fake = FakeApollo()
    assert len(apollo.find_decision_makers("smiledental.com", limit=1, transport=fake.transport())) == 1
    assert apollo.find_decision_makers("facebook.com", transport=fake.transport()) == []
    assert apollo.find_decision_makers("", transport=fake.transport()) == []
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")
    assert apollo.find_decision_makers("smiledental.com", transport=fake.transport()) == []
    assert len(fake.requests) == 2  # one search + one match, nothing for the others


@pytest.mark.parametrize("status, words", [
    (401, "APOLLO_API_KEY"), (403, "plan"), (422, "credits"), (429, "too many"), (503, "not answering"),
])
def test_search_errors_are_friendly(apollo_on, status, words):
    with pytest.raises(SourceError, match=words):
        apollo.find_decision_makers("smiledental.com", transport=FakeApollo(search_status=status).transport())
    assert usage.get_usage("apollo")[0] == 0


def test_match_error_before_anyone_raises(apollo_on):
    with pytest.raises(SourceError, match="APOLLO_API_KEY"):
        apollo.find_decision_makers("smiledental.com", transport=FakeApollo(match_status=401).transport())


def test_match_error_after_someone_keeps_them(apollo_on):
    out = apollo.find_decision_makers("smiledental.com", transport=FakeApollo(fail_match_after=1).transport())
    assert [p["full_name"] for p in out] == ["Jane Doe"]


def test_monthly_cap(apollo_on, monkeypatch):
    monkeypatch.setitem(usage.PROVIDERS, "apollo", 1)
    fake = FakeApollo()
    assert len(apollo.find_decision_makers("smiledental.com", transport=fake.transport())) == 1  # kept
    with pytest.raises(UsageLimitError, match="Apollo.io"):
        apollo.find_decision_makers("smiledental.com", transport=fake.transport())
    assert fake.matches == 1


def test_network_error(apollo_on):
    def boom(request):
        raise httpx.ConnectTimeout("slow")
    with pytest.raises(SourceError, match="Could not reach Apollo.io"):
        apollo.find_decision_makers("smiledental.com", transport=httpx.MockTransport(boom))
