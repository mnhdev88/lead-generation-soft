"""Buying intent per service (app/intent.py), its API filters/sort, and the worker parts."""
import sys
import types
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import config, intent, worker
from app.intent import HOT_SCORE, OFFERS, apply_intent, business_kind, estimate_intent
from app.models import Company, JobResult, SearchJob
from app.scoring import rescore
from app.sources import SOURCES, CompanyResult, Source

THIS_YEAR = datetime.now(timezone.utc).year
NOW = datetime.now(timezone.utc)

# What today's crawler finds on a modern, well-equipped website (nothing to sell).
GOOD_SITE = {
    "pages_crawled": 4, "https": True, "platform": "WordPress", "copyright_year": THIS_YEAR,
    "title": "Acme Dental", "description": "Dentist in Austin", "has_contact_form": True,
    "has_analytics": True, "has_search_console": True,  # ad_pixels left out: "runs ads" is a marketing signal
    "has_local_schema": True, "has_online_booking": True, "booking_provider": "zocdoc",
    "has_online_payment": True, "has_shop": False, "has_online_ordering": False,
    "has_pdf_forms": False, "has_live_chat": True, "has_viewport": True,
    "hiring_roles": [], "hiring_titles": [],
}


def company(**kw) -> Company:
    fields = dict(name="Acme Dental", category="Dentist", source="google", source_ref="x",
                  website="https://acme.example", domain="acme.example", lead_status="new",
                  business_status="OPERATIONAL", contacts=[], socials={})
    fields.update(kw)
    return Company(**fields)


def site(**changes) -> dict:
    return {**GOOD_SITE, **changes}


def reasons(c: Company, offer: str) -> list[str]:
    return estimate_intent(c)[offer]["reasons"]


def score(c: Company, offer: str) -> int:
    return estimate_intent(c)[offer]["score"]


# ----- website ------------------------------------------------------------------------


def test_good_business_has_little_intent():
    c = company(website_signals=site(), rating=4.8, reviews_count=200, google_photos_count=10,
                search_rank=1, peer_reviews_median=100)
    result = estimate_intent(c)
    assert set(result) == set(OFFERS)
    assert all(result[o]["score"] == 0 for o in OFFERS), result


def test_no_website():
    c = company(website=None, domain=None)
    assert reasons(c, "website") == ["No website +30"]
    assert score(c, "website") == 30


def test_only_a_facebook_page():
    c = company(website="https://facebook.com/acme", domain="facebook.com", socials={"facebook": "x"})
    assert reasons(c, "website") == ["Only a Facebook / free page, no own website +25"]
    assert reasons(c, "marketing") == ["Social profiles but no own website +10"]


def test_website_weaknesses_add_up():
    c = company(website_signals=site(https=False, copyright_year=THIS_YEAR - 3, has_viewport=False,
                                     platform="Wix"),
                pagespeed={"performance": 38, "seo": 90}, reviews_count=3,
                google_reviews=[{"rating": 5, "text": "Great"}] * 3,
                review_signals={"likely_new": True, "oldest_review": (NOW - timedelta(days=100)).isoformat()})
    assert reasons(c, "website") == [
        "No SSL (http only) +15", f"Outdated website (copyright {THIS_YEAR - 3}) +10",
        "Very slow on mobile (PageSpeed 38) +15", "Not mobile-friendly +10",
        "DIY website builder (Wix) +10",
        # not "Possibly a new business": a website with a 3-year-old copyright isn't new
    ]
    assert score(c, "website") == 60
    c.pagespeed = {"performance": 60, "seo": 90}
    assert "Slow on mobile (PageSpeed 60) +8" in reasons(c, "website")
    c.pagespeed = {"performance": 75}
    assert not any("PageSpeed" in r for r in reasons(c, "website"))


def test_copyright_two_years_old_is_fine():
    c = company(website_signals=site(copyright_year=THIS_YEAR - 2))
    assert reasons(c, "website") == []


