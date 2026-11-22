"""BBB facts from web search results (app/bbb.py), the "Check BBB" job and its endpoint.
The web search is a small fake on the worker module (no network)."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import bbb, worker
from app.intent import estimate_intent
from app.models import Company, SearchJob
from app.usage import UsageLimitError

URL = "https://www.bbb.org/us/tx/austin/profile/dentist/smile-dental-0825-1000130628"


def us_company(name="Smile Dental", city="Austin", address="100 Main St, Austin, TX 78704, USA", **kw):
    return Company(name=name, source="google", source_ref=name, city=city, address=address, country="US", **kw)


def result(title="Smile Dental | Better Business Bureau® Profile", url=URL, snippet=""):
    return {"title": title, "url": url, "snippet": snippet}


# ----- reading results ----------------------------------------------------------------


def test_query():
    assert bbb.query(us_company()) == "site:bbb.org Smile Dental Austin TX"


def test_reads_accredited_profile():
    snippet = ("BBB accredited since 5/30/2008. Dentist in Austin, TX. BBB Rating: A+. "
               "4 complaints closed in last 3 years. Business Started: 1/1/1998.")
    facts = bbb.parse_results([result(snippet=snippet)], us_company())
    assert facts == {"found": True, "url": URL, "name": "Smile Dental", "accredited": True,
                     "accredited_since": "5/30/2008", "rating": "A+", "complaints": 4, "started": 1998}


def test_real_serper_snippets():
    """Snippets as Serper (Google) returned them."""
    glued = result(snippet="Dentist in Austin, TX. (512) 345-0400. BBB Rating A+More · 800 Austin TX 78701")
    assert bbb.parse_results([glued], us_company())["rating"] == "A+"
    since = result(snippet="BBB Accredited since 6/12/2007. Phone: (512) 345-9973. Dentist in Austin, TX. "
                           "See BBB rating, reviews, complaints, get a quote and ...")
    facts = bbb.parse_results([since], us_company())
    assert facts["accredited"] is True and facts["accredited_since"] == "6/12/2007" and facts["rating"] is None
    not_acc = result(snippet="Smile Dental is NOT a BBB Accredited Business. To become accredited, a business must "
                             "agree to BBB Standards for Trust")
    assert bbb.parse_results([not_acc], us_company())["accredited"] is False
    assert bbb.parse_results([result(snippet="BBB Rating Average service")], us_company())["rating"] is None


def test_not_accredited_and_not_rated():
    snippet = "This business is not BBB Accredited. Not rated. No complaints. Dentist in Austin, TX."
    facts = bbb.parse_results([result(snippet=snippet)], us_company())
    assert facts["accredited"] is False and facts["rating"] == "NR" and facts["complaints"] == 0


def test_unknown_facts_stay_none():
    facts = bbb.parse_results([result(snippet="See BBB rating, reviews, complaints, & more.")], us_company())
    assert facts["found"] is True
    assert facts["accredited"] is None and facts["rating"] is None and facts["complaints"] is None
    # "a rating" in a sentence is not a grade A
    facts = bbb.parse_results([result(snippet="Leave a rating for this business.")], us_company())
    assert facts["rating"] is None


def test_needs_the_same_name_and_state():
    company = us_company()
    other = result(title="Smile Dental Studio | BBB", url=URL.replace("smile-dental-", "smile-dental-studio-"))
    assert bbb.parse_results([other], company) == {"found": False}
    elsewhere = result(url=URL.replace("/us/tx/austin/", "/us/ca/fresno/"))
    assert bbb.parse_results([elsewhere], company) == {"found": False}
    not_profile = result(url="https://www.bbb.org/us/tx/austin/category/dentist")
    assert bbb.parse_results([not_profile], company) == {"found": False}
    # legal words don't matter; a sub-page of the profile counts as the profile
    llc = result(title="Smile Dental, PLLC | BBB", url=URL + "/complaints", snippet="BBB Rating: C-")
    facts = bbb.parse_results([llc], company)
    assert facts["found"] and facts["url"] == URL and facts["rating"] == "C-"


def test_prefers_the_same_city():
    round_rock = result(url=URL.replace("/austin/", "/round-rock/").replace("1000130628", "1000999999"),
                        snippet="BBB Rating: F")
    austin = result(snippet="BBB Rating: A")
    facts = bbb.parse_results([round_rock, austin], us_company())
    assert facts["rating"] == "A" and "/austin/" in facts["url"]


def test_talking_points_and_intent():
    company = us_company(bbb={"found": True, "rating": "D", "complaints": 5, "accredited": True,
                              "accredited_since": "2015"})
    assert bbb.talking_points(company.bbb) == ["BBB rating D", "5 BBB complaints", "Pays for BBB accreditation since 2015"]
    reasons = estimate_intent(company)["marketing"]["reasons"]
    assert "BBB rating D and 5 BBB complaints +10" in reasons
    fine = us_company(bbb={"found": True, "rating": "A+", "complaints": 1})
    assert bbb.talking_points(fine.bbb) == []
    assert not any("BBB" in r for r in estimate_intent(fine)["marketing"]["reasons"])
    assert bbb.talking_points({"found": False}) == []


# ----- the job --------------------------------------------------------------------------


class FakeSearch:
    def __init__(self):
        self.queries: list[str] = []
        self.results: list[dict] = []
        self.error: Exception | None = None

    def module(self):
        def search(query, count=10):
            if self.error:
                raise self.error
            self.queries.append(query)
            return self.results
        return SimpleNamespace(available=lambda: True, search=search)


@pytest.fixture
def web(monkeypatch):
    fake = FakeSearch()
    monkeypatch.setattr(worker, "websearch", fake.module())
    return fake


def run_bbb(db, ids, **params) -> SearchJob:
    job = SearchJob(kind="bbb", query="x", location="", source="bbb", limit=len(ids),
                    params={"company_ids": ids, **params})
    db.add(job)
    db.commit()
    worker.run_bbb_job(db, job)
    db.commit()
    return job


def test_job_saves_facts_and_skips_recent_and_non_us(db, web):
    web.results = [result(snippet="BBB Rating: D. 6 complaints.")]
    smile = us_company()
    karachi = Company(name="Smile Dental PK", source="osm", source_ref="pk", city="Karachi", country="Pakistan")
    db.add_all([smile, karachi])
    db.commit()

    job = run_bbb(db, [smile.id, karachi.id])
    assert job.status == "done" and job.result_count == 1
    assert smile.bbb["rating"] == "D" and smile.bbb_checked_at is not None
    assert any("BBB" in r for r in smile.intent["marketing"]["reasons"])  # rescored
    assert karachi.bbb == {} and "1 not in the us" in job.progress_message.lower()
    assert len(web.queries) == 1

    again = run_bbb(db, [smile.id])
    assert len(web.queries) == 1 and again.result_count == 1  # checked recently: kept
    run_bbb(db, [smile.id], force=True)
    assert len(web.queries) == 2

    smile.bbb_checked_at -= timedelta(days=400)
    db.commit()
    run_bbb(db, [smile.id])
    assert len(web.queries) == 3


def test_job_stops_at_the_monthly_limit(db, web):
    web.error = UsageLimitError("Monthly Web search limit reached (2/2).")
    companies = [us_company(name=f"Biz {i}") for i in range(3)]
    db.add_all(companies)
    db.commit()
    job = run_bbb(db, [c.id for c in companies])
    assert job.status == "done" and job.processed == 1
    assert "monthly limit" in job.progress_message


def test_endpoint(client, db, monkeypatch):
    c = us_company()
    db.add(c)
    db.commit()
    from app.routers import companies as router

    monkeypatch.setattr(router.websearch, "available", lambda: False)
    assert client.post("/api/companies/check-bbb", json={"company_ids": [c.id]}).status_code == 400
    monkeypatch.setattr(router.websearch, "available", lambda: True)
    r = client.post("/api/companies/check-bbb", json={"company_ids": [c.id], "force": True})
    assert r.status_code == 201
    job = db.scalars(select(SearchJob)).one()
    assert job.kind == "bbb" and job.params == {"company_ids": [c.id], "force": True}
    out = client.get(f"/api/companies/{c.id}").json()
    assert out["bbb"] == {} and out["bbb_checked_at"] is None
