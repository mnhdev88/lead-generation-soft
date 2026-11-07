"""Your ideal customer (app/fit.py)."""
from app import fit
from app.models import Company


def biz(**kw) -> Company:
    fields = dict(name="Smile Dental", category="Dentist", source="osm", source_ref=kw.get("name", "x"),
                  city="Austin", address="1 Main St, Austin, TX", country="US", lead_status="new", contacts=[])
    fields.update(kw)
    return Company(**fields)


PROFILE = fit.clean({"business_types": ["Dentist", " orthodontist "], "exclude_types": ["hospital"],
                     "places": ["Austin"], "sizes": ["small", "medium"], "min_rating": 4.0, "min_reviews": 10})


def test_clean_profile():
    assert PROFILE["business_types"] == ["dentist", "orthodontist"]
    assert not fit.is_set(fit.clean({}))


def test_good_and_poor_fits():
    assert fit.evaluate(biz(size_band="small", rating=4.6, reviews_count=80), PROFILE) == ("good", [])
    # Unknown size / rating can't make a poor fit.
    assert fit.evaluate(biz(), PROFILE) == ("good", [])
    status, reasons = fit.evaluate(biz(name="City Gym", category="Gym", city="Dallas", address="Dallas, TX",
                                       size_band="large", rating=3.2, reviews_count=4), PROFILE)
    assert status == "poor"
    assert reasons == [
        "Not a business type you target (Gym)",
        "Outside your places (Dallas)",
        "Size 51+ people, you want 2-10 people, 11-50 people",
        "Rating 3.2, below your 4",
        "4 reviews, below your 10",
    ]
    assert fit.evaluate(biz(name="Austin Dental Hospital"), PROFILE)[1] == ["A business type you leave out (hospital)"]


def test_no_profile_means_no_fit():
    assert fit.evaluate(biz(), fit.clean({})) == (None, [])


def test_saving_the_profile_rescores_and_filters(client, db):
    db.add_all([biz(name="Smile Dental"), biz(name="City Gym", category="Gym")])
    db.commit()
    r = client.put("/api/quality/ideal-customer", json={"business_types": ["dentist"]})
    assert r.status_code == 200
    assert (r.json()["good"], r.json()["poor"]) == (1, 1)
    assert client.get("/api/quality/ideal-customer").json()["profile"]["business_types"] == ["dentist"]
    rows = {c["name"]: c for c in client.get("/api/companies").json()["items"]}
    assert rows["City Gym"]["fit"] == "poor"
    assert any(r.startswith("Outside your ideal customer") for r in rows["City Gym"]["score_reasons"])
    assert [c["name"] for c in client.get("/api/companies", params={"fit": "good"}).json()["items"]] == ["Smile Dental"]
    # Clearing it: no fit, no penalty.
    client.put("/api/quality/ideal-customer", json={})
    assert {c["fit"] for c in client.get("/api/companies").json()["items"]} == {None}