def test_website_facts_only_from_a_good_crawl():
    bad = site(https=False, has_viewport=False, has_analytics=False, has_search_console=False,
               has_online_booking=False, has_local_schema=False, pages_crawled=0)
    c = company(website_signals=bad)
    assert all(score(c, o) == 0 for o in OFFERS), estimate_intent(c)


def test_old_crawl_without_new_keys_gives_no_missing_points():
    # Crawled before the new signals existed: "no analytics" etc. are unknown, not missing.
    c = company(website_signals={"pages_crawled": 3, "https": True, "title": "A", "description": "B"})
    assert all(score(c, o) == 0 for o in OFFERS), estimate_intent(c)


def test_failed_pagespeed_is_ignored():
    c = company(website_signals=site(), pagespeed={"error": "timeout", "performance": 10, "seo": 10})
    assert score(c, "website") == 0 and score(c, "seo") == 0


# ----- seo ----------------------------------------------------------------------------


def test_seo_signals():
    c = company(website_signals=site(title=None, description="", has_local_schema=False,
                                     has_analytics=False, has_search_console=False),
                pagespeed={"performance": 90, "seo": 70}, reviews_count=12, google_photos_count=3,
                search_rank=14, peer_reviews_median=80)
    assert reasons(c, "seo") == [
        "Missing page title and meta description +15", "No LocalBusiness schema +10",
        "No Google Analytics or Search Console +10", "Low PageSpeed SEO score (70) +10",
        "Weak Google profile (12 reviews, 3 photos) +15",
        "Not in Google's top 10 for the search (position 14) +15",
    ]  # "far fewer reviews than competitors" is the same fact as "12 reviews": counted once
    assert score(c, "seo") == 75


def test_seo_far_below_peers_only_when_not_already_few_reviews():
    c = company(website_signals=site(), reviews_count=25, google_photos_count=10, peer_reviews_median=200)
    assert reasons(c, "seo") == ["Far fewer reviews than competitors (25 vs typical 200) +10"]
    c.reviews_count = 15
    assert reasons(c, "seo") == ["Weak Google profile (15 reviews) +15"]


def test_seo_tracking_needs_both_missing():
    c = company(website_signals=site(has_analytics=False, has_search_console=True))
    assert reasons(c, "seo") == []


def test_search_rank_middle_and_google_without_reviews():
    c = company(website_signals=site(), search_rank=7, google_photos_count=10, reviews_count=None)
    # Google leaves the count out when there are no reviews: with the profile loaded = 0.
    assert reasons(c, "seo") == ["Weak Google profile (0 reviews) +15", "Google search position 7 +8"]
    # Without Google profile data (OpenStreetMap) the review count is unknown.
    c = company(source="osm", website_signals=site(), reviews_count=None, google_photos_count=None)
    assert reasons(c, "seo") == []


def test_seo_capped_without_website():
    c = company(website=None, domain=None, reviews_count=25, google_photos_count=1, search_rank=20,
                peer_reviews_median=200)
    result = estimate_intent(c)["seo"]
    assert result["score"] == 30
    assert result["reasons"][-1] == "No website: SEO at most 30"


# ----- software -----------------------------------------------------------------------


def test_business_kind_from_category():
    assert business_kind(company(category="Dentist")) == "appointments"
    assert business_kind(company(category="Barber shop")) == "appointments"
    assert business_kind(company(category="Pizza restaurant")) == "food"
    assert business_kind(company(category="Coffee shop")) == "food"
    assert business_kind(company(category="Clothing store")) == "retail"
    assert business_kind(company(category="Beauty supply store")) == "retail"
    assert business_kind(company(category="Gift shop")) == "retail"
    assert business_kind(company(category="Spanish school")) is None  # not "spa"
    assert business_kind(company(category=None)) is None
    assert business_kind(company(category=None, raw={"primaryType": "dental_clinic"})) == "appointments"


