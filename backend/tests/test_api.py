"""HTTP API tests (no background worker, no network). Fixtures `client` and `db` come
from conftest.py: every test starts with an empty temporary database."""
import csv
import io

import pytest

from app.models import Company, Contact, JobResult, SearchJob
from app.sources import SOURCES, Source


class FakeSource(Source):
    def __init__(self, name="fake", reason=None):
        self.name, self.label, self.description = name, f"Fake {name}", "For tests"
        self._reason = reason

    def unavailable_reason(self):
        return self._reason

    def search(self, query, location, limit, progress):
        return []


@pytest.fixture
def fake_sources(monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", FakeSource())
    monkeypatch.setitem(SOURCES, "locked", FakeSource("locked", reason="Add GOOGLE_PLACES_API_KEY to backend/.env"))


def add_job(db, **kw) -> SearchJob:
    fields = dict(kind="search", query="dentist", location="Karachi", source="osm", status="done")
    fields.update(kw)
    job = SearchJob(**fields)
    db.add(job)
    db.commit()
    return job


def add_company(db, name, job=None, **kw) -> Company:
    c = Company(name=name, source="osm", source_ref=name, **kw)
    db.add(c)
    db.flush()
    if job is not None:
        db.add(JobResult(job_id=job.id, company_id=c.id))
    db.commit()
    return c


# ----- sources & jobs ----------------------------------------------------------------


def test_sources_show_availability(client, fake_sources):
    sources = {s["name"]: s for s in client.get("/api/sources").json()}
    assert list(sources)[0] == "osm"  # OpenStreetMap stays the default
    assert sources["fake"]["available"] is True and sources["fake"]["unavailable_reason"] is None
    assert sources["locked"]["available"] is False
    assert "GOOGLE_PLACES_API_KEY" in sources["locked"]["unavailable_reason"]


def test_create_job_with_enrich_flag(client, fake_sources):
    r = client.post("/api/jobs", json={"query": " dentist ", "location": "Lahore", "source": "fake", "enrich": True})
    assert r.status_code == 201
    job = r.json()
    assert job["kind"] == "search" and job["enrich"] is True and job["status"] == "queued"
    assert job["query"] == "dentist" and job["parent_job_id"] is None
    assert job["progress_message"] == "Waiting in queue..."
    # enrich defaults to False
    r = client.post("/api/jobs", json={"query": "dentist", "location": "Lahore", "source": "fake"})
    assert r.json()["enrich"] is False


def test_create_job_rejects_unknown_or_unavailable_source(client, fake_sources):
    r = client.post("/api/jobs", json={"query": "dentist", "location": "Lahore", "source": "nope"})
    assert r.status_code == 400 and "Unknown source" in r.json()["detail"]
    r = client.post("/api/jobs", json={"query": "dentist", "location": "Lahore", "source": "locked"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Add GOOGLE_PLACES_API_KEY to backend/.env"


def test_list_jobs_newest_first_and_get(client, db):
    a, b = add_job(db), add_job(db, query="plumber")
    ids = [j["id"] for j in client.get("/api/jobs").json()]
    assert ids == [b.id, a.id]
    assert client.get(f"/api/jobs/{a.id}").json()["query"] == "dentist"
    assert client.get("/api/jobs/9999").status_code == 404


def test_enrich_search_job(client, db):
    search = add_job(db, limit=50)
    r = client.post(f"/api/jobs/{search.id}/enrich")  # body is optional
    assert r.status_code == 201
    job = r.json()
    assert job["kind"] == "enrich" and job["source"] == "crawler" and job["status"] == "queued"
    assert job["parent_job_id"] == search.id
    assert (job["query"], job["location"]) == ("dentist", "Karachi")
    db.expire_all()
    assert db.get(SearchJob, job["id"]).params == {"force": False}

    r = client.post(f"/api/jobs/{search.id}/enrich", json={"force": True})
    db.expire_all()
    assert db.get(SearchJob, r.json()["id"]).params == {"force": True}


def test_enrich_job_rules(client, db):
    assert client.post("/api/jobs/9999/enrich").status_code == 404
    running = add_job(db, status="running")
    assert client.post(f"/api/jobs/{running.id}/enrich").status_code == 400
    failed = add_job(db, status="failed")
    assert client.post(f"/api/jobs/{failed.id}/enrich").status_code == 400
    enrich = add_job(db, kind="enrich", source="crawler")
    assert client.post(f"/api/jobs/{enrich.id}/enrich").status_code == 400


def test_enrich_selected_companies(client, db):
    a, b = add_company(db, "A"), add_company(db, "B")
    r = client.post("/api/companies/enrich", json={"company_ids": [b.id, a.id, 9999], "force": True})
    assert r.status_code == 201
    job = r.json()
    assert job["kind"] == "enrich" and job["source"] == "crawler" and job["parent_job_id"] is None
    db.expire_all()
    assert db.get(SearchJob, job["id"]).params == {"company_ids": [a.id, b.id], "force": True}

    assert client.post("/api/companies/enrich", json={"company_ids": [9999]}).status_code == 400
    assert client.post("/api/companies/enrich", json={"company_ids": []}).status_code == 422


# ----- companies ---------------------------------------------------------------------


@pytest.fixture
def sample(db):
    """Three companies from one search: one crawled with a contact, one failed, one new."""
    job = add_job(db)
    crawled = add_company(
        db, "Alpha Dental", job, website="https://alpha.pk", email="info@alpha.pk",
        emails=["info@alpha.pk", "dr.ali@alpha.pk"], phone="+92 21 111", rating=4.5, reviews_count=12,
        google_maps_url="https://maps.google.com/?cid=1", business_status="OPERATIONAL",
        socials={"facebook": "https://facebook.com/alpha"}, enrich_status="done",
        website_signals={"https": True, "platform": "WordPress", "copyright_year": 2019},
    )
    db.add(Contact(company_id=crawled.id, full_name="Ali Khan", title="Dentist", email="dr.ali@alpha.pk",
                   source="website"))
    failed = add_company(db, "Beta Clinic", job, website="https://beta.pk", enrich_status="failed",
                         enrich_error="Timed out")
    fresh = add_company(db, "Gamma Care", job)
    add_company(db, "Not In Job")
    db.commit()
    return job, crawled, failed, fresh


def names(resp) -> list[str]:
    return [c["name"] for c in resp.json()["items"]]


def test_companies_list_filters(client, sample):
    job, *_ = sample
    base = f"/api/companies?job_id={job.id}"
    assert client.get(base).json()["total"] == 3
    assert names(client.get(base + "&has_email=true")) == ["Alpha Dental"]
    assert names(client.get(base + "&has_email=false")) == ["Beta Clinic", "Gamma Care"]
    assert names(client.get(base + "&enrich_status=failed")) == ["Beta Clinic"]
    assert names(client.get(base + "&enrich_status=none")) == ["Gamma Care"]
    assert names(client.get(base + "&has_website=true&enrich_status=done")) == ["Alpha Dental"]
    assert client.get("/api/companies").json()["total"] == 4


def test_companies_include_contacts(client, sample):
    _, crawled, *_ = sample
    items = {c["name"]: c for c in client.get("/api/companies").json()["items"]}
    alpha = items["Alpha Dental"]
    assert alpha["emails"] == ["info@alpha.pk", "dr.ali@alpha.pk"]
    assert alpha["website_signals"]["platform"] == "WordPress"
    assert [c["full_name"] for c in alpha["contacts"]] == ["Ali Khan"]
    assert alpha["contacts"][0]["email"] == "dr.ali@alpha.pk"
    assert items["Gamma Care"]["contacts"] == []

    one = client.get(f"/api/companies/{crawled.id}").json()
    assert one["name"] == "Alpha Dental" and one["contacts"][0]["title"] == "Dentist"
    assert client.get("/api/companies/9999").status_code == 404


def test_export_csv(client, sample):
    job, *_ = sample
    r = client.get(f"/api/companies/export.csv?job_id={job.id}")
    assert r.status_code == 200
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")  # Excel needs the BOM for Urdu/Hindi names
    rows = list(csv.DictReader(io.StringIO(text[1:])))
    for column in ("emails", "rating", "reviews_count", "google_maps_url", "business_status",
                   "platform", "https", "copyright_year", "contacts"):
        assert column in rows[0], column
    alpha = rows[0]
    assert alpha["name"] == "Alpha Dental"
    assert alpha["emails"] == "info@alpha.pk; dr.ali@alpha.pk"
    assert alpha["platform"] == "WordPress" and alpha["https"] == "yes" and alpha["copyright_year"] == "2019"
    assert alpha["contacts"] == "Ali Khan (Dentist) <dr.ali@alpha.pk>"
    assert alpha["facebook"] == "https://facebook.com/alpha"
    assert alpha["rating"] == "4.5" and alpha["business_status"] == "OPERATIONAL"
    assert [r["name"] for r in rows] == ["Alpha Dental", "Beta Clinic", "Gamma Care"]
    # filters apply to the export too
    r = client.get(f"/api/companies/export.csv?job_id={job.id}&enrich_status=failed")
    assert [r["name"] for r in csv.DictReader(io.StringIO(r.content.decode("utf-8")[1:]))] == ["Beta Clinic"]


def test_usage_endpoint(client):
    from app import usage

    usage.consume("google_places", 3)
    rows = {u["provider"]: u for u in client.get("/api/usage").json()}
    google = rows["google_places"]
    assert google["count"] == 3
    assert google["limit"] == usage.PROVIDERS["google_places"]
    assert google["period"] == usage.current_period()


# ----- Step 4-6: find emails + titles -------------------------------------------------


def test_create_job_find_emails_implies_enrich_and_stores_titles(client, db, fake_sources):
    r = client.post("/api/jobs", json={
        "query": "dentist", "location": "Lahore", "source": "fake", "find_emails": True,
        "titles": ["  Office   Manager ", "OWNER", "", "owner"],
    })
    assert r.status_code == 201
    job = r.json()
    assert job["enrich"] is True
    assert job["params"] == {"find_emails": True, "titles": ["office manager", "owner"]}

    r = client.post("/api/jobs", json={"query": "dentist", "location": "Lahore", "source": "fake"})
    assert r.json()["enrich"] is False
    assert r.json()["params"] == {"find_emails": False, "titles": []}


def test_find_emails_for_search_job(client, db):
    search = add_job(db, limit=40)
    r = client.post(f"/api/jobs/{search.id}/find-emails")  # body is optional
    assert r.status_code == 201
    job = r.json()
    assert (job["kind"], job["source"], job["status"]) == ("emails", "emails", "queued")
    assert job["parent_job_id"] == search.id
    assert (job["query"], job["location"], job["limit"]) == ("dentist", "Karachi", 40)
    assert job["params"] == {"force": False, "use_hunter": True}

    r = client.post(f"/api/jobs/{search.id}/find-emails", json={"force": True, "use_hunter": False})
    assert r.json()["params"] == {"force": True, "use_hunter": False}


def test_find_emails_for_job_rules(client, db):
    assert client.post("/api/jobs/9999/find-emails").status_code == 404
    running = add_job(db, status="running")
    assert client.post(f"/api/jobs/{running.id}/find-emails").status_code == 400
    crawl = add_job(db, kind="enrich", source="crawler")
    assert client.post(f"/api/jobs/{crawl.id}/find-emails").status_code == 400


def test_usage_says_whether_hunter_is_configured(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "HUNTER_API_KEY", "")
    rows = {u["provider"]: u for u in client.get("/api/usage").json()}
    assert rows["hunter"]["configured"] is False
    monkeypatch.setattr(config, "HUNTER_API_KEY", "test-key")
    rows = {u["provider"]: u for u in client.get("/api/usage").json()}
    assert rows["hunter"]["configured"] is True
