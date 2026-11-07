"""Owners confirmed by several sources, and the business register (app/providers/registry.py)."""
import httpx
import pytest

from app import config
from app.models import Company, Contact
from app.providers import registry
from app.scoring import score_contact
from app.sources import SourceError
from tests.test_people_job import company, keys, people_of, profile, run_people, services  # noqa: F401  (fixtures)

SEARCH = {"results": {"companies": [
    {"company": {"name": "SMILE DENTAL PLLC", "company_number": "801", "jurisdiction_code": "us_tx",
                 "inactive": False, "opencorporates_url": "https://opencorporates.com/companies/us_tx/801"}},
    {"company": {"name": "SMILEY BURGERS LLC", "company_number": "900", "jurisdiction_code": "us_tx"}},
]}}
DETAIL = {"results": {"company": {
    "name": "SMILE DENTAL PLLC", "opencorporates_url": "https://opencorporates.com/companies/us_tx/801",
    "officers": [
        {"officer": {"name": "DOE, JANE A", "position": "MANAGING MEMBER"}},
        {"officer": {"name": "CORPORATION SERVICE COMPANY", "position": "registered agent"}},
        {"officer": {"name": "OLD PARTNER", "position": "member", "end_date": "2019-01-01"}},
    ],
}}}


class FakeRegister:
    def __init__(self, status=200):
        self.status, self.requests = status, []

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.status != 200:
                return httpx.Response(self.status, json={})
            return httpx.Response(200, json=DETAIL if "/companies/us_tx/801" in request.url.path else SEARCH)
        return httpx.MockTransport(handler)


@pytest.fixture
def register(monkeypatch, db):
    monkeypatch.setattr(config, "OPENCORPORATES_API_TOKEN", "test-token")
    fake = FakeRegister()
    monkeypatch.setattr(registry, "mock_transport", fake.transport())
    return fake


def texas(**kw):
    fields = dict(name="Smile Dental", address="1 Main St, Austin, TX 78701, USA", country="US")
    fields.update(kw)
    return Company(**fields)


def test_which_register():
    assert registry.jurisdiction(texas()) == "us_tx"
    assert registry.jurisdiction(texas(address="Austin", country="US")) is None  # no state
    assert registry.jurisdiction(texas(address="London", country="United Kingdom")) == "gb"
    assert registry.jurisdiction(texas(address="Karachi", country="Pakistan")) is None


def test_person_names():
    assert registry.person_name("DOE, JANE A") == "Jane A Doe"
    assert registry.person_name("John McAdams") == "John McAdams"
    assert registry.person_name("ACME HOLDINGS LLC") is None
    assert registry.person_name("MADONNA") is None


def test_find_officers(register):
    officers = registry.find_officers(texas())
    assert [(o.full_name, o.position) for o in officers] == [("Jane A Doe", "Managing Member")]
    assert officers[0].source_url == "https://opencorporates.com/companies/us_tx/801"
    assert all(r.url.params["api_token"] == "test-token" for r in register.requests)


def test_register_errors_are_explained(register, monkeypatch):
    fake = FakeRegister(status=401)
    monkeypatch.setattr(registry, "mock_transport", fake.transport())
    with pytest.raises(SourceError, match="OPENCORPORATES_API_TOKEN"):
        registry.find_officers(texas())


def test_owner_found_on_the_website_is_confirmed_by_the_register(db, services, register):
    services.web_key = services.apollo_key = False
    owner = Contact(full_name="Jane Doe", title="Owner", source="website", is_decision_maker=True, seniority="owner")
    c = company(db, "Smile Dental", address="1 Main St, Austin, TX 78701", country="US", contacts=[owner])
    # Already has a decision-maker: only looked up when asked to confirm.
    run_people(db, [c.id])
    assert register.requests == []
    job = run_people(db, [c.id], confirm=True)
    assert "1 people from business registers" in job.progress_message
    jane = people_of(db, c)["Jane Doe"]  # "Jane A Doe" in the register: the same person
    assert jane.confirmed_by == ["website", "registry"]
    assert "Confirmed by 2 sources (website + business register) +10" in jane.score_reasons


def test_linkedin_search_confirms_too(db, services):
    owner = Contact(full_name="Sara Khan", title="Owner", source="website", is_decision_maker=True, seniority="owner")
    c = company(db, "Bright Dental", contacts=[owner])
    services.people_results["Bright Dental"] = [
        profile("Sara Khan", "Owner", "Bright Dental", "https://www.linkedin.com/in/sara-khan")]
    run_people(db, [c.id], confirm=True)
    sara = people_of(db, c)["Sara Khan"]
    assert sara.confirmed_by == ["website", "linkedin_search"]
    assert sara.linkedin_url == "https://www.linkedin.com/in/sara-khan"


def test_one_source_is_not_confirmed():
    person = Contact(full_name="A B", source="website", confirmed_by=["website"])
    assert not any("Confirmed" in r for r in score_contact(person)[1])


def test_find_people_endpoint_passes_confirm(client, db, keys):
    c = company(db, "Smile Dental")
    r = client.post("/api/companies/find-people", json={"company_ids": [c.id], "confirm": True})
    assert r.status_code == 201 and r.json()["params"]["confirm"] is True


def test_the_register_alone_is_enough_to_find_owners(client, db, monkeypatch):
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    monkeypatch.setattr(config, "SERPER_API_KEY", "")
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")
    c = company(db, "Smile Dental")
    assert client.post("/api/companies/find-people", json={"company_ids": [c.id]}).status_code == 400
    monkeypatch.setattr(config, "OPENCORPORATES_API_TOKEN", "t")
    assert client.post("/api/companies/find-people", json={"company_ids": [c.id]}).status_code == 201