def test_business_kind_misfires_are_unknown_or_retail():
    """Categories that only look like an appointment / food business (review finding)."""
    unknown = [
        ("Health Department", "local_government_office"), ("Public health department", None),
        ("Medical school", "school"), ("Health insurance agency", "insurance_agency"),
        ("Mental health service", None), ("Cleaning products supplier", "wholesaler"),
        ("Tire wholesaler", None), ("Medical laboratory", "medical_lab"), ("Car wash", None),
        ("General contractor", None), ("Dentist", "local_government_office"),
    ]
    for category, primary in unknown:
        raw = {"primaryType": primary} if primary else {}
        assert business_kind(company(category=category, raw=raw)) is None, category
    for category in ("Grill store", "Dental Supply Store", "Plumbing supply store", "Auto Parts Store"):
        assert business_kind(company(category=category)) == "retail", category
    # Google's primaryType wins over a misleading category word.
    assert business_kind(company(category="Smile Studio", raw={"primaryType": "dentist"})) == "appointments"
    assert business_kind(company(category="Hair care", raw={"primaryType": "hair_salon"})) == "appointments"
    assert business_kind(company(category="Restaurant and gift shop")) == "food"
    # A county health department gets no booking / payment points.
    dept = company(category="Health Department", website_signals=site(has_online_booking=False,
                                                                       has_online_payment=False))
    assert reasons(dept, "software") == []


def test_software_appointment_business():
    c = company(website_signals=site(has_online_booking=False, booking_provider=None, has_online_payment=False,
                                     has_pdf_forms=True, has_live_chat=False,
                                     hiring_roles=["front_desk", "sales", "scheduling"]),
                size_band="medium", review_signals={"pain_points": {"phone": ["nobody answers"],
                                                                    "booking": ["hard to book"]}})
    assert reasons(c, "software") == [
        "No online booking +20", "No online payment +10", "Paper / PDF forms on the website +15",
        "Hiring front desk, scheduling +20", "Size medium +10",
        "Reviews complain about booking, phone +15", "No live chat +5",
    ]
    assert score(c, "software") == 95


def test_tag_manager_makes_widget_absence_unknown():
    """GTM can load chat, ads and booking widgets after the page loads: no absence points."""
    c = company(website_signals=site(has_tag_manager=True, has_online_booking=False, booking_provider=None,
                                     has_online_payment=False, has_live_chat=False, ad_pixels=[]))
    assert reasons(c, "software") == []
    assert reasons(c, "marketing") == []  # not "Measures visitors but runs no ads"
    # Something found in the HTML still counts.
    c.website_signals = site(has_tag_manager=True, ad_pixels=["meta"])
    assert reasons(c, "marketing") == ["Runs ads (Meta) +20"]


def test_absence_unknown_or_small_crawl_gives_no_points():
    # None = the crawler could not tell (e.g. a page built by JavaScript).
    c = company(website_signals=site(has_online_booking=None, has_online_payment=None, has_live_chat=None))
    assert reasons(c, "software") == []
    # "No online payment" / "No live chat" need ABSENCE_MIN_PAGES pages; booking does not.
    c.website_signals = site(pages_crawled=intent.ABSENCE_MIN_PAGES - 1, has_online_booking=False,
                             booking_provider=None, has_online_payment=False, has_live_chat=False)
    assert reasons(c, "software") == ["No online booking +20"]


def test_platform_page_facts_are_not_the_business():
    """A crawled / PageSpeed-tested Facebook or Yelp page describes the platform."""
    for website, domain in (("https://www.facebook.com/joesplumbing", "facebook.com"),
                            ("https://m.facebook.com/joes", "m.facebook.com"),
                            ("https://www.yelp.com/biz/joes", "yelp.com")):
        c = company(website=website, domain=domain, socials={"facebook": "x"},
                    website_signals=site(has_online_booking=False, has_online_payment=False,
                                         has_live_chat=False, has_local_schema=False, description="",
                                         ad_pixels=["google_ads"]),
                    pagespeed={"performance": 30, "seo": 50})
        assert reasons(c, "website") == ["Only a Facebook / free page, no own website +25"], website
        assert reasons(c, "seo") == [] and reasons(c, "software") == []
        assert reasons(c, "marketing") == ["Social profiles but no own website +10"]
    # A free builder site is the business's own page: its facts count.
    wix = company(website="https://joes.wixsite.com/home", domain="joes.wixsite.com",
                  website_signals=site(has_viewport=False), pagespeed={"performance": 30})
    assert "Not mobile-friendly +10" in reasons(wix, "website")
    assert "Very slow on mobile (PageSpeed 30) +15" in reasons(wix, "website")


