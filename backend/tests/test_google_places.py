"""Tests for the Google Places source and the monthly usage cap.
No real network: Google and Nominatim are faked with httpx.MockTransport."""
import os
import tempfile

# Use a throw-away database (unless a conftest already chose one) BEFORE importing the app.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/test_google.db")

import json  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import config, usage  # noqa: E402
from app.sources import SOURCES  # noqa: E402
from app.sources import google_places as gp  # noqa: E402
from app.sources.base import SourceError  # noqa: E402


# ----- fake data ---------------------------------------------------------------

def make_place(i: int, status: str = "OPERATIONAL", **extra) -> dict:
    place = {
        "id": f"place{i}",
        "displayName": {"text": f"Clinic {i}", "languageCode": "en"},
        "formattedAddress": f"{i} Main St, Karachi, Pakistan",
        "addressComponents": [
            {"longText": "Karachi", "shortText": "Karachi", "types": ["locality", "political"]},
            {"longText": "Pakistan", "shortText": "PK", "types": ["country", "political"]},
        ],
        "location": {"latitude": 24.8 + i / 1000, "longitude": 67.0},
        "internationalPhoneNumber": f"+92 21 000{i}",
        "nationalPhoneNumber": f"021 000{i}",
        "websiteUri": f"https://clinic{i}.example.com/",
        "rating": 4.5,
        "userRatingCount": 10 + i,
        "googleMapsUri": f"https://maps.google.com/?cid={i}",
        "businessStatus": status,
        "primaryTypeDisplayName": {"text": "Dentist", "languageCode": "en"},
        "primaryType": "dentist",
        "regularOpeningHours": {"weekdayDescriptions": ["Monday: 9 AM-5 PM", "Tuesday: Closed"]},
    }
    place.update(extra)
    return place


NOMINATIM_HIT = [{
    "display_name": "Karachi, Pakistan",
    "boundingbox": ["24.0", "26.0", "66.0", "68.0"],  # south, north, west, east
    "osm_type": "relation",
    "osm_id": 1,
}]


class FakeGoogle:
    """Answers Text Search requests. `pages` = list of place-lists, served one per page.
    With `per_tile`, every request gets fresh unique places (to test deep search)."""

    def __init__(self, pages=None, status=200, error=None, per_tile=False):
        self.pages = pages or []
        self.status = status
        self.error = error
        self.per_tile = per_tile
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []
        self.nominatim_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "nominatim" in request.url.host:
            self.nominatim_calls += 1
            return httpx.Response(200, json=NOMINATIM_HIT)
        assert str(request.url) == gp.SEARCH_URL
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(request.headers)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": self.error or {}})
        if self.per_tile:
            n = len(self.requests)
            places = [make_place(n * 100 + k) for k in range(20)]
            # Each tile has 1 page only, plus one duplicate of the very first place.
            places.append(make_place(100))
            return httpx.Response(200, json={"places": places})
        page = int(body.get("pageToken", "0"))
        data = {"places": self.pages[page]}
        if page + 1 < len(self.pages):
            data["nextPageToken"] = str(page + 1)
        return httpx.Response(200, json=data)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "test-key")


@pytest.fixture
def usage_calls(monkeypatch):
    """Replace the DB-backed usage counter with an in-memory one (cap = 1000)."""
    calls = {"n": 0, "cap": 1000}

    def fake_consume(provider, n=1):
        assert provider == "google_places"
        if calls["n"] + n > calls["cap"]:
            raise usage.UsageLimitError(f"Monthly Google Places limit reached ({calls['n']}/{calls['cap']}).")
        calls["n"] += n

    monkeypatch.setattr(usage, "consume", fake_consume)
    return calls


def run(fake: FakeGoogle, limit: int = 20, query="dentist", location="Karachi, Pakistan"):
    messages: list[str] = []
    src = gp.GooglePlacesSource(transport=httpx.MockTransport(fake))
    results = src.search(query, location, limit, messages.append)
    return results, messages


# ----- tests -------------------------------------------------------------------

