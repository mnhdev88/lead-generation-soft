"""Lead scores (app/scoring.py) and POST /api/scores/recompute."""
from datetime import datetime, timezone

from app.models import Company, Contact
from app.scoring import rescore, score_company, score_contact

THIS_YEAR = datetime.now(timezone.utc).year


def company(**kw) -> Company:
    fields = dict(name="Acme Dental", source="osm", source_ref="x", website="https://acme.example",
                  lead_status="new", contacts=[])
    fields.update(kw)
    return Company(**fields)


def test_empty_company_scores_zero():
    assert score_company(company(website="https://a.example")) == (0, [])


def test_company_points_add_up_with_reasons():
    owner = Contact(full_name="Ali", is_decision_maker=True, seniority="owner", email="ali@acme.example",
                    email_status="valid")
    c = company(
        email="info@acme.example", emails=["info@acme.example"], email_statuses={"info@acme.example": "valid"},
        phone="+1 555", rating=4.8, reviews_count=120, enrich_status="done",
        website_signals={"https": True, "copyright_year": THIS_YEAR, "has_contact_form": True},
        contacts=[owner],
    )
    score, reasons = score_company(c)
    assert reasons == [
        "Verified email +25", "Phone +10", "Decision-maker with email +20",
        "Rating 4.8 (120 reviews) +10", "Website crawled +5",
    ]
    assert score == 70


def test_best_email_grade_is_used():
    c = company(email="a@x.example", emails=["a@x.example", "b@x.example"],
                email_statuses={"a@x.example": "invalid", "b@x.example": "catch_all"})
    assert score_company(c)[1] == ["Catch-all email, only a guess (risky) +8"]
    c.email_sources = {"b@x.example": "website"}
    assert score_company(c)[1] == ["Catch-all email, published by the business +20"]
    c = company(email="a@x.example", emails=["a@x.example"])  # not checked yet
    assert score_company(c)[1] == ["Unverified email +8"]
    c = company(email="a@x.example", emails=["a@x.example"], email_statuses={"a@x.example": "invalid"})
    assert score_company(c) == (0, [])


def test_decision_maker_without_email_and_good_rating():
    c = company(rating=4.2, reviews_count=3, contacts=[Contact(full_name="Sara", is_decision_maker=True)])
    assert score_company(c) == (13, ["Decision-maker (no email) +8", "Rating 4.2 +5"])


def test_website_opportunities_only_after_successful_crawl():
    signals = {"https": False, "copyright_year": THIS_YEAR - 5, "has_contact_form": False}
    crawled = company(enrich_status="done", website_signals=signals)
    assert score_company(crawled) == (21, [
        "Website crawled +5", "No SSL (http only) +8",
        f"Outdated website (copyright {THIS_YEAR - 5}) +5", "No contact form +3",
    ])
    # A failed crawl (blocked/timeout) says nothing about SSL etc.
    failed = company(enrich_status="failed", website_signals=signals)
    assert score_company(failed) == (0, [])
    assert score_company(company(website=None)) == (10, ["No website +10"])


def test_closed_business_and_clamping():
    c = company(phone="1", business_status="CLOSED_TEMPORARILY")
    assert score_company(c) == (0, ["Phone +10", "Closed temporarily -30"])


def test_dead_status_scores_zero():
    c = company(phone="1", lead_status="not_interested")
    assert score_company(c) == (0, ["Status Not interested → 0", "Phone +10"])


def test_contact_score():
    co = company()
    co.score = 70
    p = Contact(full_name="Ali", email="ali@x.example", email_status="valid", is_decision_maker=True,
                seniority="owner", phone="1", linkedin_url="https://linkedin.com/in/ali", lead_status="new")
    score, reasons = score_contact(p, co)
    assert reasons == ["Verified email +40", "Decision-maker +30", "Owner / Founder +15", "Phone +5",
                       "LinkedIn +5", "Company score 70 +7"]
    assert score == 100  # 102 clamped


def test_contact_email_grades():
    assert score_contact(Contact(email="a@x.example", email_status="catch_all"))[0] == 12  # only a guess
    assert score_contact(Contact(email="a@x.example", email_status="unknown"))[0] == 10
    assert score_contact(Contact(email="a@x.example", email_status="invalid"))[0] == 0
    assert score_contact(Contact(seniority="manager"))[1] == ["Manager +5"]
    assert score_contact(Contact(seniority="director", lead_status="lost"))[0] == 0