def test_pagespeed_of_an_old_website_is_ignored():
    c = company(website="https://www.acme.example/", website_signals=site(),
                pagespeed={"performance": 30, "url": "https://acme.example"})
    assert "Very slow on mobile (PageSpeed 30) +15" in reasons(c, "website")  # same site
    c.pagespeed = {"performance": 30, "url": "https://old-wix-site.wixsite.com/acme"}
    assert reasons(c, "website") == []


def test_possibly_new_is_worked_out_today():
    def new_biz(days_old: int, count: int, seen: int, flag=True) -> Company:
        oldest = (NOW - timedelta(days=days_old)).isoformat()
        return company(website_signals=site(), reviews_count=count,
                       google_reviews=[{"rating": 5, "text": "ok"}] * seen,
                       review_signals={"likely_new": flag, "oldest_review": oldest})

    assert "Possibly a new business +20" in reasons(new_biz(100, 3, 3), "website")
    # Saved as new a year ago: the oldest review is now over a year old.
    assert reasons(new_biz(400, 3, 3), "website") == []
    # More reviews than we saw (Google sends at most 5): the oldest may be years old.
    assert reasons(new_biz(100, 12, 5), "website") == []
    assert reasons(new_biz(100, 6, 6), "website") == []  # more than Google ever sends
    assert reasons(new_biz(100, 3, 3, flag=False), "website") == []  # undated review: can't tell
    no_date = company(website_signals=site(), reviews_count=2, review_signals={"likely_new": True})
    assert reasons(no_date, "website") == []


def test_only_confirmed_pain_points_score():
    review = {"pain_points": {"phone": ["nobody answers"], "website": ["site is down"]},
              "pain_points_confirmed": ["website"]}
    c = company(website_signals=site(), review_signals=review)
    assert reasons(c, "software") == []  # one passing remark about the phone: a quote only
    assert reasons(c, "marketing") == ["Reviews complain about the website +5"]


def test_software_by_kind():
    food = company(category="Restaurant", website_signals=site(has_online_ordering=False))
    assert reasons(food, "software") == ["No online ordering +20"]
    shop = company(category="Furniture store", website_signals=site(has_shop=False))
    assert reasons(shop, "software") == ["No online shop +10"]
    # Unknown kind of business: no booking / ordering / shop / payment points.
    other = company(category="Church", website_signals=site(has_online_booking=False, has_online_ordering=False,
                                                              has_online_payment=False))
    assert reasons(other, "software") == []
    # A dentist with booking already: nothing for booking.
    assert reasons(company(website_signals=site()), "software") == []


def test_software_without_website_signals():
    c = company(website=None, domain=None, size_band="large",
                review_signals={"pain_points": {"wait_time": ["waited an hour"]}})
    assert reasons(c, "software") == ["Size large +10", "Reviews complain about waiting +15"]


# ----- marketing ----------------------------------------------------------------------


def test_marketing_signals():
    c = company(website_signals=site(ad_pixels=["google_ads", "meta"], hiring_roles=["social_media"]),
                rating=3.6, reviews_count=30, peer_reviews_median=100,
                review_signals={"pain_points": {"website": ["website is broken"]}})
    assert reasons(c, "marketing") == [
        "Runs ads (Google Ads, Meta) +20", "Hiring social media +15", "Rating 3.6 +15",
        "Fewer reviews than competitors (30 vs typical 100) +10", "Reviews complain about the website +5",
    ]
    assert score(c, "marketing") == 65


def test_marketing_measures_but_no_ads_and_rating_needs_reviews():
    c = company(website_signals=site(ad_pixels=[]), rating=2.0, reviews_count=1)
    assert reasons(c, "marketing") == ["Measures visitors but runs no ads +5"]


# ----- dead leads, apply_intent, rescore -------------------------------------------------


