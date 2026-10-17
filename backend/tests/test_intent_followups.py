"""Small fixes where the intent parts meet: Google's own result position, payment through a
booking platform, established businesses never "possibly new", PPP names with HTML entities,
the Tag Manager flag and the business kind in the API."""
from datetime import timedelta

import httpx

from app import ppp
from app.enrich.extract import detect_intent_signals
from app.intent import estimate_intent
from app.schemas import CompanyOut
from app.sources import google_places as gp
from tests.test_google_places import FakeGoogle, make_place, usage_calls  # noqa: F401  (fixture)
from tests.test_intent import NOW, THIS_YEAR, company, reasons, site
from tests.test_ppp import csv_bytes, google_company, loan, ppp_path  # noqa: F401  (fixture)

NEW_REVIEWS = dict(reviews_count=3, google_reviews=[{"rating": 5, "text": "Great"}] * 3,
                   review_signals={"likely_new": True,
                                   "oldest_review": (NOW - timedelta(days=100)).isoformat()})


def test_google_rank_is_the_position_in_its_own_query(usage_calls):  # noqa: F811
    # Page 1 has a place we can't use (no name) at position 2: positions still count it.
    nameless = {**make_place(2), "displayName": {}}
    fake = FakeGoogle(pages=[[make_place(1), nameless, make_place(3)]])
    results = gp.GooglePlacesSource(transport=httpx.MockTransport(fake)).search("dentist", "Austin", 20, print)
    assert [r.rank for r in results] == [1, 3]


def test_booking_platform_counts_as_online_payment():
    full = site(pages_crawled=6, has_online_payment=False)
    with_booking = company(website_signals={**full, "has_online_booking": True})
    without = company(website_signals={**full, "has_online_booking": False, "booking_provider": None})
    assert not any(r.startswith("No online payment") for r in reasons(with_booking, "software"))
    assert any(r.startswith("No online payment") for r in reasons(without, "software"))


def test_old_founding_year_or_ppp_loan_is_never_a_new_business():
    assert "Possibly a new business +20" in reasons(company(website_signals=site(), **NEW_REVIEWS), "website")
    founded = company(website_signals=site(founded_year=THIS_YEAR - 10), **NEW_REVIEWS)
    loan = company(website_signals=site(), ppp={"jobs": 4, "loan_number": "123"}, **NEW_REVIEWS)
    for c in (founded, loan):
        assert "Possibly a new business +20" not in reasons(c, "website")


def test_ppp_names_with_html_entities_are_shown_plainly(ppp_path, tmp_path):  # noqa: F811
    # Some SBA rows really spell "&" as "&AMP;" (e.g. "TRISTAR PLUMBING &AMP; HEATING INC").
    csv_file = tmp_path / "public_up_to_150k_1_240930.csv"
    csv_file.write_bytes(csv_bytes([loan("1001", "TRISTAR PLUMBING &AMP; HEATING INC", "10 Main St",
                                         zip_code="78701", jobs="45")]))
    ppp.import_files([str(csv_file)])
    match = ppp.match_company(google_company("Tristar Plumbing & Heating", "10 Main St, Austin, TX 78701, USA"))
    assert match and match["name"] == "TRISTAR PLUMBING & HEATING INC" and match["jobs"] == 45


def test_tag_manager_flag_is_saved():
    gtm = '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"></script>' + "<p>x</p>" * 80
    assert detect_intent_signals(gtm, "https://acme.example/")["has_tag_manager"] is True
    assert detect_intent_signals("<p>hello</p>" * 80, "https://acme.example/")["has_tag_manager"] is False


def test_business_kind_is_sent_to_the_dashboard():
    out = CompanyOut.model_validate(company(website_signals=site(), id=1, intent={}, review_signals={}))
    assert out.business_kind == "appointments"
    assert estimate_intent(company(category="Something odd"))  # unknown kind still scores


def test_no_website_counts_only_when_google_confirms_it():
    google = company(website=None, domain=None, source="google")
    osm = company(website=None, domain=None, source="osm")
    assert "No website +30" in reasons(google, "website")
    assert not any(r.startswith("No website +") for r in reasons(osm, "website"))
