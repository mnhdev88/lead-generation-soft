"""'What are you selling?': searches that remember the services picked, results filtered by
several services, the intent summary of a search and PageSpeed testing those first."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import config, pagespeed
from app.models import Company, JobResult
from app.routers.companies import filters, parse_looking_for, reason_key
from app.scoring import rescore

from .test_api import add_job, fake_sources  # noqa: F401  (fake_sources is a fixture)
from .test_intent import THIS_YEAR, company, site
from .test_pagespeed import FakePSI


# ----- searches -------------------------------------------------------------------------


def test_batch_keeps_offers_and_crawls(client, fake_sources):  # noqa: F811
    r = client.post("/api/jobs/batch", json={
        "queries": ["dentist"], "locations": ["Austin"], "source": "fake",
        "offers": ["seo", "website", "seo"],
    })
    assert r.status_code == 201
    job = r.json()[0]
    assert job["params"]["offers"] == ["website", "seo"]  # usual order, no repeats
    assert job["enrich"] is True  # every intent score needs the websites crawled

    plain = client.post("/api/jobs/batch", json={"queries": ["gym"], "locations": ["Austin"], "source": "fake"})
    assert plain.json()[0]["params"]["offers"] == [] and plain.json()[0]["enrich"] is False

    bad = client.post("/api/jobs/batch", json={"queries": ["gym"], "locations": ["Austin"], "source": "fake",
                                               "offers": ["plumbing"]})
    assert bad.status_code == 422


def test_single_job_keeps_offers(client, fake_sources):  # noqa: F811
    r = client.post("/api/jobs", json={"query": "dentist", "location": "Austin", "source": "fake",
                                       "offers": ["software"]})
    assert r.status_code == 201
    assert r.json()["params"]["offers"] == ["software"] and r.json()["enrich"] is True


# ----- several services at once -------------------------------------------------------


def test_parse_looking_for():
    assert parse_looking_for(None) == [] and parse_looking_for("any") == []
    assert parse_looking_for("seo,website,seo") == ["website", "seo"]
    with pytest.raises(ValueError):
        parse_looking_for("seo,nope")
    with pytest.raises(ValueError):
        filters(looking_for="seo,nope")  # the Google Sheets export calls it as a function


def _names(r):
    assert r.status_code == 200, r.text
    return [c["name"] for c in r.json()["items"]]


def test_filter_and_sort_by_several_services(client, db):
    db.add_all([
        company(name="NoSite", source_ref="1", website=None, domain=None),  # website 30, seo 0
        company(name="Slow", source_ref="2", website_signals=site(https=False, copyright_year=THIS_YEAR - 5),
                pagespeed={"performance": 20}),  # website 40
        company(name="Fine", source_ref="3", website_signals=site(), pagespeed={"performance": 90, "seo": 70}),
    ])
    db.commit()
    for c in db.scalars(select(Company)):
        rescore(c, branches=0)
    db.commit()
    scores = {c.name: (c.intent_website, c.intent_seo) for c in db.scalars(select(Company))}
    best = {name: max(s) for name, s in scores.items()}

    got = _names(client.get("/api/companies?sort=intent&looking_for=website,seo"))
    assert got == sorted(best, key=lambda n: (-best[n], n.lower()))
    assert _names(client.get("/api/companies?looking_for=website,seo&min_intent=31")) == sorted(
        n for n, b in best.items() if b >= 31)
    assert client.get("/api/companies?looking_for=website,,seo").status_code == 422
    assert client.get("/api/companies?looking_for=any,seo").status_code == 422


# ----- the summary of a search ----------------------------------------------------------


def test_reason_key():
    assert reason_key("Slow on mobile (PageSpeed 38) +15") == "Slow on mobile"
    assert reason_key("Rating 3.5 +15") == "Low Google rating"
    assert reason_key("Not in Google's top 10 for the search (position 14) +15") == "Not in Google's top 10 for the search"
    assert reason_key("No website: SEO at most 30") is None
    assert reason_key("Closed → 0") is None


def test_intent_summary(client, db):
    job = add_job(db)
    other = add_job(db)
    hot = {"score": 70, "reasons": ["No SSL (http only) +15", "Slow on mobile (PageSpeed 41) +15", "No website +30"]}
    rows = [
        ("A", {"website": hot, "seo": {"score": 20, "reasons": ["Missing description +15", "Rating 3.9 +5"]}}, job),
        ("B", {"website": {"score": 15, "reasons": ["Slow on mobile (PageSpeed 60) +8", "Closed → 0"]},
               "seo": {"score": 0, "reasons": []}}, job),
        ("C", {"website": {"score": 0, "reasons": []}, "seo": {"score": 65, "reasons": ["Missing description +15"]}},
         job),
        ("Elsewhere", {"website": hot}, other),
    ]
    for name, intent, j in rows:
        c = Company(name=name, source="osm", source_ref=name, intent=intent, website=f"https://{name}.example",
                    website_signals={"pages_crawled": 3} if name != "C" else {})
        db.add(c)
        db.flush()
        db.add(JobResult(job_id=j.id, company_id=c.id))
    db.commit()

    r = client.get(f"/api/companies/intent-summary?job_id={job.id}&looking_for=website,seo")
    assert r.status_code == 200, r.text
    s = r.json()
    assert (s["total"], s["any_hot"], s["website_not_read"], s["speed_pending"]) == (3, 2, 1, 3)
    website, seo = s["offers"]
    assert (website["offer"], website["hot"], website["warm"]) == ("website", 1, 1)
    assert website["top_reasons"][0] == {"reason": "Slow on mobile", "count": 2}
    assert {r["reason"] for r in website["top_reasons"]} == {"Slow on mobile", "No SSL", "No website"}
    assert (seo["offer"], seo["hot"], seo["warm"]) == ("seo", 1, 1)
    assert seo["top_reasons"][0] == {"reason": "Missing description", "count": 2}

    every = client.get(f"/api/companies/intent-summary?job_id={job.id}").json()
    assert [o["offer"] for o in every["offers"]] == ["website", "seo", "software", "marketing"]


# ----- PageSpeed tests Website / SEO searches first ------------------------------------


def _site(i, age_days):
    return Company(name=f"Clinic {i}", source="google", source_ref=f"p{i}", raw={},
                   website=f"https://site{i}.example.com",
                   created_at=datetime.now(timezone.utc) - timedelta(days=age_days))


@pytest.mark.parametrize("offers, first", [(["seo"], "site1"), (["software"], "site2"), ([], "site2")])
def test_pagespeed_tests_website_and_seo_searches_first(db, monkeypatch, offers, first):
    monkeypatch.setattr(config, "PAGESPEED_ENABLED", True)
    monkeypatch.setattr(config, "PAGESPEED_API_KEY", "")
    new, old = _site(1, age_days=0), _site(2, age_days=9)
    db.add_all([new, old])
    db.commit()
    job = add_job(db, params={"offers": offers})
    db.add(JobResult(job_id=job.id, company_id=new.id))
    db.commit()

    fake = FakePSI()
    assert pagespeed.check_saved_companies(db, limit=1, transport=fake.transport) == 1
    assert fake.requests[0].url.params["url"] == f"https://{first}.example.com"
