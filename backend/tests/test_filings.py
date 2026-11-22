"""New business filings (app/sources/filings.py), saving them, matching them on Google Maps
and the search API options. The state portals are faked with httpx.MockTransport."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app import worker
from app.intent import estimate_intent
from app.models import Company, Contact, JobResult, SearchJob
from app.sources import SOURCES, CompanyResult, SourceError
from app.sources.filings import NewBusinessFilingsSource, keyword, nice_name, parse_location

from .test_api import fake_sources  # noqa: F401  (fixture)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def portal(rows_by_host: dict[str, list | callable], calls: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        rows = rows_by_host[request.url.host]
        return httpx.Response(200, json=rows(request) if callable(rows) else rows)
    return httpx.MockTransport(handler)


def soql(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


# ----- small helpers ------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("Denver, CO", ("CO", "DENVER")), ("Colorado", ("CO", None)), ("Denver CO", ("CO", "DENVER")),
    ("New York", ("NY", None)), ("Brooklyn, New York, USA", ("NY", "BROOKLYN")), ("Portland, OR", ("OR", "PORTLAND")),
    ("Hartford, Connecticut", ("CT", "HARTFORD")),
])
def test_parse_location(text, expected):
    assert parse_location(text) == expected


def test_other_states_are_refused():
    with pytest.raises(SourceError, match="Colorado, New York, Connecticut and Oregon"):
        parse_location("Austin, TX")


def test_keyword_and_names():
    assert keyword("all") is None and keyword(" Any ") is None and keyword("house  cleaning") == "HOUSE CLEANING"
    assert nice_name("JOSIAH'S HANDYMAN & CONSTRUCTION LLC") == "Josiah's Handyman & Construction LLC"
    assert nice_name("JCD Cleaning LLC") == "JCD Cleaning LLC"  # mixed case stays


# ----- each state ------------------------------------------------------------------------


def test_colorado():
    calls = []
    rows = [
        {"entityid": "1", "entityname": "JCD Cleaning LLC", "principaladdress1": "1335 Peoria St",
         "principalcity": "Aurora", "principalstate": "CO", "principalzipcode": "80011", "entitytype": "DLLC",
         "agentfirstname": "JONATHAN", "agentlastname": "ALVARENGA", "entityformdate": f"{TODAY}T00:00:00.000"},
        {"entityid": "2", "entityname": "Sparkle Cleaning Holdings LLC", "principalcity": "Aurora",
         "entityformdate": f"{TODAY}T00:00:00.000", "agentorganizationname": "REGISTERED AGENTS INC"},
        {"entityid": "3", "entityname": "Fresh Cleaning LLC", "principalcity": "Aurora",
         "agentfirstname": "Northwest Registered Agent", "agentlastname": "LLC", "entityformdate": f"{TODAY}T00:00:00"},
    ]
    notes = []
    source = NewBusinessFilingsSource(portal({"data.colorado.gov": rows}, calls))
    results = source.search("Cleaning", "Aurora, CO", 10, notes.append, days=14)
    assert [r.name for r in results] == ["JCD Cleaning LLC", "Fresh Cleaning LLC"]  # holdings left out
    assert any("Left out 1" in n for n in notes)
    jcd = results[0]
    assert jcd.source_ref == "CO-1" and jcd.country == "US" and jcd.category == "Cleaning"
    assert jcd.address == "1335 Peoria St, Aurora, CO 80011"
    assert jcd.raw["filing"]["filed"] == TODAY and jcd.raw["filing"]["state"] == "CO"
    assert jcd.raw["filing"]["people"] == [{"name": "Jonathan Alvarenga", "role": "Registered agent"}]
    assert results[1].raw["filing"]["people"] == []  # an agent company is not a person
    where = soql(calls[0])["$where"]
    assert "upper(principalcity) = 'AURORA'" in where and "upper(entityname) like '%CLEANING%'" in where
    assert "entitytype != 'DNC'" in where


def test_new_york_uses_the_location_else_the_mail_address():
    rows = [{"dos_id": "8138766", "current_entity_name": "2002 MAYA FOOD CORP.", "initial_dos_filing_date": TODAY,
             "entity_type": "DOMESTIC BUSINESS CORPORATION", "dos_process_name": "ALOKAIMI M AL HALEMI",
             "dos_process_address_1": "2002 AMSTERDAM AVE", "dos_process_city": "NEW YORK", "dos_process_state": "NY",
             "dos_process_zip": "10032"}]
    r = NewBusinessFilingsSource(portal({"data.ny.gov": rows})).search("all", "New York", 5, lambda m: None)[0]
    assert r.name == "2002 Maya Food CORP." and r.city == "New York" and r.category is None
    assert r.address == "2002 AMSTERDAM AVE, NEW YORK, NY 10032"
    assert r.raw["filing"]["people"] == [{"name": "Alokaimi M Al Halemi", "role": "Contact for state mail"}]


def test_connecticut_gives_an_email_and_industry():
    calls = []
    rows = [{"id": "abc", "name": "Get Squeezed LLC", "business_type": "LLC", "date_registration": TODAY,
             "billingstreet": "5 Sunburst Rd", "billingcity": "Naugatuck", "billingstate": "CT",
             "billingpostalcode": "06770", "business_email_address": "getsqueezed26@yahoo.com",
             "naics_code": "Snack and Nonalcoholic Beverage Bars (722515)", "accountnumber": "2901"}]
    r = NewBusinessFilingsSource(portal({"data.ct.gov": rows}, calls)).search("juice", "Connecticut", 5, lambda m: None)[0]
    assert r.email == "getsqueezed26@yahoo.com" and r.category == "Snack and Nonalcoholic Beverage Bars"
    assert "upper(naics_code) like '%JUICE%'" in soql(calls[0])["$where"]


def test_oregon_joins_the_rows_of_a_business():
    place = {"registry_number": "265124892", "business_name": "JOSIAH'S HANDYMAN & CONSTRUCTION SERVICES",
             "entity_type": "ASSUMED BUSINESS NAME", "registry_date": TODAY, "associated_name_type":
             "PRINCIPAL PLACE OF BUSINESS", "address": "33940 ROLLING HILLS DR", "city": "SCAPPOOSE", "state": "OR",
             "zip": "97056", "business_details": {"url": "http://egov.sos.state.or.us/br/x"}}
    rep = {"registry_number": "265124892", "associated_name_type": "AUTHORIZED REPRESENTATIVE",
           "first_name": "JOSIAH", "last_name": "PLUEARD"}
    calls = []

    def rows(request):
        return [rep] if "registry_number in" in soql(request)["$where"] else [place]
    results = NewBusinessFilingsSource(portal({"data.oregon.gov": rows}, calls)).search(
        "handyman", "Oregon", 5, lambda m: None)
    assert len(calls) == 2 and "registry_number in ('265124892')" in soql(calls[1])["$where"]
    r = results[0]
    assert r.name == "Josiah's Handyman & Construction Services" and r.city == "Scappoose"
    assert r.raw["filing"]["people"] == [{"name": "Josiah Plueard", "role": "Owner"}]  # the DBA's registrant
    assert r.raw["filing"]["url"] == "http://egov.sos.state.or.us/br/x"


def test_portal_errors_are_readable():
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="oops"))
    with pytest.raises(SourceError, match="not answering properly"):
        NewBusinessFilingsSource(transport).search("all", "Colorado", 5, lambda m: None)


# ----- saving and Google Maps ---------------------------------------------------------------


def filing_result(ref="CO-1", name="JCD Cleaning LLC", filed=TODAY, people=None):
    return CompanyResult(name=name, source_ref=ref, city="Aurora", country="US",
                         address="1335 Peoria St, Aurora, CO 80011",
                         raw={"filing": {"state": "CO", "filed": filed, "entity_type": "DLLC", "registry_id": ref,
                                         "people": people if people is not None else
                                         [{"name": "Jonathan Alvarenga", "role": "Registered agent"}]}})


class FakeFilings(NewBusinessFilingsSource):
    def __init__(self, results):
        super().__init__()
        self.results, self.days = results, None

    def search(self, query, location, limit, progress, days=30):
        self.days = days
        return self.results


class FakeGoogle:
    name, label, description = "google", "Google", ""

    def __init__(self, results, reason=None):
        self.results, self.queries, self.reason = results, [], reason

    def unavailable_reason(self):
        return self.reason

    def search(self, query, location, limit, progress):
        self.queries.append((query, location))
        return self.results


def run(db, monkeypatch, filings, google=None, **params):
    monkeypatch.setitem(SOURCES, "filings", FakeFilings(filings))
    if google is not None:
        monkeypatch.setitem(SOURCES, "google", google)
    job = SearchJob(kind="search", query="cleaning", location="Aurora, CO", source="filings", limit=50,
                    params={"filed_days": 14, **params})
    db.add(job)
    db.commit()
    worker.run_search_job(db, job)
    db.commit()
    return job


def test_saves_the_filing_and_its_people(db, monkeypatch):
    job = run(db, monkeypatch, [filing_result()])
    assert SOURCES["filings"].days == 14
    company = db.scalars(select(Company)).one()
    assert company.filing["filed"] == TODAY and company.source == "filings"
    person = db.scalars(select(Contact)).one()
    assert person.full_name == "Jonathan Alvarenga" and person.title == "Registered agent"
    assert f"New business (registered with the state {TODAY}) +20" in company.intent["website"]["reasons"]
    assert not any(r.startswith("No website") for r in company.intent["website"]["reasons"])  # can't tell yet
    assert job.result_count == 1


def test_old_filing_is_not_new():
    old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
    company = Company(name="X", source="filings", source_ref="CO-9", filing={"state": "CO", "filed": old})
    assert not any("registered with the state" in r for r in estimate_intent(company)["website"]["reasons"])


def google_listing(name="JCD Cleaning", address="1335 Peoria St, Aurora, CO 80011, USA"):
    return CompanyResult(name=name, source_ref="g-jcd", phone="+1 720-555-0100", address=address, city="Aurora",
                         country="US", reviews_count=None, google_maps_url="https://maps.google.com/?cid=1",
                         business_status="OPERATIONAL")


def test_matches_on_google_and_merges(db, monkeypatch):
    google = FakeGoogle([google_listing(name="Other Place"), google_listing()])
    job = run(db, monkeypatch, [filing_result(), filing_result(ref="CO-2", name="Nowhere Cleaning LLC", people=[])],
              google=google, match_google=True)
    assert google.queries[0] == ('"JCD Cleaning LLC"', "Aurora, CO")
    companies = {c.name: c for c in db.scalars(select(Company))}
    assert len(companies) == 2  # JCD's filing and listing are one lead now
    jcd = companies["JCD Cleaning"]
    assert jcd.source == "google" and jcd.phone and jcd.filing["filed"] == TODAY
    assert [p.full_name for p in jcd.contacts] == ["Jonathan Alvarenga"]
    assert "No website +30" in jcd.intent["website"]["reasons"]  # Google's listing proves it
    results = {r.company_id for r in db.scalars(select(JobResult).where(JobResult.job_id == job.id))}
    assert results == {c.id for c in companies.values()} and job.result_count == 2
    assert "Found 1 of 2 new businesses on Google Maps (the other 1 have no phone yet)." in job.progress_message


def test_google_match_needs_the_same_state(db, monkeypatch):
    google = FakeGoogle([google_listing(address="1 Main St, Aurora, IL 60505, USA")])
    run(db, monkeypatch, [filing_result()], google=google, match_google=True)
    assert db.scalars(select(Company)).one().source == "filings"


def test_google_not_set_up(db, monkeypatch):
    google = FakeGoogle([], reason="Add GOOGLE_PLACES_API_KEY to backend/.env")
    job = run(db, monkeypatch, [filing_result()], google=google, match_google=True)
    assert "Not looked up on Google Maps: Add GOOGLE_PLACES_API_KEY" in job.progress_message
    assert google.queries == []


def test_google_stops_at_the_limit(db, monkeypatch):
    class Capped(FakeGoogle):
        def search(self, *a):
            raise SourceError("Monthly Google Places limit reached.")
    job = run(db, monkeypatch, [filing_result()], google=Capped([]), match_google=True)
    assert "Stopped looking: Monthly Google Places limit reached." in job.progress_message
    assert db.scalars(select(Company)).one().source == "filings"


# ----- API ---------------------------------------------------------------------------------


def test_api_options_and_place_check(client, monkeypatch):
    monkeypatch.setitem(SOURCES, "filings", NewBusinessFilingsSource())
    r = client.post("/api/jobs/batch", json={"queries": ["cleaning"], "locations": ["Denver, CO"],
                                             "source": "filings", "filed_days": 60, "match_google": True})
    assert r.status_code == 201
    params = r.json()[0]["params"]
    assert params["filed_days"] == 60 and params["match_google"] is True

    r = client.post("/api/jobs/batch", json={"queries": ["cleaning"], "locations": ["Austin, TX"], "source": "filings"})
    assert r.status_code == 400 and "Colorado, New York" in r.json()["detail"]
    r = client.post("/api/jobs", json={"query": "cleaning", "location": "Austin, TX", "source": "filings"})
    assert r.status_code == 400

    r = client.post("/api/jobs/batch", json={"queries": ["gym"], "locations": ["Austin"], "source": "osm"})
    assert "filed_days" not in r.json()[0]["params"]
    assert client.post("/api/jobs/batch", json={"queries": ["gym"], "locations": ["Denver, CO"], "source": "filings",
                                                "filed_days": 0}).status_code == 422