def test_registered_after_osm():
    assert list(SOURCES)[:2] == ["osm", "google"]


def test_missing_key_makes_source_unavailable(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "")
    src = gp.GooglePlacesSource()
    assert src.unavailable_reason() == "Add GOOGLE_PLACES_API_KEY to backend/.env to use Google Maps, then restart the backend"
    with pytest.raises(SourceError):
        src.search("dentist", "Karachi", 10, lambda m: None)
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "abc")
    assert src.unavailable_reason() is None


def test_mapping_and_request_shape(api_key, usage_calls):
    long_hours = [f"Day {i}: 9 AM-5 PM, 6 PM-11 PM" for i in range(20)]
    fake = FakeGoogle(pages=[[make_place(1, regularOpeningHours={"weekdayDescriptions": long_hours})]])
    results, _ = run(fake)

    body, headers = fake.requests[0], fake.headers[0]
    assert body == {"textQuery": "dentist in Karachi, Pakistan", "pageSize": 20}
    assert headers["X-Goog-Api-Key"] == "test-key"
    assert headers["X-Goog-FieldMask"] == gp.field_mask()
    assert "nextPageToken" in gp.FIELD_MASK.split(",")

    [c] = results
    assert c.name == "Clinic 1"
    assert c.source_ref == "place1"
    assert c.category == "Dentist"
    assert c.phone == "+92 21 0001"
    assert c.website == "https://clinic1.example.com/"
    assert c.address == "1 Main St, Karachi, Pakistan"
    assert (c.city, c.country) == ("Karachi", "Pakistan")
    assert c.lat == pytest.approx(24.801) and c.lon == 67.0
    assert c.rating == 4.5 and c.reviews_count == 11
    assert c.google_maps_url == "https://maps.google.com/?cid=1"
    assert c.business_status == "OPERATIONAL"
    assert len(c.opening_hours) == 300 and c.opening_hours.startswith("Day 0: 9 AM-5 PM, 6 PM-11 PM; Day 1")
    assert c.raw["id"] == "place1"
    assert usage_calls["n"] == 1


def test_mapping_fallbacks():
    place = make_place(2)
    del place["internationalPhoneNumber"], place["primaryTypeDisplayName"], place["regularOpeningHours"]
    c = gp._to_company(place)
    assert c.phone == "021 0002"
    assert c.category == "dentist"
    assert c.opening_hours is None
    assert gp._to_company({"id": "x"}) is None  # no name -> skipped


def google_review(rating=2, text="Nobody answers the phone.", **extra) -> dict:
    review = {
        "name": "places/x/reviews/1",
        "relativePublishTimeDescription": "2 months ago",
        "rating": rating,
        "text": {"text": text, "languageCode": "en"},
        "originalText": {"text": text, "languageCode": "en"},
        "authorAttribution": {"displayName": "Sam", "uri": "https://maps.google.com/contrib/1"},
        "publishTime": "2026-08-01T10:20:30.123456789Z",
    }
    review.update(extra)
    return review