def test_lost_and_closed_score_zero():
    c = company(website=None, domain=None, lead_status="lost")
    result = estimate_intent(c)
    assert all(result[o]["score"] == 0 for o in OFFERS)
    assert result["website"]["reasons"] == ["Status Lost → 0", "No website +30"]
    c = company(website=None, domain=None, business_status="CLOSED_TEMPORARILY")
    assert estimate_intent(c)["website"]["reasons"][0] == "Closed → 0"
    assert estimate_intent(c)["website"]["score"] == 0


def test_apply_intent_sets_columns():
    c = company(website=None, domain=None, size_band="large")
    apply_intent(c)
    assert c.intent_website == 30 and c.intent_seo == 0 and c.intent_software == 10
    assert c.intent_best == "website" and c.intent_best_score == 30
    assert c.intent["website"]["reasons"] == ["No website +30"]
    nothing = company(website_signals=site())
    apply_intent(nothing)
    assert nothing.intent_best is None and nothing.intent_best_score == 0


def test_rescore_applies_intent():
    c = company(website=None, domain=None)
    rescore(c, branches=0)
    assert c.intent_best == "website" and c.intent_website == 30
    assert HOT_SCORE == 60 and intent.OFFER_LABELS["seo"] == "SEO"


# ----- API ----------------------------------------------------------------------------


def _names(r):
    assert r.status_code == 200, r.text
    return [c["name"] for c in r.json()["items"]]


def test_api_filter_and_sort_by_intent(client, db):
    db.add_all([
        company(name="NoSite", source_ref="1", website=None, domain=None),  # website 30
        company(name="Slow", source_ref="2", website_signals=site(https=False, copyright_year=THIS_YEAR - 5),
                pagespeed={"performance": 20}),  # website 40
        company(name="Fine", source_ref="3", website_signals=site(), pagespeed={"performance": 90, "seo": 70}),
    ])
    db.commit()
    for c in db.scalars(select(Company)):
        rescore(c, branches=0)
    db.add(company(name="Unscored", source_ref="4"))  # saved before intent existed
    db.commit()

    assert _names(client.get("/api/companies?sort=intent")) == ["Slow", "NoSite", "Fine", "Unscored"]
    assert _names(client.get("/api/companies?sort=intent&looking_for=seo")) == ["Fine", "NoSite", "Slow", "Unscored"]
    assert _names(client.get("/api/companies?looking_for=website&min_intent=31&sort=intent")) == ["Slow"]
    assert _names(client.get("/api/companies?looking_for=website&min_intent=30&sort=intent")) == ["Slow", "NoSite"]
    assert _names(client.get("/api/companies?looking_for=any&min_intent=1")) == ["Fine", "NoSite", "Slow"]
    assert client.get("/api/companies?looking_for=nope").status_code == 422

    item = next(i for i in client.get("/api/companies?q=Slow").json()["items"])
    assert item["intent_best"] == "website" and item["intent_best_score"] == 40
    assert item["intent"]["website"]["reasons"] == [
        "No SSL (http only) +15", f"Outdated website (copyright {THIS_YEAR - 5}) +10",
        "Very slow on mobile (PageSpeed 20) +15"]
    assert item["pagespeed"] == {"performance": 20}
    unscored = client.get("/api/companies?q=Unscored").json()["items"][0]
    assert unscored["intent"] == {} and unscored["intent_best_score"] is None
    assert unscored["review_signals"] == {} and unscored["search_rank"] is None

    r = client.get("/api/companies/export.csv?sort=intent&looking_for=website&min_intent=30")
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert lines[0].endswith("intent_best,intent_website,intent_seo,intent_software,intent_marketing,intent_reasons")
    assert [line.split(",")[0].lstrip("﻿") for line in lines[1:]] == ["Slow", "NoSite"]


def test_filters_plain_function_checks_looking_for(db):
    from app.routers.companies import filters

    filters(looking_for="seo", min_intent=60)  # fine
    try:
        filters(looking_for="nope")
    except ValueError:
        pass
    else:
        raise AssertionError("expected a ValueError")


# ----- worker -------------------------------------------------------------------------


