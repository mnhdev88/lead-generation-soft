"""Google Maps source, using the Places API (New) "Text Search".

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search

How it works:
- One search ("dentist in Karachi") returns at most 60 places, 20 per page.
  Each page is one paid request, counted with usage.consume() before it is sent.
- With GOOGLE_PLACES_REVIEWS on (default) we also ask for the photos and up to 5 reviews of
  each place: how many photos (Google lists at most 10) and what customers complain about
  (app/reviews.py) help tell who needs our services. Google bills this at a higher price
  tier; set GOOGLE_PLACES_REVIEWS=0 in backend/.env to switch it off.
- To get more than 60 ("deep search"), we look up the city's map rectangle, cut it
  into a grid of smaller tiles and search each tile separately, skipping duplicates.
"""
import math

import httpx

from .. import config, usage
from ..geocode import bounding_box, geocode
from .base import CompanyResult, ProgressFn, Source, SourceError

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
USAGE_PROVIDER = "google_places"

# Only these fields are returned (and billed). nextPageToken is needed for paging.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.location",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.googleMapsUri",
    "places.businessStatus",
    "places.primaryTypeDisplayName",
    "places.primaryType",
    "places.regularOpeningHours.weekdayDescriptions",
    "nextPageToken",
])
# Extra fields when config.GOOGLE_PLACES_REVIEWS is on (see field_mask()).
REVIEW_FIELDS = ["places.reviews", "places.photos"]
REVIEW_TEXT_MAX = 1000  # characters kept per review

PAGE_SIZE = 20  # Google's maximum per page
RESULTS_PER_QUERY = 60  # Google stops after 3 pages
MAX_TILES = 16  # deep search: at most a 4x4 grid (= at most 48 requests)
TIMEOUT = 30  # seconds


class GooglePlacesSource(Source):
    name = "google"
    label = "Google Maps"
    description = "Best coverage worldwide, incl. Pakistan/India. Needs a Google API key; capped monthly."

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        # Tests pass an httpx.MockTransport here so no real network is used.
        self.transport = transport

    def unavailable_reason(self) -> str | None:
        # Read at call time so the key can be added (or monkeypatched in tests) later.
        if not config.GOOGLE_PLACES_API_KEY:
            return "Add GOOGLE_PLACES_API_KEY to backend/.env to use Google Maps, then restart the backend"
        return None

    # ----- main entry point -------------------------------------------------

    def search(self, query: str, location: str, limit: int, progress: ProgressFn) -> list[CompanyResult]:
        if reason := self.unavailable_reason():
            raise SourceError(reason)

        found: dict[str, CompanyResult] = {}  # place id -> result (dedupes automatically)
        with httpx.Client(
            transport=self.transport, timeout=TIMEOUT, headers={"User-Agent": config.USER_AGENT}
        ) as client:
            try:
                if limit <= RESULTS_PER_QUERY:
                    progress(f"Searching Google Maps for '{query}' in {location}...")
                    self._run_query(client, f"{query} in {location}", None, limit, found)
                else:
                    self._deep_search(client, query, location, limit, found, progress)
            except SourceError as e:
                # Keep what we already have (e.g. the monthly cap was hit half-way).
                if not found:
                    raise
                progress(f"{e} Keeping the {len(found)} businesses found so far.")

        return list(found.values())[:limit]

    # ----- deep search (more than 60 results) -------------------------------

    def _deep_search(
        self, client: httpx.Client, query: str, location: str, limit: int,
        found: dict[str, CompanyResult], progress: ProgressFn,
    ) -> None:
        progress(f"Finding location '{location}'...")
        try:
            box = bounding_box(geocode(client, location))
        except (SourceError, httpx.HTTPError, ValueError, KeyError):
            # Without the map area we can still do one normal search (up to 60 results).
            progress(f"Could not map '{location}', doing a normal Google search (max 60 results)...")
            self._run_query(client, f"{query} in {location}", None, limit, found)
            return

        tiles = _make_tiles(box, limit)
        for i, rect in enumerate(tiles, start=1):
            if len(found) >= limit:
                break
            progress(f"Deep search: area {i} of {len(tiles)}, {len(found)} businesses so far...")
            # The rectangle limits the area, so the text only needs the business type.
            self._run_query(client, query, rect, limit, found)

    # ----- one text query, following up to 3 pages --------------------------

    def _run_query(
        self, client: httpx.Client, text_query: str, rect: dict | None,
        limit: int, found: dict[str, CompanyResult],
    ) -> None:
        body: dict = {"textQuery": text_query, "pageSize": PAGE_SIZE}
        if rect:
            body["locationRestriction"] = {"rectangle": rect}

        position = 0  # Google's own order in this query, 1 = shown first (CompanyResult.rank)
        while len(found) < limit:
            data = self._post(client, body)
            for place in data.get("places", []):
                position += 1
                result = _to_company(place)
                if result and result.source_ref not in found and len(found) < limit:
                    result.rank = position
                    found[result.source_ref] = result
            token = data.get("nextPageToken")
            if not token:
                break
            # Next page: same request plus the token (Google requires the rest to be identical).
            body = {**body, "pageToken": token}

    def _post(self, client: httpx.Client, body: dict) -> dict:
        """Send one request to Google (counted against the monthly cap first)."""
        usage.consume(USAGE_PROVIDER)  # raises UsageLimitError when the cap is reached
        headers = {
            "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": field_mask(),
            "Content-Type": "application/json",
        }
        try:
            r = client.post(SEARCH_URL, json=body, headers=headers)
        except httpx.TimeoutException:
            raise SourceError("Google Maps did not answer in time. Try again in a minute.")
        except httpx.HTTPError as e:
            raise SourceError(f"Could not reach Google Maps ({type(e).__name__}). Check your internet connection.")
        if r.status_code != 200:
            raise SourceError(_error_message(r))
        try:
            return r.json()
        except ValueError:
            raise SourceError("Google Maps sent back an unreadable answer. Try again later.")


