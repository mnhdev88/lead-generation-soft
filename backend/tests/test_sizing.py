"""Company size estimate (app/sizing.py): rules, branches, startup fill, API filter/sort,
export columns and the worker's PPP lookup (with a fake app.ppp, no PPP database needed)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import exporting, sizing, worker
from app.models import Company, Contact, SearchJob
from app.scoring import rescore
from app.sizing import NO_INFO_REASON, apply_size, band_for, estimate_size
from app.sources import SOURCES, CompanyResult, Source


def make(enrich_status="done", signals=None, ppp=None, people=0, **kw) -> Company:
    """A company not saved in any database (estimate_size only reads it). Website signals
    come from a good crawl (pages_crawled 1) unless the test says otherwise."""
    contacts = [Contact(full_name=f"Person {i}", source="website") for i in range(people)]
    signals = {"pages_crawled": 1, **signals} if signals else {}
    return Company(name="Acme Dental", source="osm", source_ref=kw.pop("source_ref", "x"),
                   enrich_status=enrich_status, website_signals=signals, ppp=ppp or {},
                   contacts=contacts, **kw)


def text_site(n, snippet=None, **extra) -> dict:
    return {"pages_crawled": 1, "employees_stated": n, "employees_from": "text",
            "employees_snippet": snippet, **extra}


PPP_14 = {"jobs": 14, "date_approved": "2021-02-03", "match": "same name and ZIP"}


# ----- bands and headcount -------------------------------------------------------------


def test_band_boundaries():
    assert [band_for(n) for n in (None, 0, 1, 2, 10, 11, 50, 51, 5000)] == [
        None, None, "solo", "small", "small", "medium", "medium", "large", "large"]


def test_no_evidence_is_unknown_not_solo():
    est = estimate_size(make())
    assert (est.band, est.employees, est.confidence) == (None, None, None)
    assert est.reasons == [NO_INFO_REASON]


def test_website_text_is_high_confidence():
    est = estimate_size(make(signals=text_site(25, "Our team of 25 dentists")))
    assert (est.band, est.employees, est.employees_source, est.confidence) == ("medium", 25, "website", "high")
    assert est.reasons == ['Website says: "Our team of 25 dentists"']


def test_solo_only_when_one_person_is_stated():
    assert estimate_size(make(signals=text_site(1))).band == "solo"
    assert estimate_size(make(ppp={"jobs": 1, "date_approved": "2020-05-01"})).band == "solo"


def test_jsonld_alone_is_medium_confidence():
    est = estimate_size(make(signals={"employees_stated": 8, "employees_from": "jsonld"}))
    assert (est.band, est.confidence) == ("small", "medium")
    assert est.reasons == ["Website page data: 8 employees (may be a template value)"]


def test_ppp_alone_mentions_2020_21():
    est = estimate_size(make(ppp=PPP_14))
    assert (est.band, est.employees, est.employees_source, est.confidence) == ("medium", 14, "ppp", "high")
    assert est.reasons == ["PPP loan 2021: 14 jobs reported (figure from 2020-21, matched by same name and ZIP)"]


def test_ppp_match_confidence_medium_lowers_the_size_confidence():
    est = estimate_size(make(ppp={**PPP_14, "confidence": "medium"}))
    assert (est.band, est.employees, est.confidence) == ("medium", 14, "medium")
    assert estimate_size(make(ppp={**PPP_14, "confidence": "high"})).confidence == "high"


def test_ppp_not_applicable_is_no_evidence():
    for ppp in ({"not_applicable": True}, {}):
        est = estimate_size(make(ppp=ppp))
        assert est.reasons == [NO_INFO_REASON]


def test_ppp_zero_jobs_is_not_a_headcount():
    est = estimate_size(make(ppp={"jobs": 0, "date_approved": "2020-04-10"}))
    assert est.band is None and est.employees is None
    assert est.reasons == ["PPP loan 2020 found, no headcount given", NO_INFO_REASON]


def test_website_and_ppp_agree():
    est = estimate_size(make(signals=text_site(12), ppp=PPP_14))
    assert (est.employees, est.employees_source, est.confidence) == (12, "website", "high")
    assert len(est.reasons) == 2 and est.reasons[1].startswith("PPP loan 2021: 14 jobs")


def test_website_and_ppp_disagree_lowers_confidence():
    est = estimate_size(make(signals=text_site(60), ppp=PPP_14))
    assert (est.band, est.employees, est.employees_source, est.confidence) == ("large", 60, "website", "medium")
    assert "Website and PPP loan disagree (60 vs 14): using the website figure" in est.reasons
    assert any(r.startswith("PPP loan 2021") for r in est.reasons)


def test_ppp_beats_disagreeing_jsonld():
    est = estimate_size(make(signals={"employees_stated": 200, "employees_from": "jsonld"}, ppp=PPP_14))
    assert (est.band, est.employees, est.employees_source, est.confidence) == ("medium", 14, "ppp", "medium")
    assert est.reasons[0].startswith("PPP loan") and "using the PPP figure" in est.reasons[-1]


def test_failed_crawl_is_ignored():
    # Signals of a crawl that never worked (pages_crawled 0) tell nothing.
    signals = text_site(25, has_careers=True, founded_year=1998, locations_stated=12, pages_crawled=0)
    est = estimate_size(make(enrich_status="failed", signals=signals))
    assert (est.band, est.founded_year, est.locations_count) == (None, None, None)
    assert est.reasons == [NO_INFO_REASON]


def test_failed_recrawl_keeps_the_earlier_good_size():
    # Review finding 3: the site said "team of 25", a later re-crawl timed out. The worker
    # keeps the stored signals; the size must not drop to unknown.
    est = estimate_size(make(enrich_status="failed", signals=text_site(25, "Our team of 25")))
    assert (est.band, est.employees, est.employees_source, est.confidence) == ("medium", 25, "website", "high")


def test_out_of_range_values_are_ignored():
    est = estimate_size(make(signals={"employees_stated": "lots", "founded_year": 1066, "locations_stated": 1}))
    assert (est.band, est.founded_year, est.locations_count) == (None, None, None)


# ----- people listed on the website (a minimum) -----------------------------------------


def test_team_page_alone_is_a_low_confidence_minimum():
    est = estimate_size(make(people=4))
    assert (est.band, est.employees, est.employees_source, est.confidence) == ("small", 4, "team_page", "low")
    assert est.reasons == ["4 people listed on the website (a minimum)"]


def test_one_person_on_the_website_tells_nothing():
    assert estimate_size(make(people=1)).band is None


def test_team_page_raises_but_never_lowers():
    raised = estimate_size(make(signals=text_site(5), people=12))
    assert (raised.band, raised.employees, raised.employees_source, raised.confidence) == (
        "medium", 12, "team_page", "medium")
    kept = estimate_size(make(signals=text_site(40), people=3))
    assert (kept.band, kept.employees, kept.employees_source, kept.confidence) == ("medium", 40, "website", "high")
    assert "3 people listed on the website" in kept.reasons


def test_contacts_from_other_sources_do_not_count():
    company = make()
    company.contacts = [Contact(full_name=f"P{i}", source="hunter") for i in range(5)]
    assert estimate_size(company).band is None


# ----- locations, hints, founded --------------------------------------------------------


def test_locations_raise_the_band():
    est = estimate_size(make(signals={"locations_stated": 3}))
    assert (est.band, est.locations_count, est.confidence) == ("medium", 3, "low")
    assert est.reasons == ["3 locations (website): at least medium (11-50 people)"]



def test_locations_never_raise_a_real_headcount():
    # Review finding 14: "Medium - 4" must not happen. The headcount band stays; the
    # locations are a reason and lower the confidence.
    est = estimate_size(make(signals=text_site(4, locations_stated=3)))
    assert (est.band, est.employees, est.confidence, est.locations_count) == ("small", 4, "medium", 3)
    assert est.reasons[-1] == "3 locations (website): more than the 4 employees suggests"
    big = estimate_size(make(signals=text_site(8, locations_stated=12)))
    assert (big.band, big.employees, big.confidence) == ("small", 8, "medium")
    ppp = estimate_size(make(ppp={"jobs": 6, "date_approved": "2021-01-05"}), branches=3)
    assert (ppp.band, ppp.employees, ppp.confidence, ppp.locations_count) == ("small", 6, "medium", 4)
    # Locations that fit the headcount change nothing.
    fits = estimate_size(make(signals=text_site(30, locations_stated=3)))
    assert (fits.band, fits.confidence) == ("medium", "high")
    assert fits.reasons[-1] == "3 locations (website)"


def test_locations_raise_a_team_page_minimum():
    # People listed on the website are only a minimum, not a headcount.
    est = estimate_size(make(signals={"locations_stated": 3}, people=4))
    assert (est.band, est.employees, est.employees_source, est.confidence) == ("medium", 4, "team_page", "low")


def test_franchise_domain_is_not_counted_as_branches():
    # Review finding 13: 11 saved stores link to the brand's website; the franchisee
    # with 6 PPP jobs is a small business, not "large".
    est = estimate_size(make(ppp={"jobs": 6, "date_approved": "2021-01-05"}), branches=11)
    assert (est.band, est.employees, est.confidence, est.locations_count) == ("small", 6, "high", None)
    assert "12 saved listings share this website - may be a chain or franchise" in est.reasons
    # Without a headcount a chain domain alone says nothing about the size either.
    alone = estimate_size(make(), branches=11)
    assert (alone.band, alone.locations_count) == (None, None)
    assert alone.reasons == ["12 saved listings share this website - may be a chain or franchise", NO_INFO_REASON]
    # Up to CHAIN_BRANCHES saved branches still count as locations.
    few = estimate_size(make(), branches=sizing.CHAIN_BRANCHES)
    assert (few.band, few.locations_count) == ("medium", sizing.CHAIN_BRANCHES + 1)
    # The website's own count still counts on a chain domain.
    stated = estimate_size(make(signals={"locations_stated": 3}), branches=40)
    assert (stated.band, stated.locations_count) == ("medium", 3)


def test_capped_branch_count_is_shown_with_a_plus():
    est = estimate_size(make(), branches=sizing.MAX_BRANCHES)
    assert est.reasons[0] == f"{sizing.MAX_BRANCHES + 1}+ saved listings share this website - may be a chain or franchise"


def test_branches_count_as_locations():
    est = estimate_size(make(signals={"locations_stated": 2}), branches=4)
    assert (est.locations_count, est.band) == (5, "medium")
    assert est.reasons[0].startswith("5 locations (same website, other places)")
    assert estimate_size(make(), branches=0).locations_count is None


def test_reviews_are_weak_hints():
    assert estimate_size(make(reviews_count=299)).band is None
    small = estimate_size(make(reviews_count=300))
    assert (small.band, small.confidence, small.employees) == ("small", "low", None)
    medium = estimate_size(make(reviews_count=1500))
    assert medium.band == "medium" and medium.reasons == ["1,500 Google reviews: at least medium (11-50 people)"]
    # With a headcount, reviews change nothing.
    assert estimate_size(make(signals=text_site(1), reviews_count=5000)).band == "solo"


def test_hiring_means_not_solo_and_founded_is_shown():
    est = estimate_size(make(signals={"has_careers": True, "founded_year": 1998}))
    assert (est.band, est.confidence, est.founded_year) == ("small", "low", 1998)
    assert est.reasons == ["Hiring (careers page): at least small (2-10 people)", "Founded 1998"]
    with_count = estimate_size(make(signals=text_site(30, has_careers=True)))
    assert with_count.band == "medium" and "Hiring (careers page)" in with_count.reasons


def test_founded_alone_is_still_unknown_size():
    est = estimate_size(make(signals={"founded_year": 2005}))
    assert (est.band, est.founded_year) == (None, 2005)
    assert est.reasons == ["Founded 2005", NO_INFO_REASON]


# ----- apply_size with a database (branches) -------------------------------------------


def saved(db, ref, **kw) -> Company:
    c = Company(name=kw.pop("name", "Smile Dental"), source="osm", source_ref=ref, **kw)
    db.add(c)
    db.commit()
    return c


def test_apply_size_counts_branches(db):
    here = saved(db, "a", domain="smile.com", lat=30.0, lon=-97.0, city="Austin")
    saved(db, "b", domain="smile.com", lat=30.0005, lon=-97.0, city="Austin")  # same place (~55 m)
    saved(db, "c", domain="smile.com", lat=30.3, lon=-97.7, city="Austin")  # other branch
    saved(db, "d", domain="smile.com", city="Dallas")  # no position, other city
    saved(db, "e", domain="smile.com", city="austin ")  # no position, same city: maybe the same place
    saved(db, "f", domain="other.com", lat=30.3, lon=-97.7)
    apply_size(here)
    assert (here.locations_count, here.size_band, here.size_confidence) == (3, "medium", "low")
    assert here.size_reasons == ["3 locations (same website, other places): at least medium (11-50 people)"]


def test_shared_domain_has_no_branches(db):
    here = saved(db, "a", domain="facebook.com", city="Austin")
    saved(db, "b", domain="facebook.com", city="Dallas")
    saved(db, "c", domain="facebook.com", city="Houston")
    apply_size(here)
    assert here.locations_count is None and here.size_band is None


def test_rescore_fills_the_size_columns(db):
    c = saved(db, "a", enrich_status="done", website_signals=text_site(25, founded_year=1990),
              ppp=PPP_14)
    rescore(c)
    db.commit()
    db.expire_all()
    c = db.get(Company, c.id)
    assert (c.size_band, c.employees, c.employees_source, c.size_confidence) == ("medium", 25, "website", "high")
    assert c.founded_year == 1990 and len(c.size_reasons) == 3


def test_branch_counts_match_count_branches_in_one_query(db):
    from sqlalchemy import event

    a = saved(db, "a", domain="smile.com", lat=30.0, lon=-97.0, city="Austin")
    b = saved(db, "b", domain="smile.com", lat=30.3, lon=-97.7, city="Austin")
    c = saved(db, "c", domain="smile.com", city="Dallas")
    d = saved(db, "d", domain="teeth.com", lat=30.0, lon=-97.0)
    e = saved(db, "e", domain="teeth.com", lat=31.0, lon=-97.0)
    f = saved(db, "f", domain="facebook.com", city="Houston")
    g = saved(db, "g")
    companies = [a, b, c, d, e, f, g]
    expected = {x.id: sizing.count_branches(db, x) for x in companies}
    assert expected == {a.id: 2, b.id: 2, c.id: 2, d.id: 1, e.id: 1, f.id: 0, g.id: 0}

    statements = []
    engine = db.get_bind()

    def listener(*args):
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", listener)
    try:
        assert sizing.branch_counts(db, companies) == expected
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert len(statements) == 1


def test_branch_counting_stops_at_the_cap(db, monkeypatch):
    monkeypatch.setattr(sizing, "MAX_BRANCHES", 3)
    here = saved(db, "here", domain="chain.com", lat=30.0, lon=-90.0)
    for i in range(6):
        saved(db, f"b{i}", domain="chain.com", lat=31.0 + i, lon=-90.0)
    assert sizing.count_branches(db, here) == 3
    assert sizing.branch_counts(db, [here])[here.id] == 3


def test_rescore_with_given_branches_does_not_query(db, monkeypatch):
    c = saved(db, "a", domain="smile.com")

    def no_query(session, company):
        raise AssertionError("count_branches must not run")

    monkeypatch.setattr(sizing, "count_branches", no_query)
    rescore(c, branches=2)
    assert c.locations_count == 3


def test_recompute_scores_counts_branches_per_batch(db, monkeypatch):
    from app.scoring import recompute_scores

    for ref, lat in (("a", 30.0), ("b", 30.5), ("c", 31.0)):
        saved(db, ref, domain="smile.com", lat=lat, lon=-97.0)

    def no_query(session, company):
        raise AssertionError("count_branches must not run per company")

    monkeypatch.setattr(sizing, "count_branches", no_query)
    assert recompute_scores(db) == (3, 0)
    db.expire_all()
    assert [c.locations_count for c in db.scalars(select(Company).order_by(Company.id))] == [3, 3, 3]


def test_refresh_branch_sizes_updates_every_branch(db):
    first = saved(db, "a", domain="smile.com", lat=30.0, lon=-97.0)
    apply_size(first)  # saved first: no other branch yet
    assert first.locations_count is None
    saved(db, "b", domain="smile.com", lat=30.5, lon=-97.0)
    saved(db, "c", domain="smile.com", lat=31.0, lon=-97.0)
    saved(db, "d", domain="other.com", lat=31.0, lon=-97.0)
    assert sizing.refresh_branch_sizes(db, {"smile.com", "facebook.com", None}) == 3
    db.commit()
    db.expire_all()
    rows = db.scalars(select(Company).order_by(Company.id)).all()
    assert [c.locations_count for c in rows] == [3, 3, 3, None]


def test_size_saved_companies_with_a_limit(db):
    for ref in "abc":
        saved(db, ref)
    assert sizing.size_saved_companies(db, limit=2) == 2
    assert sizing.size_saved_companies(db, limit=2) == 1
    assert sizing.size_saved_companies(db, limit=2) == 0


def test_company_brief_has_size_confidence_and_reasons():
    # Review finding 33: the People page needs the confidence to grey out rough guesses.
    from app.schemas import CompanyBrief

    c = make(size_band="small", employees=4, employees_source="team_page", size_confidence="low")
    c.id, c.size_reasons = 1, None
    brief = CompanyBrief.model_validate(c)
    assert (brief.employees_source, brief.size_confidence, brief.size_reasons) == ("team_page", "low", [])


def test_size_saved_companies_runs_once(db):
    old = saved(db, "a", reviews_count=400)
    nothing = saved(db, "b")
    assert sizing.size_saved_companies(db) == 2
    db.expire_all()
    assert db.get(Company, old.id).size_band == "small"
    assert db.get(Company, nothing.id).size_reasons == [NO_INFO_REASON]
    assert sizing.size_saved_companies(db) == 0  # already sized, even the unknown one


# ----- API and export -----------------------------------------------------------------


@pytest.fixture
def sized(db):
    rows = [("Alpha", "small", 4), ("Beta", "large", 120), ("Gamma", None, None), ("Delta", "medium", 30)]
    for name, band, people in rows:
        saved(db, name, name=name, size_band=band, employees=people,
              size_reasons=[f"{people} people"] if people else None, ppp=None)


def names(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [c["name"] for c in resp.json()["items"]]


def test_api_size_filter_and_sort(client, sized):
    assert names(client.get("/api/companies?size_band=large")) == ["Beta"]
    assert names(client.get("/api/companies?size_band=unknown")) == ["Gamma"]
    assert names(client.get("/api/companies?sort=employees")) == ["Beta", "Delta", "Alpha", "Gamma"]
    assert client.get("/api/companies?size_band=huge").status_code == 422
    item = {c["name"]: c for c in client.get("/api/companies").json()["items"]}
    assert (item["Gamma"]["size_reasons"], item["Gamma"]["ppp"]) == ([], {})  # NULL -> [] / {}
    assert (item["Beta"]["size_band"], item["Beta"]["employees"]) == ("large", 120)


def test_csv_export_uses_the_size_filter(client, sized):
    r = client.get("/api/companies/export.csv?size_band=medium&sort=employees")
    assert r.status_code == 200
    lines = r.text.lstrip("﻿").strip().splitlines()
    assert len(lines) == 2 and lines[1].startswith("Delta,")


def test_sheets_filters_reject_unknown_band():
    from app.routers.companies import filters

    filters(size_band="small")  # plain call works
    with pytest.raises(ValueError):
        filters(size_band="huge")


def test_export_size_columns():
    c = make(signals={"has_careers": True}, size_band="small", employees=4, employees_source="team_page",
             founded_year=2001, locations_count=2, ppp=PPP_14)
    headers, rows = exporting.company_table([c])
    row = dict(zip(headers, rows[0]))
    assert [row[h] for h in headers[-8:]] == ["small", 4, "team_page", 2001, 2, "yes", 14, "2021-02-03"]
    never_crawled = make(enrich_status="failed", signals={"has_careers": True, "pages_crawled": 0})
    headers, rows = exporting.company_table([never_crawled])
    row = dict(zip(headers, rows[0]))
    assert (row["hiring"], row["ppp_jobs"]) == ("", None)
    # A failed re-crawl after a good one keeps the stored facts (review finding 3).
    headers, rows = exporting.company_table([make(enrich_status="failed", signals={"has_careers": True})])
    assert dict(zip(headers, rows[0]))["hiring"] == "yes"
    headers, rows = exporting.company_table([make(ppp={"not_applicable": True})])
    assert dict(zip(headers, rows[0]))["ppp_jobs"] is None


# ----- worker: PPP lookup during a search ------------------------------------------------


class FakeSource(Source):
    name = "fake"
    label = "Fake"
    description = "test"

    def __init__(self, results):
        self.results = results

    def unavailable_reason(self):
        return None

    def search(self, query, location, limit, progress):
        return self.results


@pytest.fixture
def fake_ppp(monkeypatch):
    looked_up = []

    def apply_ppp(company):
        looked_up.append(company.name)
        company.ppp = {"jobs": 30, "date_approved": "2021-01-20", "match": "same name and ZIP"}
        company.ppp_checked_at = datetime.now(timezone.utc)
        return True

    monkeypatch.setattr(worker.ppp, "available", lambda: True)
    monkeypatch.setattr(worker.ppp, "apply_ppp", apply_ppp)
    return looked_up


def run_search(db) -> None:
    job = SearchJob(kind="search", query="dentist", location="Austin, TX", source="fake")
    db.add(job)
    db.commit()
    worker.run_job(job.id)
    db.expire_all()


def test_search_looks_up_ppp_once_and_sizes(db, monkeypatch, fake_ppp):
    monkeypatch.setitem(SOURCES, "fake", FakeSource([CompanyResult(name="A", source_ref="a")]))
    run_search(db)
    c = db.scalar(select(Company))
    assert fake_ppp == ["A"]
    assert (c.ppp["jobs"], c.size_band, c.employees_source) == (30, "medium", "ppp")
    run_search(db)  # already checked: not looked up again
    assert fake_ppp == ["A"]


def test_search_without_ppp_data_skips_lookup(db, monkeypatch):
    calls = []
    monkeypatch.setattr(worker.ppp, "available", lambda: False)
    monkeypatch.setattr(worker.ppp, "apply_ppp", lambda c: calls.append(c))
    monkeypatch.setitem(SOURCES, "fake", FakeSource([CompanyResult(name="A", source_ref="a")]))
    run_search(db)
    assert calls == []
    assert db.scalar(select(Company)).size_reasons == [NO_INFO_REASON]


def test_search_sizes_all_branches_together(db, monkeypatch):
    # Review finding 4: three branches saved in one search all show 3 locations, not 1, 2, 3.
    results = [CompanyResult(name=f"Smile {i}", source_ref=f"s{i}", website="https://smile.com",
                             lat=30.0 + i, lon=-97.0) for i in range(3)]
    monkeypatch.setattr(worker.ppp, "available", lambda: False)
    monkeypatch.setitem(SOURCES, "fake", FakeSource(results))
    run_search(db)
    rows = db.scalars(select(Company).order_by(Company.id)).all()
    assert [(c.locations_count, c.size_band) for c in rows] == [(3, "medium")] * 3


def test_ppp_error_does_not_stop_the_search(db, monkeypatch):
    def broken(company):
        raise RuntimeError("disk error")

    monkeypatch.setattr(worker.ppp, "available", lambda: True)
    monkeypatch.setattr(worker.ppp, "apply_ppp", broken)
    monkeypatch.setitem(SOURCES, "fake", FakeSource([CompanyResult(name="A", source_ref="a")]))
    run_search(db)
    assert db.get(SearchJob, 1).status == "done"