class FakeGoogle(Source):
    name = "google"
    label = "Fake Google"
    description = ""

    def __init__(self, results):
        self.results = results

    def search(self, query, location, limit, progress):
        return self.results


def _result(i: int, reviews_count, **kw) -> CompanyResult:
    r = CompanyResult(name=f"Biz {i}", source_ref=f"g{i}", category="Dentist", reviews_count=reviews_count,
                      business_status="OPERATIONAL", **kw)
    return r


def test_search_saves_rank_peers_photos_and_reviews(db, monkeypatch):
    fake_reviews = types.ModuleType("app.reviews")
    calls = []

    def analyze_reviews(reviews, reviews_count, now=None):
        calls.append((len(reviews), reviews_count))
        return {"pain_points": {"phone": ["nobody answers"]}, "likely_new": False}

    fake_reviews.analyze_reviews = analyze_reviews
    monkeypatch.setitem(sys.modules, "app.reviews", fake_reviews)
    import app

    monkeypatch.setattr(app, "reviews", fake_reviews, raising=False)

    results = [_result(i, n) for i, n in enumerate([100, 50, 10, 40, 60, None], start=1)]
    results[2].photos_count = 2  # attributes the Google source sets (CompanyResult may not declare them yet)
    results[2].reviews = [{"rating": 1, "text": "Nobody answers the phone", "publish_time": None, "relative": None}]
    monkeypatch.setitem(SOURCES, "google", FakeGoogle(results))
    # An earlier search had Biz 3 higher up: the latest small search wins.
    db.add(Company(name="Biz 3", source="google", source_ref="g3", search_rank=1, contacts=[]))
    job = SearchJob(kind="search", query="dentist", location="Austin", source="google", limit=10)
    db.add(job)
    db.commit()

    worker.run_search_job(db, job)
    db.commit()
    by_name = {c.name: c for c in db.scalars(select(Company))}
    assert by_name["Biz 1"].search_rank == 1 and by_name["Biz 5"].search_rank == 5
    assert by_name["Biz 3"].search_rank == 3
    assert {c.peer_reviews_median for c in by_name.values()} == {50}
    biz3 = by_name["Biz 3"]
    assert biz3.google_photos_count == 2 and len(biz3.google_reviews) == 1
    assert biz3.review_signals["pain_points"] == {"phone": ["nobody answers"]}
    assert calls == [(1, 10)]
    assert biz3.intent_best is not None  # rescored with the new facts
    assert "Weak Google profile (10 reviews, 2 photos) +15" in biz3.intent["seo"]["reasons"]
    assert "Reviews complain about phone +15" in biz3.intent["software"]["reasons"]
    assert len(db.scalars(select(JobResult)).all()) == 6

    # A source without photos/reviews (OpenStreetMap) does not wipe them.
    plain = CompanyResult(name="Biz 3", source_ref="g3", category="Dentist", reviews_count=10)
    worker._upsert_company(db, "google", plain)
    assert biz3.google_photos_count == 2 and len(biz3.google_reviews) == 1


def test_peer_median_needs_enough_results():
    assert worker._peer_reviews_median([_result(i, 5) for i in range(4)]) is None
    assert worker._peer_reviews_median([_result(i, n) for i, n in enumerate([1, 2, 3, 4, 100])]) == 3


def test_peer_median_compares_like_with_like():
    """A solo doctor is compared with doctors, not with the hospitals in the same search."""
    doctors = [_result(i, n, raw={"primaryType": "doctor"}) for i, n in enumerate([10, 15, 20, 25, 30])]
    hospitals = [_result(10 + i, 2000, raw={"primaryType": "hospital"}) for i in range(3)]
    assert worker._peer_medians(doctors + hospitals) == {"doctor": 20}  # 3 hospitals: too few


def _run_google(db, monkeypatch, results, limit):
    monkeypatch.setitem(SOURCES, "google", FakeGoogle(results))
    job = SearchJob(kind="search", query="dentist", location="Austin", source="google", limit=limit)
    db.add(job)
    db.commit()
    worker.run_search_job(db, job)
    db.commit()
    return {c.name: c for c in db.scalars(select(Company))}


