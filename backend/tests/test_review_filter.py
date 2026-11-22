"""Search by Google review count: min_reviews / max_reviews keep only businesses in that range."""
from sqlalchemy import select

from app import worker
from app.models import Company, JobResult, SearchJob
from app.sources import SOURCES

from .test_api import fake_sources  # noqa: F401  (fixture)
from .test_intent import FakeGoogle, _result


def _run(db, monkeypatch, results, source="google", **params):
    monkeypatch.setitem(SOURCES, source, FakeGoogle(results))
    if source != "google":
        SOURCES[source].name = source
    job = SearchJob(kind="search", query="dentist", location="Austin", source=source, limit=20, params=params)
    db.add(job)
    db.commit()
    worker.run_search_job(db, job)
    db.commit()
    return job, {c.name for c in db.scalars(select(Company))}


def test_keeps_only_the_review_range(db, monkeypatch):
    counts = [None, 3, 10, 50, 100, 400]  # None = Google gives no count: no reviews yet
    results = [_result(i, n) for i, n in enumerate(counts)]
    job, names = _run(db, monkeypatch, results, min_reviews=10, max_reviews=100)
    assert names == {"Biz 2", "Biz 3", "Biz 4"}
    assert job.result_count == 3 and len(db.scalars(select(JobResult)).all()) == 3
    assert "Kept 10-100 Google reviews, skipped 3." in job.progress_message
    # The competitors are still the whole search (median of 3, 10, 50, 100, 400 = 50).
    assert {c.peer_reviews_median for c in db.scalars(select(Company))} == {50}


def test_max_only_keeps_businesses_without_reviews(db, monkeypatch):
    results = [_result(i, n) for i, n in enumerate([None, 0, 5, 30])]
    job, names = _run(db, monkeypatch, results, max_reviews=5)
    assert names == {"Biz 0", "Biz 1", "Biz 2"}
    assert "Kept at most 5 Google reviews, skipped 1." in job.progress_message


def test_no_range_keeps_everything(db, monkeypatch):
    job, names = _run(db, monkeypatch, [_result(i, n) for i, n in enumerate([1, 900])])
    assert names == {"Biz 0", "Biz 1"} and "reviews" not in job.progress_message


def test_openstreetmap_ignores_the_range(db, monkeypatch):
    results = [_result(i, None) for i in range(3)]
    job, names = _run(db, monkeypatch, results, source="osm", min_reviews=10)
    assert len(names) == 3
    assert "only works with Google Maps" in job.progress_message


def test_api_saves_the_range(client, fake_sources):  # noqa: F811
    r = client.post("/api/jobs/batch", json={"queries": ["dentist"], "locations": ["Austin"], "source": "fake",
                                             "min_reviews": 10, "max_reviews": 100})
    assert r.status_code == 201
    params = r.json()[0]["params"]
    assert params["min_reviews"] == 10 and params["max_reviews"] == 100

    r = client.post("/api/jobs", json={"query": "dentist", "location": "Austin", "source": "fake", "min_reviews": 5})
    assert r.json()["params"]["min_reviews"] == 5 and "max_reviews" not in r.json()["params"]

    plain = client.post("/api/jobs/batch", json={"queries": ["gym"], "locations": ["Austin"], "source": "fake"})
    assert "min_reviews" not in plain.json()[0]["params"]

    for bad in ({"min_reviews": 100, "max_reviews": 10}, {"min_reviews": -1}):
        r = client.post("/api/jobs/batch", json={"queries": ["gym"], "locations": ["Austin"], "source": "fake", **bad})
        assert r.status_code == 422