# ----- helpers ---------------------------------------------------------------

def field_mask() -> str:
    """The fields we ask Google for. Reads config at call time (tests switch reviews on/off)."""
    if config.GOOGLE_PLACES_REVIEWS:
        return FIELD_MASK + "," + ",".join(REVIEW_FIELDS)
    return FIELD_MASK


def _make_tiles(box: tuple[float, float, float, float], limit: int) -> list[dict]:
    """Cut the (south, north, west, east) box into an n x n grid of Google rectangles.
    About one tile per 60 wanted results, at most MAX_TILES tiles."""
    wanted = min(math.ceil(limit / RESULTS_PER_QUERY), MAX_TILES)
    n = max(1, math.isqrt(wanted - 1) + 1)  # smallest n with n*n >= wanted
    south, north, west, east = box
    lat_step = (north - south) / n
    lon_step = (east - west) / n
    tiles = []
    for row in range(n):
        for col in range(n):
            tiles.append({
                "low": {"latitude": south + row * lat_step, "longitude": west + col * lon_step},
                "high": {"latitude": south + (row + 1) * lat_step, "longitude": west + (col + 1) * lon_step},
            })
    return tiles


def _address_part(place: dict, *types: str) -> str | None:
    """Pick e.g. the city ("locality") out of Google's addressComponents list."""
    for wanted in types:
        for comp in place.get("addressComponents") or []:
            if wanted in (comp.get("types") or []):
                return comp.get("longText") or comp.get("shortText")
    return None


def _reviews(place: dict) -> list[dict]:
    """Google's reviews in our small format. Odd or empty entries are skipped."""
    out = []
    for rev in place.get("reviews") or []:
        if not isinstance(rev, dict):
            continue
        # "text" is in the requested language (English by default), "originalText" as written.
        text = ""
        for key in ("text", "originalText"):
            part = rev.get(key)
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                text = part["text"].strip()
                break
        rating = rev.get("rating")
        rating = int(rating) if isinstance(rating, (int, float)) and not isinstance(rating, bool) else None
        if not text and rating is None:
            continue
        publish = rev.get("publishTime")
        relative = rev.get("relativePublishTimeDescription")
        out.append({
            "rating": rating,
            "text": text[:REVIEW_TEXT_MAX],
            "publish_time": publish if isinstance(publish, str) and publish else None,
            "relative": relative if isinstance(relative, str) and relative else None,
        })
    return out


