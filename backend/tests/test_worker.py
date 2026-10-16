"""Worker tests: search jobs and website-crawl (enrich) jobs.
The real crawler is replaced by a fake `crawl_website`, so no network is used."""
import asyncio
import threading

import pytest
from sqlalchemy import select

from app import worker
from app.enrich import PersonFound, WebsiteData
from app.models import Company, Contact, JobResult, SearchJob
from app.sources import SOURCES, CompanyResult, Source


def site(emails=(), phones=(), socials=None, people=(), ok=True, error=None, **signals) -> WebsiteData:
    return WebsiteData(
        ok=ok,
        error=error,
        final_url=None,
        emails=list(emails),
        phones=list(phones),
        socials=dict(socials or {}),
        people=list(people),
        # employees_stated: today's crawler always sets the size keys (None = not stated)
        signals={"https": True, "platform": None, "pages_crawled": 3, "employees_stated": None, **signals},
    )


class FakeCrawler:
    """Stands in for crawl_website: answers from a {url: WebsiteData | Exception} dict."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, url, client=None, max_pages=None):
        self.calls.append(url)
        answer = self.answers.get(url, site())
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def crawler(monkeypatch):
    def install(answers):
        fake = FakeCrawler(answers)
        monkeypatch.setattr(worker, "crawl_website", fake)
        return fake

    return install


def make_search(db, *companies: Company) -> SearchJob:
    job = SearchJob(kind="search", query="dentist", location="Karachi", source="osm", status="done")
    db.add(job)
    db.add_all(companies)
    db.flush()
    for c in companies:
        db.add(JobResult(job_id=job.id, company_id=c.id))
    db.commit()
    return job


def company(name, **kw) -> Company:
    return Company(name=name, source="osm", source_ref=name, **kw)


def enrich(db, parent=None, **params) -> SearchJob:
    """Create an enrich job and run it like the worker thread would."""
    job = SearchJob(kind="enrich", query="q", location="l", source="crawler",
                    parent_job_id=parent.id if parent else None, params=params)
    db.add(job)
    db.commit()
    worker.run_job(job.id)
    db.expire_all()
    return db.get(SearchJob, job.id)


# ----- enrich jobs -------------------------------------------------------------------


def test_enrich_saves_emails_phone_socials_signals(db, crawler):
    alpha = company("Alpha", website="https://alpha.pk", email="Owner@Alpha.pk", emails=["owner@alpha.pk"],
                    phone="+92 1", socials={"facebook": "https://facebook.com/old"})
    beta = company("Beta", website="https://beta.pk")
    search = make_search(db, alpha, beta)
    crawler({
        "https://alpha.pk": site(
            emails=["info@alpha.pk", "owner@alpha.pk", "alpha@gmail.com"], phones=["+92 2"],
            socials={"facebook": "https://facebook.com/new", "instagram": "https://instagram.com/alpha"},
            platform="WordPress", copyright_year=2018,
        ),
        "https://beta.pk": site(emails=["sales@beta.pk", "beta@gmail.com"], phones=["+92 3", "+92 4"]),
    })

    job = enrich(db, search)
    a, b = db.get(Company, alpha.id), db.get(Company, beta.id)

    # existing emails first, then the crawler's list in its (best-first) order, no duplicates
    assert a.emails == ["owner@alpha.pk", "info@alpha.pk", "alpha@gmail.com"]
    assert a.email == "Owner@Alpha.pk"  # had one already: unchanged
    assert a.phone == "+92 1"  # had one already: unchanged
    assert a.socials == {"facebook": "https://facebook.com/old", "instagram": "https://instagram.com/alpha"}
    assert a.website_signals["platform"] == "WordPress" and a.website_signals["copyright_year"] == 2018
    assert a.enrich_status == "done" and a.enrich_error is None and a.enriched_at is not None

    assert b.emails == ["sales@beta.pk", "beta@gmail.com"]
    assert b.email == "sales@beta.pk"  # was empty: filled with the best email
    assert b.phone == "+92 3"

    assert job.status == "done" and job.error is None
    assert (job.total, job.processed, job.result_count) == (2, 2, 2)
    assert job.progress_message == "Crawled 2 websites: 2 with emails, 0 people found, 0 failed."


def test_contacts_saved_and_not_duplicated_on_recrawl(db, crawler):
    shop = company("Shop", website="https://shop.pk")
    search = make_search(db, shop)
    people = [
        PersonFound(full_name="Dr. Ayesha Khan", title="Founder", email="Ayesha@shop.pk", source_url="https://shop.pk/team"),
        PersonFound(full_name="Bilal Ahmed", title="Manager"),
    ]
    crawler({"https://shop.pk": site(emails=["info@shop.pk"], people=people)})
    job = enrich(db, search)
    assert job.progress_message.startswith("Crawled 1 websites: 1 with emails, 2 people found")

    # second crawl finds the same people (one now with an email) plus a new one
    people2 = [
        PersonFound(full_name="dr. ayesha khan", email="ayesha@shop.pk"),
        PersonFound(full_name="Bilal Ahmed", email="bilal@shop.pk", phone="+92 5"),
        PersonFound(full_name="Sara Malik", title="Receptionist"),
    ]
    crawler({"https://shop.pk": site(emails=["info@shop.pk"], people=people2)})
    enrich(db, search, force=True)

    contacts = {c.full_name: c for c in db.scalars(select(Contact).where(Contact.company_id == shop.id))}
    assert sorted(contacts) == ["Bilal Ahmed", "Dr. Ayesha Khan", "Sara Malik"]
    ayesha = contacts["Dr. Ayesha Khan"]
    assert (ayesha.first_name, ayesha.last_name) == ("Ayesha", "Khan")
    assert ayesha.email == "ayesha@shop.pk" and ayesha.email_source == "website"
    assert ayesha.title == "Founder" and ayesha.source == "website"
    assert ayesha.source_url == "https://shop.pk/team"
    bilal = contacts["Bilal Ahmed"]
    assert (bilal.email, bilal.phone, bilal.title) == ("bilal@shop.pk", "+92 5", "Manager")
    assert contacts["Sara Malik"].email is None and contacts["Sara Malik"].email_source is None


def test_no_website_and_skip_already_crawled(db, crawler):
    with_site = company("WithSite", website="https://a.pk")
    without = company("NoSite")
    search = make_search(db, with_site, without)
    fake = crawler({"https://a.pk": site(emails=["info@a.pk"])})

    job = enrich(db, search)
    assert fake.calls == ["https://a.pk"]
    assert db.get(Company, without.id).enrich_status == "no_website"
    assert (job.total, job.processed, job.result_count) == (2, 2, 1)
    assert "1 without a website" in job.progress_message

    # Run again: the crawled company is skipped, only the no-website one is re-checked.
    job = enrich(db, search)
    assert fake.calls == ["https://a.pk"]
    assert (job.total, job.processed) == (1, 1)
    assert "1 already crawled, skipped" in job.progress_message

    # force=True crawls it again
    job = enrich(db, search, force=True)
    assert fake.calls == ["https://a.pk", "https://a.pk"]
    assert job.total == 2


def test_failures_recorded_and_job_continues(db, crawler):
    good = company("Good", website="https://good.pk")
    down = company("Down", website="https://down.pk", email="kept@down.pk")
    buggy = company("Buggy", website="https://buggy.pk")
    search = make_search(db, good, down, buggy)
    crawler({
        "https://good.pk": site(emails=["info@good.pk"]),
        "https://down.pk": site(ok=False, error="Website did not respond (timeout)"),
        "https://buggy.pk": RuntimeError("boom"),
    })

    job = enrich(db, search)
    assert job.status == "done"
    assert (job.total, job.processed, job.result_count) == (3, 3, 1)
    assert job.progress_message.endswith("1 with emails, 0 people found, 2 failed.")

    d, b = db.get(Company, down.id), db.get(Company, buggy.id)
    assert d.enrich_status == "failed" and d.enrich_error == "Website did not respond (timeout)"
    assert d.email == "kept@down.pk"  # a failed crawl never removes data
    assert b.enrich_status == "failed" and "boom" in b.enrich_error
    assert db.get(Company, good.id).email == "info@good.pk"


def test_enrich_selected_company_ids(db, crawler):
    a, b = company("A", website="https://a.pk"), company("B", website="https://b.pk")
    db.add_all([a, b])
    db.commit()
    fake = crawler({})
    job = enrich(db, company_ids=[b.id])
    assert fake.calls == ["https://b.pk"]
    assert job.total == 1 and db.get(Company, a.id).enrich_status is None


def test_crawls_run_concurrently_with_a_limit(db, monkeypatch):
    companies = [company(f"C{i}", website=f"https://c{i}.pk") for i in range(6)]
    search = make_search(db, *companies)
    state = {"now": 0, "max": 0}

    async def slow_crawl(url, client=None, max_pages=None):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return site(emails=[f"info@{url[8:]}"])

    monkeypatch.setattr(worker, "crawl_website", slow_crawl)
    monkeypatch.setattr(worker, "CRAWL_CONCURRENCY", 2)
    job = enrich(db, search)
    assert state["max"] == 2
    assert job.result_count == 6 and job.processed == 6


# ----- search jobs -------------------------------------------------------------------


class FakeSource(Source):
    name, label, description = "fake", "Fake", "For tests"

    def __init__(self, results=(), reason=None):
        self.results, self.reason = list(results), reason

    def unavailable_reason(self):
        return self.reason

    def search(self, query, location, limit, progress):
        progress("Searching...")
        return self.results


def run_search(db, enrich_flag: bool) -> SearchJob:
    job = SearchJob(kind="search", query="dentist", location="Karachi", source="fake", enrich=enrich_flag)
    db.add(job)
    db.commit()
    worker.run_job(job.id)
    db.expire_all()
    return db.get(SearchJob, job.id)


def test_search_with_enrich_queues_crawl(db, monkeypatch):
    results = [CompanyResult(name="A", source_ref="a", website="a.pk"), CompanyResult(name="B", source_ref="b")]
    monkeypatch.setitem(SOURCES, "fake", FakeSource(results))

    job = run_search(db, enrich_flag=True)
    assert job.status == "done" and job.result_count == 2
    queued = db.scalars(select(SearchJob).where(SearchJob.kind == "enrich")).all()
    assert len(queued) == 1
    e = queued[0]
    assert (e.parent_job_id, e.source, e.status) == (job.id, "crawler", "queued")
    assert (e.query, e.location, e.progress_message) == ("dentist", "Karachi", "Waiting in queue...")

    run_search(db, enrich_flag=False)
    assert len(db.scalars(select(SearchJob).where(SearchJob.kind == "enrich")).all()) == 1


def test_search_without_results_queues_nothing(db, monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", FakeSource([]))
    run_search(db, enrich_flag=True)
    assert db.scalars(select(SearchJob).where(SearchJob.kind == "enrich")).all() == []


def test_search_fails_when_source_unavailable(db, monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", FakeSource(reason="No API key"))
    job = run_search(db, enrich_flag=True)
    assert job.status == "failed" and job.error == "No API key"


def test_new_search_keeps_crawled_email(db, monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", FakeSource([CompanyResult(name="A", source_ref="a", website="a.pk")]))
    run_search(db, enrich_flag=False)
    c = db.scalar(select(Company))
    c.email, c.emails, c.phone = "info@a.pk", ["info@a.pk"], "+92 1"
    db.commit()
    run_search(db, enrich_flag=False)
    db.expire_all()
    c = db.scalar(select(Company))
    assert (c.email, c.emails, c.phone) == ("info@a.pk", ["info@a.pk"], "+92 1")


def test_interrupted_jobs_are_requeued(db):
    job = SearchJob(kind="enrich", query="q", location="l", source="crawler", status="running")
    db.add(job)
    db.commit()
    worker.recover_jobs()
    db.expire_all()
    assert db.get(SearchJob, job.id).status == "queued"


def test_missing_job_is_ignored(db):
    worker.run_job(424242)  # must not raise


# ----- fixes from the Steps 2-3 review --------------------------------------------------


def test_failed_recrawl_keeps_website_signals(db, crawler):
    shop = company("Shop", website="https://shop.pk", enrich_status="done",
                   website_signals={"https": True, "platform": "WordPress", "copyright_year": 2020})
    search = make_search(db, shop)
    crawler({"https://shop.pk": site(ok=False, error="Website timed out", https=False)})
    enrich(db, search, force=True)
    s = db.get(Company, shop.id)
    assert s.enrich_status == "failed" and s.enrich_error == "Website timed out"
    assert s.website_signals == {"https": True, "platform": "WordPress", "copyright_year": 2020}


def test_crawl_classifies_people_and_rescores(db, crawler, monkeypatch):
    from app.people import Classification

    rescored = []
    monkeypatch.setattr(worker, "classify", lambda title: Classification("owner", "management", True))
    monkeypatch.setattr(worker, "rescore", lambda c: rescored.append(c.id))
    shop = company("Shop", website="https://shop.pk")
    search = make_search(db, shop)
    crawler({"https://shop.pk": site(people=[PersonFound(full_name="Ayesha Khan", title="Founder")])})
    enrich(db, search)
    ayesha = db.scalar(select(Contact))
    assert (ayesha.seniority, ayesha.department, ayesha.is_decision_maker) == ("owner", "management", True)
    assert rescored == [shop.id]


class WarningSource(FakeSource):
    def search(self, query, location, limit, progress):
        progress("Monthly Google Places limit reached (900/900). Keeping the 1 businesses found so far.")
        return self.results


def test_partial_result_warning_kept_in_final_message(db, monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", WarningSource([CompanyResult(name="A", source_ref="a")]))
    job = run_search(db, enrich_flag=False)
    assert job.status == "done"
    assert job.progress_message == (
        "Found 1 businesses. Monthly Google Places limit reached (900/900). Keeping the 1 businesses found so far."
    )
    # normal progress messages are not repeated
    monkeypatch.setitem(SOURCES, "fake", FakeSource([CompanyResult(name="A", source_ref="a")]))
    assert run_search(db, enrich_flag=False).progress_message == "Found 1 businesses."


def test_search_with_find_emails_passes_it_to_the_crawl(db, monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", FakeSource([CompanyResult(name="A", source_ref="a", website="a.pk")]))
    job = SearchJob(kind="search", query="q", location="l", source="fake", enrich=True,
                    params={"find_emails": True, "titles": []})
    db.add(job)
    db.commit()
    worker.run_job(job.id)
    crawl = db.scalar(select(SearchJob).where(SearchJob.kind == "enrich"))
    assert crawl.params == {"find_emails": True}


def test_source_emails_are_normalized(db, monkeypatch):
    results = [CompanyResult(name="A", source_ref="a", email=" Info@A.pk; sales@a.pk, INFO@a.pk "),
               CompanyResult(name="B", source_ref="b", email="not an email")]
    monkeypatch.setitem(SOURCES, "fake", FakeSource(results))
    run_search(db, enrich_flag=False)
    a = db.scalar(select(Company).where(Company.name == "A"))
    assert a.email == "info@a.pk"
    assert a.emails == ["info@a.pk", "sales@a.pk"]
    b = db.scalar(select(Company).where(Company.name == "B"))
    assert b.email is None and b.emails == []


def test_search_rescores_companies(db, monkeypatch):
    rescored = []
    monkeypatch.setattr(worker, "rescore", lambda c: rescored.append(c.name))
    monkeypatch.setitem(SOURCES, "fake", FakeSource([CompanyResult(name="A", source_ref="a")]))
    run_search(db, enrich_flag=False)
    assert rescored == ["A"]


def test_recover_rules(db):
    def running(kind, source):
        job = SearchJob(kind=kind, query="q", location="l", source=source, status="running")
        db.add(job)
        db.commit()
        return job.id

    google = running("search", "google")
    osm = running("search", "osm")
    crawl = running("enrich", "crawler")
    emails = running("emails", "emails")
    worker.recover_jobs()
    db.expire_all()
    g = db.get(SearchJob, google)
    assert g.status == "failed" and g.finished_at is not None
    assert g.error == "Interrupted by a restart — run the search again (to avoid paying twice)"
    assert [db.get(SearchJob, i).status for i in (osm, crawl, emails)] == ["queued", "queued", "queued"]


# ----- fixes from the company-size review ---------------------------------------------


def test_crawls_from_before_the_size_hints_are_crawled_again_once(db, crawler):
    # Review finding 2: companies crawled before the size hints existed get them on the
    # next normal crawl; ones crawled by today's crawler are still skipped.
    old = company("Old", website="https://old.pk", enrich_status="done",
                  website_signals={"https": True, "pages_crawled": 2})
    new = company("New", website="https://new.pk", enrich_status="done",
                  website_signals={"https": True, "pages_crawled": 2, "employees_stated": None})
    search = make_search(db, old, new)
    fake = crawler({"https://old.pk": site(employees_stated=12, employees_from="text")})
    job = enrich(db, search)
    assert fake.calls == ["https://old.pk"]
    assert "1 already crawled, skipped" in job.progress_message
    o = db.get(Company, old.id)
    assert (o.website_signals["employees_stated"], o.employees, o.size_band) == (12, 12, "medium")
    enrich(db, search)  # now it has the size keys: skipped like the other one
    assert fake.calls == ["https://old.pk"]


def run_loop_once(db, monkeypatch, calls, stop_after):
    """Run worker._loop until `stop_after` is recorded in calls (fakes record what ran)."""
    stop = threading.Event()

    def record(name, result):
        calls.append(name)
        if name == stop_after:
            stop.set()
        return result

    def fake_run_job(job_id):
        record("job", None)
        with worker.SessionLocal() as session:
            session.get(SearchJob, job_id).status = "done"
            session.commit()

    monkeypatch.setattr(worker, "run_job", fake_run_job)
    monkeypatch.setattr(worker, "size_saved_companies", lambda session, limit=None: record(("size", limit), 0))
    monkeypatch.setattr(worker.ppp, "available", lambda: True)
    monkeypatch.setattr(worker.ppp, "match_saved_companies",
                        lambda session, only_unchecked=True, limit=None: record(("ppp", limit), (0, 0)))
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    thread = threading.Thread(target=worker._loop, args=(stop,), daemon=True)
    thread.start()
    thread.join(timeout=30)
    stop.set()
    assert not thread.is_alive()


def test_backfill_runs_between_jobs_not_before_them(db, monkeypatch):
    # Review findings 20/25: a queued job starts first; the size and PPP back-fills only
    # run when nothing is waiting, a capped slice at a time.
    db.add(company("Unchecked"))  # ppp_checked_at is None: the PPP slice has work
    db.add(SearchJob(kind="search", query="q", location="l", source="osm"))
    db.commit()
    calls = []
    run_loop_once(db, monkeypatch, calls, stop_after=("ppp", worker.BACKFILL_LIMIT))
    assert calls == ["job", ("size", worker.BACKFILL_LIMIT), ("ppp", worker.BACKFILL_LIMIT)]


def test_backfill_slice_skips_ppp_when_everything_is_checked(db, monkeypatch):
    from datetime import datetime, timezone

    db.add(company("Checked", ppp_checked_at=datetime.now(timezone.utc)))
    db.commit()
    calls = []
    monkeypatch.setattr(worker, "size_saved_companies", lambda session, limit=None: calls.append("size") or 0)
    monkeypatch.setattr(worker.ppp, "available", lambda: True)
    monkeypatch.setattr(worker.ppp, "match_saved_companies", lambda *a, **k: calls.append("ppp") or (0, 0))
    assert worker._backfill_slice() is False  # nothing to do: the loop waits before trying again
    assert calls == ["size"]


def test_backfill_slice_errors_are_logged_not_raised(db, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(worker, "size_saved_companies", broken)
    monkeypatch.setattr(worker.ppp, "available", lambda: True)
    monkeypatch.setattr(worker.ppp, "match_saved_companies", broken)
    db.add(company("Unchecked"))
    db.commit()
    assert worker._backfill_slice() is False