def test_rescore_sets_company_and_contacts():
    p = Contact(full_name="Ali", is_decision_maker=True, seniority="director")
    c = company(phone="1", contacts=[p])
    rescore(c)
    assert c.score == 18 and c.score_reasons == ["Phone +10", "Decision-maker (no email) +8"]
    assert p.score == 42  # 30 decision-maker + 10 director + 2 company
    assert p.score_reasons[-1] == "Company score 18 +2"


def test_recompute_endpoint(client, db):
    c = Company(name="A", source="osm", source_ref="a", phone="123", website="https://a.example")
    c.contacts.append(Contact(full_name="Ali", is_decision_maker=True, seniority="owner"))
    c.contacts.append(Contact(full_name="Mo", title="Practice Manager"))  # saved before classification
    db.add_all([c, Contact(full_name="Loner", phone="1")])
    db.commit()

    r = client.post("/api/scores/recompute")
    assert r.status_code == 200 and r.json() == {"companies": 1, "contacts": 3}
    db.expire_all()
    assert db.get(Company, c.id).score == 18
    mo = db.query(Contact).filter_by(full_name="Mo").one()
    assert (mo.seniority, mo.department, mo.is_decision_maker) == ("manager", "management", True)
    loner = db.query(Contact).filter_by(full_name="Loner").one()
    assert loner.score == 5 and loner.score_reasons == ["Phone +5"]


def test_recompute_only_missing_scores_old_leads_once(db):
    from app.scoring import recompute_scores

    old = Company(name="Old", source="osm", source_ref="old", phone="1")
    old.contacts.append(Contact(full_name="Mo", title="Practice Manager"))
    done = Company(name="Done", source="osm", source_ref="done", score=77, score_reasons=["kept"])
    db.add_all([old, done])
    db.commit()
    assert recompute_scores(db, only_missing=True) == (1, 1)
    db.expire_all()
    assert db.get(Company, old.id).score == 28  # phone 10 + no website 10 + decision-maker 8
    assert db.get(Company, done.id).score == 77  # already scored: left alone
    mo = db.query(Contact).filter_by(full_name="Mo").one()
    assert mo.seniority == "manager" and mo.score is not None
    assert recompute_scores(db, only_missing=True) == (0, 0)  # nothing left to do


def test_catch_all_email_points_follow_the_evidence():
    from app.models import Company as C

    def person(**kw):
        return Contact(full_name="Jane Doe", email="jane@x.example", email_status="catch_all", **kw)

    cases = [
        (dict(email_source="website"), "Catch-all email, published by the business +35"),
        (dict(email_source="hunter", email_confidence=70), "Catch-all email, known to Hunter/Apollo +30"),
        (dict(email_source="pattern", email_confidence=90), "Catch-all email, matches their proven email pattern +25"),
        (dict(email_source="pattern", email_confidence=60), "Catch-all email, only a guess (risky) +12"),
    ]
    for kw, reason in cases:
        assert score_contact(person(**kw), C(name="X"))[1][0] == reason


def test_published_catch_all_email_is_preferred_over_a_guess():
    c = company(email="guess@x.example", emails=["guess@x.example", "info@x.example"],
                email_statuses={"guess@x.example": "catch_all", "info@x.example": "catch_all"},
                email_sources={"info@x.example": "website"})
    assert score_company(c)[1] == ["Catch-all email, published by the business +20"]


def test_activity_from_the_newest_google_review():
    from datetime import timedelta

    now = datetime.now(timezone.utc)

    def reasons(days_ago, reviews=30):
        c = company(reviews_count=reviews, review_signals={"newest_review": (now - timedelta(days=days_ago)).isoformat()})
        return [r for r in score_company(c)[1] if "Active" in r or "quiet" in r]

    assert reasons(10) == ["Active: a Google review in the last 3 months +5"]
    assert reasons(300) == []
    assert reasons(700) == ["Gone quiet: no Google review in 23 months -10"]
    assert reasons(700, reviews=2) == []  # just a small business
    c = company(business_status="CLOSED_PERMANENTLY", review_signals={"newest_review": now.isoformat()})
    assert not any("Active" in r for r in score_company(c)[1])