def _to_company(place: dict) -> CompanyResult | None:
    """Convert one Google place into our CompanyResult (None = skip it)."""
    if place.get("businessStatus") == "CLOSED_PERMANENTLY":
        return None
    name = (place.get("displayName") or {}).get("text")
    if not place.get("id") or not name:
        return None

    category = (place.get("primaryTypeDisplayName") or {}).get("text")
    if not category and place.get("primaryType"):
        category = place["primaryType"].replace("_", " ")

    hours = (place.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    location = place.get("location") or {}

    # Photos and reviews only when we asked for them; a place without photos has no "photos" key.
    photos_count, reviews = None, []
    if config.GOOGLE_PLACES_REVIEWS:
        photos = place.get("photos")
        photos_count = len(photos) if isinstance(photos, list) else 0
        reviews = _reviews(place)

    return CompanyResult(
        name=name,
        source_ref=place["id"],
        category=category,
        phone=place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber"),
        website=place.get("websiteUri"),
        address=place.get("formattedAddress"),
        city=_address_part(place, "locality", "postal_town"),
        country=_address_part(place, "country"),
        lat=location.get("latitude"),
        lon=location.get("longitude"),
        rating=place.get("rating"),
        reviews_count=place.get("userRatingCount"),
        opening_hours=("; ".join(hours)[:300] or None),
        google_maps_url=place.get("googleMapsUri"),
        business_status=place.get("businessStatus"),
        photos_count=photos_count,
        reviews=reviews,
        # Reviews and photos are kept (shortened) above, not a second time in the raw data.
        raw={k: v for k, v in place.items() if k not in ("reviews", "photos")},
    )


def _error_message(r: httpx.Response) -> str:
    """Turn a Google error response into a message a non-programmer can act on."""
    try:
        err = r.json().get("error", {})
    except (ValueError, AttributeError):
        err = {}
    google_msg = str(err.get("message") or "").strip()
    reasons = {str(d.get("reason", "")) for d in err.get("details") or [] if isinstance(d, dict)}
    text = (google_msg + " " + " ".join(reasons)).lower()

    if "api_key_invalid" in text or "api key not valid" in text or "api key expired" in text:
        return "Your Google API key is not valid. Check GOOGLE_PLACES_API_KEY in backend/.env."
    if r.status_code == 400:
        return f"Google could not understand the search request: {google_msg or 'bad request'}"
    if r.status_code == 403:
        if "billing" in text:
            return (
                "Billing is not enabled on your Google Cloud project. Open console.cloud.google.com "
                "-> Billing, link a billing account to the project, then try again."
            )
        if "service_disabled" in text or "has not been used" in text or "is disabled" in text:
            return (
                "Places API (New) is not enabled for your API key's project. Open console.cloud.google.com "
                "-> APIs & Services -> Library, search 'Places API (New)', click Enable, wait a few minutes "
                "and try again."
            )
        return (
            "Google refused the API key (403). Check that: the key in backend/.env is correct, "
            "'Places API (New)' is enabled, billing is enabled, and the key's API restrictions allow "
            f"Places API (New). Google said: {google_msg or 'permission denied'}"
        )
    if r.status_code == 429:
        return (
            "Google Maps quota exceeded (too many requests). Wait a minute and try again, or check "
            "the quotas for Places API (New) in Google Cloud Console."
        )
    if r.status_code >= 500:
        return f"Google Maps is having problems right now (HTTP {r.status_code}). Try again later."
    return f"Google Maps request failed (HTTP {r.status_code}): {google_msg or r.reason_phrase}"