def test_deep_search_saves_no_rank(db, monkeypatch):
    """A deep (map tiles) search joins the tiles one after another: the #1 of tile 2 would get
    position 61+ and a false "not in the top 10". Big searches save no position at all."""
    results = [_result(i, 100) for i in range(1, 31)]
    by_name = _run_google(db, monkeypatch, results, limit=120)
    assert all(c.search_rank is None for c in by_name.values())
    assert not any("top 10" in r for c in by_name.values() for r in c.intent["seo"]["reasons"])


def test_search_rank_uses_googles_own_position(db, monkeypatch):
    results = [_result(i, 100) for i in range(1, 4)]
    for r, rank in zip(results, (1, 4, 2)):
        r.rank = rank  # Google's position in its own query (when the source gives it)
    by_name = _run_google(db, monkeypatch, results, limit=20)
    assert [by_name[f"Biz {i}"].search_rank for i in (1, 2, 3)] == [1, 4, 2]


def test_intent_backfill(db):
    db.add_all([company(name="A", source_ref="1", website=None, domain=None),
                company(name="B", source_ref="2", website_signals=site())])
    db.commit()
    assert worker.intent_saved_companies(db) == 2
    by_name = {c.name: c for c in db.scalars(select(Company))}
    assert by_name["A"].intent_best_score == 30 and by_name["B"].intent_best_score == 0
    assert worker.intent_saved_companies(db) == 0  # all done


def test_backfill_slice_runs_pagespeed_and_intent(db, monkeypatch):
    fake = types.ModuleType("app.pagespeed")
    calls = []

    def check_saved_companies(session, limit=5):
        calls.append(limit)
        return 0

    fake.check_saved_companies = check_saved_companies
    monkeypatch.setitem(sys.modules, "app.pagespeed", fake)
    import app

    monkeypatch.setattr(app, "pagespeed", fake, raising=False)
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))
    db.add(company(name="A", source_ref="1", website=None, domain=None))
    db.commit()

    monkeypatch.setattr(config, "PAGESPEED_ENABLED", True)
    assert worker._backfill_slice() is True  # the intent back-fill had work
    assert calls == []  # PageSpeed runs in its own thread, never in the job thread's slice
    assert db.scalar(select(Company)).intent_best_score == 30
    assert worker._backfill_slice() is False  # nothing left

    # The PageSpeed thread's round.
    assert worker.pagespeed_round() == 0
    assert calls == [worker.PAGESPEED_BACKFILL_LIMIT]
    monkeypatch.setattr(config, "PAGESPEED_ENABLED", False)
    assert worker.pagespeed_round() == 0
    assert calls == [worker.PAGESPEED_BACKFILL_LIMIT]  # switched off: Google not asked


def test_pagespeed_thread_does_not_hold_the_job_loop(monkeypatch):
    """The PageSpeed loop pauses between rounds and stops with the worker."""
    import threading

    rounds = []
    stop = threading.Event()

    def fake_round():
        rounds.append(1)
        if len(rounds) == 2:
            stop.set()
        return 1

    monkeypatch.setattr(worker, "pagespeed_round", fake_round)
    monkeypatch.setattr(worker, "PAGESPEED_PAUSE_SECONDS", 0)
    worker._pagespeed_loop(stop)
    assert len(rounds) == 2


def test_new_website_drops_the_old_pagespeed_result(db):
    c = Company(name="Acme", source="google", source_ref="g1", website="https://old.example",
                domain="old.example", pagespeed={"performance": 30}, pagespeed_checked_at=NOW, contacts=[])
    db.add(c)
    db.commit()
    same = CompanyResult(name="Acme", source_ref="g1", website="https://old.example")
    worker._upsert_company(db, "google", same)
    assert c.pagespeed == {"performance": 30} and c.pagespeed_checked_at is not None
    moved = CompanyResult(name="Acme", source_ref="g1", website="https://new.example")
    worker._upsert_company(db, "google", moved)
    assert c.pagespeed == {} and c.pagespeed_checked_at is None  # the back-fill tests the new site


class _NoClose:
    """Lets _backfill_slice use the test session in a `with` block without closing it."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False