def test_reviews_and_photos(api_key, usage_calls, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_PLACES_REVIEWS", True)
    place = make_place(1, photos=[{"name": f"places/1/photos/{k}"} for k in range(10)], reviews=[
        google_review(),
        google_review(rating=5, text="x" * 1500, publishTime=None, relativePublishTimeDescription=None),
        # No translated text: fall back to the original.
        google_review(rating=4.0, text=None, originalText={"text": "  Bueno  ", "languageCode": "es"}),
        google_review(rating=None, text=None, originalText=None),  # nothing useful -> skipped
        "odd entry",
    ])
    fake = FakeGoogle(pages=[[place, make_place(2)]])
    results, _ = run(fake)

    mask = fake.headers[0]["X-Goog-FieldMask"].split(",")
    assert "places.reviews" in mask and "places.photos" in mask

    one, two = results
    assert one.photos_count == 10
    assert one.reviews == [
        {"rating": 2, "text": "Nobody answers the phone.", "publish_time": "2026-08-01T10:20:30.123456789Z",
         "relative": "2 months ago"},
        {"rating": 5, "text": "x" * 1000, "publish_time": None, "relative": None},
        {"rating": 4, "text": "Bueno", "publish_time": "2026-08-01T10:20:30.123456789Z", "relative": "2 months ago"},
    ]
    assert "reviews" not in one.raw and "photos" not in one.raw  # not stored twice
    assert two.photos_count == 0 and two.reviews == []  # no photos/reviews at Google


def test_reviews_switched_off(api_key, usage_calls, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_PLACES_REVIEWS", False)
    place = make_place(1, photos=[{"name": "p"}], reviews=[google_review()])
    fake = FakeGoogle(pages=[[place]])
    [c], _ = run(fake)
    assert fake.headers[0]["X-Goog-FieldMask"] == gp.FIELD_MASK
    assert "places.reviews" not in gp.FIELD_MASK
    assert c.photos_count is None and c.reviews == []


def test_pagination_follows_next_page_token(api_key, usage_calls):
    pages = [[make_place(i) for i in range(p * 20, p * 20 + 20)] for p in range(3)]
    fake = FakeGoogle(pages=pages)
    results, _ = run(fake, limit=60)
    assert len(results) == 60
    assert [r.get("pageToken") for r in fake.requests] == [None, "1", "2"]
    # Everything except the token must stay identical between pages.
    assert all(r["textQuery"] == "dentist in Karachi, Pakistan" for r in fake.requests)
    assert usage_calls["n"] == 3


def test_closed_places_skipped(api_key, usage_calls):
    fake = FakeGoogle(pages=[[make_place(1), make_place(2, status="CLOSED_PERMANENTLY"),
                              make_place(3, status="CLOSED_TEMPORARILY")]])
    results, _ = run(fake)
    assert [r.source_ref for r in results] == ["place1", "place3"]


def test_duplicates_removed(api_key, usage_calls):
    fake = FakeGoogle(pages=[[make_place(1), make_place(2)], [make_place(2), make_place(3)]])
    results, _ = run(fake)
    assert [r.source_ref for r in results] == ["place1", "place2", "place3"]


def test_limit_respected_and_stops_paging(api_key, usage_calls):
    pages = [[make_place(i) for i in range(p * 20, p * 20 + 20)] for p in range(3)]
    fake = FakeGoogle(pages=pages)
    results, _ = run(fake, limit=25)
    assert len(results) == 25
    assert len(fake.requests) == 2  # no need for page 3


def test_deep_search_uses_tiles(api_key, usage_calls):
    fake = FakeGoogle(per_tile=True)
    results, messages = run(fake, limit=130)
    assert fake.nominatim_calls == 1
    # 130 wanted -> 3 tiles -> 2x2 grid. Each fake tile gives 20 new places (+1 duplicate),
    # so the grid runs out at 80 unique places.
    assert len(results) == 80
    assert len({r.source_ref for r in results}) == 80  # deduped
    first = fake.requests[0]
    assert first["textQuery"] == "dentist"
    rect = first["locationRestriction"]["rectangle"]
    assert rect == {"low": {"latitude": 24.0, "longitude": 66.0}, "high": {"latitude": 25.0, "longitude": 67.0}}
    assert len(fake.requests) == 4  # 4 tiles x 1 page, then the grid is used up
    assert any("Deep search: area 1 of 4" in m for m in messages)


def test_deep_search_stops_at_limit(api_key, usage_calls):
    fake = FakeGoogle(per_tile=True)
    results, _ = run(fake, limit=61)
    assert len(results) == 61
    assert len(fake.requests) == 4


def test_deep_search_falls_back_when_location_unknown(api_key, usage_calls):
    def handler(request: httpx.Request) -> httpx.Response:
        if "nominatim" in request.url.host:
            return httpx.Response(200, json=[])  # location not found
        return httpx.Response(200, json={"places": [make_place(1)]})

    src = gp.GooglePlacesSource(transport=httpx.MockTransport(handler))
    messages: list[str] = []
    results = src.search("dentist", "Atlantis", 200, messages.append)
    assert [r.source_ref for r in results] == ["place1"]
    assert any("normal Google search" in m for m in messages)


def test_make_tiles_capped():
    tiles = gp._make_tiles((0.0, 4.0, 0.0, 4.0), limit=10_000)
    assert len(tiles) == gp.MAX_TILES
    assert tiles[-1]["high"] == {"latitude": 4.0, "longitude": 4.0}
    assert len(gp._make_tiles((0.0, 1.0, 0.0, 1.0), limit=61)) == 4


def test_403_message_explains_fix(api_key, usage_calls):
    fake = FakeGoogle(status=403, error={
        "code": 403, "status": "PERMISSION_DENIED",
        "message": "Places API (New) has not been used in project 123 before or it is disabled.",
        "details": [{"reason": "SERVICE_DISABLED"}],
    })
    with pytest.raises(SourceError, match=r"Places API \(New\) is not enabled.*Enable"):
        run(fake)

    fake = FakeGoogle(status=403, error={"code": 403, "message": "This API method requires billing to be enabled.",
                                         "details": [{"reason": "BILLING_DISABLED"}]})
    with pytest.raises(SourceError, match="Billing is not enabled"):
        run(fake)


def test_invalid_key_and_quota_messages(api_key, usage_calls):
    fake = FakeGoogle(status=400, error={"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                         "details": [{"reason": "API_KEY_INVALID"}]})
    with pytest.raises(SourceError, match="API key is not valid"):
        run(fake)
    with pytest.raises(SourceError, match="quota exceeded"):
        run(FakeGoogle(status=429, error={"code": 429, "message": "Quota exceeded"}))


def test_cap_reached_before_anything_found_raises(api_key, usage_calls):
    usage_calls["cap"] = 0
    fake = FakeGoogle(pages=[[make_place(1)]])
    with pytest.raises(SourceError, match="limit reached"):
        run(fake)
    assert fake.requests == []  # nothing was sent to Google


def test_cap_reached_midway_returns_partial(api_key, usage_calls):
    usage_calls["cap"] = 2
    pages = [[make_place(i) for i in range(p * 20, p * 20 + 20)] for p in range(3)]
    fake = FakeGoogle(pages=pages)
    results, messages = run(fake, limit=60)
    assert len(results) == 40
    assert len(fake.requests) == 2
    assert "limit reached" in messages[-1] and "40" in messages[-1]


# ----- usage counter (real temp database) ---------------------------------------

@pytest.fixture
def usage_db(monkeypatch):
    from app.db import engine, init_db

    if not str(engine.url).endswith(".db") or str(config.DATA_DIR) in str(engine.url):
        pytest.skip("refusing to write usage rows into the real database")
    init_db()
    monkeypatch.setitem(usage.PROVIDERS, "test_provider", 3)
    from app.db import SessionLocal
    from app.models import ApiUsage

    with SessionLocal() as s:
        s.query(ApiUsage).filter(ApiUsage.provider == "test_provider").delete()
        s.commit()


def test_current_period_format():
    period = usage.current_period()
    assert len(period) == 7 and period[4] == "-" and period.replace("-", "").isdigit()


def test_usage_counts_and_enforces_cap(usage_db):
    assert usage.get_usage("test_provider") == (0, 3)
    usage.consume("test_provider")
    usage.consume("test_provider", 2)
    assert usage.get_usage("test_provider") == (3, 3)
    with pytest.raises(usage.UsageLimitError, match=r"limit reached \(3/3\)"):
        usage.consume("test_provider")
    assert usage.get_usage("test_provider") == (3, 3)  # failed call did not count
    assert issubclass(usage.UsageLimitError, SourceError)


def test_google_limit_comes_from_config():
    assert usage.PROVIDERS["google_places"] == config.GOOGLE_PLACES_MONTHLY_LIMIT
    with pytest.raises(ValueError):
        usage.get_usage("nope")
