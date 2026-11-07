"""Websites that aren't really one: parked, suspended, placeholder, dead."""
import pytest

from app.enrich.extract import site_state
from app.intent import estimate_intent
from app.models import Company
from app.scoring import score_company


@pytest.mark.parametrize("text, html, title, state", [
    ("This domain is for sale! Make an offer.", "", None, "parked"),
    ("Related searches", '<script src="https://www.sedoparking.com/x.js"></script>', None, "parked"),
    ("Account Suspended. Please contact your hosting provider.", "", None, "suspended"),
    ("If you see this page, the nginx web server is successfully installed.", "", "Welcome to nginx!", "placeholder"),
    ("Our new website is coming soon. Call us!", "", "Acme Dental", "placeholder"),
    ("Acme Dental. " + "We look after your teeth. " * 100 + "New menu coming soon.", "", "Acme Dental", None),
    ("Welcome to Acme Dental, family dentistry in Austin.", "", "Acme Dental", None),
])
def test_site_state(text, html, title, state):
    assert site_state(text, html, title) == state


def company(**kw):
    fields = dict(name="Acme", source="google", source_ref="x", website="https://acme.example",
                  domain="acme.example", enrich_status="done", lead_status="new", contacts=[],
                  business_status="OPERATIONAL", socials={},
                  website_signals={"pages_crawled": 1, "https": False, "copyright_year": 2015,
                                   "has_analytics": False, "has_search_console": False})
    fields.update(kw)
    return Company(**fields)


def test_parked_website_counts_as_no_working_website():
    c = company(website_signals={**company().website_signals, "site_state": "parked"})
    website = estimate_intent(c)["website"]["reasons"]
    assert website == ["Website parked / for sale: no working website +30"]  # no "No SSL" from the parking page
    assert "Website parked / for sale +10" in score_company(c)[1]
    assert not any("crawled" in r for r in score_company(c)[1])
    assert not any("No SSL" in r for r in score_company(c)[1])


def test_a_real_site_keeps_its_facts():
    reasons = estimate_intent(company())["website"]["reasons"]
    assert "No SSL (http only) +15" in reasons
