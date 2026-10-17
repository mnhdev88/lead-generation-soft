"""Worker "people" jobs ("Find owner") and the POST /api/companies/find-people endpoint.

The web search and Apollo.io are replaced by small fakes on the worker module (no network);
the real LinkedIn result reader (app/people/linkedin.py) and title classifier are used."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import config, worker
from app.models import Company, Contact, SearchJob
from app.sources import SourceError
from app.usage import UsageLimitError


def profile(name, title, company, url, snippet=""):
    return {"title": f"{name} - {title} - {company} | LinkedIn", "url": url, "snippet": snippet}


class FakeServices:
    def __init__(self):
        self.web_key = True
        self.apollo_key = True
        self.people_results: dict[str, list[dict]] = {}  # company name -> results
        self.page_results: dict[str, list[dict]] = {}
        self.queries: list[str] = []
        self.web_error_after: int | None = None  # raise after this many searches
        self.web_error: Exception = UsageLimitError("Monthly Web search limit reached (2/2).")
        self.apollo_people: dict[str, list[dict]] = {}  # domain -> people
        self.apollo_calls: list[str] = []
        self.apollo_error: Exception | None = None

    def websearch(self):
        def search(query, count=10):
            if self.web_error_after is not None and len(self.queries) >= self.web_error_after:
                raise self.web_error
            self.queries.append(query)
            table = self.page_results if "linkedin.com/company" in query else self.people_results
            return next((v for k, v in table.items() if f'"{k}"' in query), [])
        return SimpleNamespace(available=lambda: self.web_key, search=search)

    def apollo(self):
        def find_decision_makers(domain, limit=2):
            if self.apollo_error:
                raise self.apollo_error
            self.apollo_calls.append(domain)
            return self.apollo_people.get(domain, [])[:limit]
        return SimpleNamespace(available=lambda: self.apollo_key, find_decision_makers=find_decision_makers)


@pytest.fixture
def services(db, monkeypatch):
    fake = FakeServices()
    monkeypatch.setattr(worker, "websearch", fake.websearch())
    monkeypatch.setattr(worker, "apollo", fake.apollo())
    return fake


def company(db, name, *, city="Austin", domain=None, contacts=(), **kw) -> Company:
    c = Company(name=name, source="osm", source_ref=name, city=city, domain=domain,
                website=f"https://{domain}" if domain else None, **kw)
    c.contacts = list(contacts)
    db.add(c)
    db.commit()
    return c


def run_people(db, ids, **params) -> SearchJob:
    job = SearchJob(kind="people", query=f"{len(ids)} selected companies", location="", source="people",
                    limit=len(ids), params={"company_ids": ids, **params})
    db.add(job)
    db.commit()
    worker.run_job(job.id)
    db.expire_all()
    return db.get(SearchJob, job.id)


def people_of(db, c: Company) -> dict[str, Contact]:
    return {x.full_name: x for x in db.scalars(select(Contact).where(Contact.company_id == c.id))}


def email_jobs(db) -> list[SearchJob]:
    return list(db.scalars(select(SearchJob).where(SearchJob.kind == "emails")))


# ----- LinkedIn via web search ----------------------------------------------------------


def test_linkedin_search_saves_owner_and_company_page(db, services, monkeypatch):
    monkeypatch.setattr(worker.config, "LINKEDIN_COMPANY_PAGE_SEARCH", True)  # off by default
    smile = company(db, "Smile Dental", domain="smiledental.com")
    services.people_results["Smile Dental"] = [
        profile("Jane Doe", "Owner", "Smile Dental", "https://www.linkedin.com/in/janedoe",
                "Owner at Smile Dental · Location: Austin, Texas, United States"),
        profile("Hal Hygienist", "Dental Hygienist", "Smile Dental", "https://www.linkedin.com/in/hal"),
        profile("Sam Other", "Owner", "Smile Dental Studio", "https://www.linkedin.com/in/sam"),
    ]
    services.page_results["Smile Dental"] = [
        {"title": "Smile Dental | LinkedIn", "url": "https://www.linkedin.com/company/smile-dental-atx", "snippet": ""}]

    job = run_people(db, [smile.id])
    assert job.status == "done", job.error
    assert (job.total, job.processed, job.result_count) == (1, 1, 1)
    assert len(services.queries) == 2 and services.queries[0].startswith("site:linkedin.com/in (")
    assert services.apollo_calls == []  # a decision-maker was found: Apollo not needed

    people = people_of(db, smile)
    assert list(people) == ["Jane Doe"]
    jane = people["Jane Doe"]
    assert (jane.first_name, jane.last_name, jane.title, jane.source) == ("Jane", "Doe", "Owner", "linkedin_search")
    assert jane.linkedin_url == jane.source_url == "https://www.linkedin.com/in/janedoe"
    assert jane.is_decision_maker and jane.seniority == "owner"
    assert jane.email is None

    db.refresh(smile)
    assert smile.socials["linkedin"] == "https://www.linkedin.com/company/smile-dental-atx"
    assert "1 with a decision-maker found (1 via LinkedIn search, 0 via Apollo)" in job.progress_message
    assert job.progress_message.endswith("Email check queued.")

    [emails] = email_jobs(db)
    assert emails.params == {"company_ids": [smile.id], "force": False, "crawl": False, "use_hunter": True}
    assert (emails.status, emails.parent_job_id, emails.source) == ("queued", job.id, "emails")


def test_company_page_not_searched_when_known(db, services):
    smile = company(db, "Smile Dental", socials={"linkedin": "https://www.linkedin.com/company/old"})
    run_people(db, [smile.id])
    assert len(services.queries) == 1
    db.refresh(smile)
    assert smile.socials == {"linkedin": "https://www.linkedin.com/company/old"}


def test_companies_with_a_decision_maker_are_skipped(db, services):
    boss = Contact(full_name="Old Boss", first_name="Old", last_name="Boss", title="Owner", is_decision_maker=True)
    known = company(db, "Known Dental", contacts=[boss])
    job = run_people(db, [known.id])
    assert services.queries == [] and services.apollo_calls == []
    assert (job.processed, job.result_count) == (1, 0)
    assert "1 already had one, skipped" in job.progress_message
    assert email_jobs(db) == []


def test_merges_with_existing_contacts(db, services):
    by_name = Contact(full_name="Jane Doe", first_name="Jane", last_name="Doe", source="website")
    by_link = Contact(full_name="J. Smith", source="website", title="Dentist",
                      linkedin_url="http://pk.linkedin.com/in/JohnSmith/")
    smile = company(db, "Smile Dental", contacts=[by_name, by_link])
    services.people_results["Smile Dental"] = [
        profile("Jane Doe", "Owner", "Smile Dental", "https://www.linkedin.com/in/janedoe"),
        profile("John Smith", "CEO", "Smile Dental", "https://www.linkedin.com/in/johnsmith"),
    ]
    job = run_people(db, [smile.id])
    people = people_of(db, smile)
    assert sorted(people) == ["J. Smith", "Jane Doe"]  # no duplicates
    jane = people["Jane Doe"]
    assert (jane.title, jane.source, jane.is_decision_maker) == ("Owner", "website", True)
    assert jane.linkedin_url == "https://www.linkedin.com/in/janedoe"
    assert people["J. Smith"].title == "Dentist"  # never overwritten
    assert job.result_count == 1


# ----- Apollo.io backup ------------------------------------------------------------------


def test_apollo_when_web_search_finds_no_one(db, services):
    smile = company(db, "Smile Dental", domain="smiledental.com")
    no_domain = company(db, "Facebook Dental", domain="facebook.com")
    services.apollo_people["smiledental.com"] = [
        {"full_name": "Jane Doe", "title": "Owner", "email": "Jane@SmileDental.com", "email_status": "verified",
         "linkedin_url": "https://www.linkedin.com/in/janedoe", "apollo_id": "a1"},
        {"full_name": "Mia Miller", "title": "Office Manager", "email": "mia@smiledental.com",
         "email_status": "extrapolated", "linkedin_url": None, "apollo_id": "a2"},
        {"full_name": "Dan Roe", "title": "Director", "email": None, "email_status": "unavailable",
         "linkedin_url": None, "apollo_id": "a3"},
    ]
    job = run_people(db, [smile.id, no_domain.id])
    assert services.apollo_calls == ["smiledental.com"]  # facebook.com is not the company's own domain
    people = people_of(db, smile)
    jane, mia = people["Jane Doe"], people["Mia Miller"]
    assert (jane.source, jane.email, jane.email_source, jane.email_status, jane.email_confidence) == (
        "apollo", "jane@smiledental.com", "apollo", "valid", 80)
    assert (mia.email_status, mia.email_confidence) == ("unknown", None)
    assert "Dan Roe" not in people  # limit 2 per company
    assert jane.is_decision_maker
    assert job.result_count == 1
    assert "(0 via LinkedIn search, 2 via Apollo)" in job.progress_message
    assert email_jobs(db)[0].params["company_ids"] == [smile.id]


def test_apollo_unavailable_status_and_use_apollo_off(db, services):
    smile = company(db, "Smile Dental", domain="smiledental.com")
    services.apollo_people["smiledental.com"] = [
        {"full_name": "Dan Roe", "title": "Director", "email": None, "email_status": "unavailable",
         "linkedin_url": None, "apollo_id": "a3"}]
    run_people(db, [smile.id], use_apollo=False)
    assert services.apollo_calls == []
    # force: the first run already looked this business up (it would be skipped for 90 days)
    job = run_people(db, [smile.id], find_emails=False, force=True)
    dan = people_of(db, smile)["Dan Roe"]
    assert (dan.email, dan.email_status, dan.email_source) == (None, None, None)
    assert job.result_count == 1
    assert email_jobs(db) == []  # find_emails=False


def test_apollo_error_stops_apollo_for_the_rest(db, services):
    a = company(db, "Alpha Dental", domain="alpha.com")
    b = company(db, "Beta Dental", domain="beta.com")
    services.apollo_error = SourceError("Apollo.io refused the API key (HTTP 401).")
    job = run_people(db, [a.id, b.id])
    assert job.status == "done" and job.processed == 2
    assert "Apollo.io stopped: Apollo.io refused the API key" in job.progress_message


# ----- limits and errors -----------------------------------------------------------------


def test_web_search_limit_keeps_what_was_found(db, services):
    services.apollo_key = False
    a = company(db, "Alpha Dental", socials={"linkedin": "x"})
    b = company(db, "Beta Dental")
    c = company(db, "Gamma Dental")
    services.people_results["Alpha Dental"] = [
        profile("Ann Alpha", "Owner", "Alpha Dental", "https://www.linkedin.com/in/ann")]
    services.web_error_after = 1  # the 2nd search hits the monthly cap
    job = run_people(db, [a.id, b.id, c.id])
    assert job.status == "done", job.error
    assert list(people_of(db, a)) == ["Ann Alpha"]
    assert (job.processed, job.result_count) == (2, 1)
    assert "Stopped after 2 of 3 companies (Web search monthly limit reached); kept what was found." in job.progress_message
    assert len(email_jobs(db)) == 1


def test_web_search_limit_then_apollo_continues(db, services):
    a = company(db, "Alpha Dental", domain="alpha.com")
    services.web_error_after = 0
    services.apollo_people["alpha.com"] = [{"full_name": "Ann Alpha", "title": "CEO", "email": None}]
    job = run_people(db, [a.id])
    assert job.result_count == 1 and services.apollo_calls == ["alpha.com"]
    assert "Web search monthly limit reached." in job.progress_message


def test_repeated_web_search_errors_stop_web_search(db, services):
    services.apollo_key = False
    ids = [company(db, f"Clinic {n} Dental").id for n in range(5)]
    services.web_error_after = 0
    services.web_error = SourceError("Brave Search is not answering properly (HTTP 500). Try again later.")
    job = run_people(db, ids)
    assert job.status == "done"
    assert job.processed == worker.WEB_SEARCH_MAX_FAILURES
    assert "3 searches failed" in job.progress_message
    assert "Web search stopped: Brave Search is not answering" in job.progress_message


def test_nothing_configured_fails_the_job(db, services):
    services.web_key = services.apollo_key = False
    a = company(db, "Alpha Dental")
    job = run_people(db, [a.id])
    assert job.status == "failed" and "BRAVE_API_KEY" in job.error
    services.apollo_key = True
    job = run_people(db, [a.id], use_apollo=False)
    assert job.status == "failed"


def test_interrupted_people_job_is_failed_not_rerun(db):
    job = SearchJob(kind="people", query="q", location="", source="people", status="running")
    db.add(job)
    db.commit()
    worker.recover_jobs()
    db.expire_all()
    job = db.get(SearchJob, job.id)
    assert job.status == "failed" and "owner lookup again" in job.error and job.finished_at is not None


# ----- the endpoint ----------------------------------------------------------------------


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(config, "BRAVE_API_KEY", "k")
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")


def test_find_people_endpoint(client, db, keys):
    beta = company(db, "Beta")
    alpha = company(db, "Alpha")
    r = client.post("/api/companies/find-people", json={"company_ids": [beta.id, alpha.id, 9999]})
    assert r.status_code == 201, r.text
    job = r.json()
    assert (job["kind"], job["source"], job["status"], job["query"]) == ("people", "people", "queued",
                                                                         "2 selected companies")
    assert job["params"] == {"company_ids": sorted([alpha.id, beta.id]), "use_apollo": True, "find_emails": True,
                             "force": False}

    r = client.post("/api/companies/find-people", json={"company_ids": [alpha.id], "use_apollo": False,
                                                        "find_emails": False})
    assert r.json()["params"] == {"company_ids": [alpha.id], "use_apollo": False, "find_emails": False,
                                  "force": False}
    assert r.json()["query"] == "1 selected company"

    assert client.post("/api/companies/find-people", json={"company_ids": [9999]}).status_code == 400
    assert client.post("/api/companies/find-people", json={"company_ids": []}).status_code == 422
    assert client.post("/api/companies/find-people", json={"company_ids": list(range(1, 502))}).status_code == 422


def test_find_people_endpoint_needs_a_key(client, db, monkeypatch):
    alpha = company(db, "Alpha")
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    monkeypatch.setattr(config, "SERPER_API_KEY", "")
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")
    r = client.post("/api/companies/find-people", json={"company_ids": [alpha.id]})
    assert r.status_code == 400 and "BRAVE_API_KEY" in r.json()["detail"]
    monkeypatch.setattr(config, "APOLLO_API_KEY", "a")
    assert client.post("/api/companies/find-people", json={"company_ids": [alpha.id]}).status_code == 201
    r = client.post("/api/companies/find-people", json={"company_ids": [alpha.id], "use_apollo": False})
    assert r.status_code == 400


def test_usage_lists_web_search_and_apollo(client, monkeypatch, keys):
    rows = {u["provider"]: u for u in client.get("/api/usage").json()}
    assert rows["web_search"]["configured"] is True and rows["apollo"]["configured"] is False
    assert rows["web_search"]["limit"] == config.WEB_SEARCH_MONTHLY_LIMIT
    assert rows["apollo"]["limit"] == config.APOLLO_MONTHLY_LIMIT


# ----- not spending searches twice (lead's follow-up) -----------------------------------


def test_recently_looked_up_business_is_skipped_and_company_page_search_is_off(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import config, worker
    from app.models import Company, SearchJob

    calls = []
    monkeypatch.setattr(worker.websearch, "available", lambda: True)
    monkeypatch.setattr(worker.websearch, "search", lambda q, n=10: calls.append(q) or [])
    monkeypatch.setattr(worker.apollo, "available", lambda: False)
    monkeypatch.setattr(config, "LINKEDIN_COMPANY_PAGE_SEARCH", False)
    fresh = Company(name="Smile Dental", city="Austin", source="osm", source_ref="1")
    recent = Company(name="Bright Dental", city="Austin", source="osm", source_ref="2",
                     people_searched_at=datetime.now(timezone.utc) - timedelta(days=5))
    db.add_all([fresh, recent])
    db.commit()

    def run(force=False):
        job = SearchJob(kind="people", query="2 selected companies", location="", source="people",
                        params={"company_ids": [fresh.id, recent.id], "use_apollo": False,
                                "find_emails": False, "force": force})
        db.add(job)
        db.commit()
        worker.run_job(job.id)
        db.expire_all()

    run()
    assert len(calls) == 1  # only the fresh one, and one search each (no company-page search)
    assert db.get(Company, fresh.id).people_searched_at is not None
    run()
    assert len(calls) == 1  # both looked up recently now
    run(force=True)
    assert len(calls) == 3
