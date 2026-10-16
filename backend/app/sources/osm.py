"""OpenStreetMap source: free, no API key.

1. Nominatim turns "Karachi, Pakistan" into a map area.
2. Overpass finds businesses of the requested category inside that area.

Coverage is very good in USA/Canada, patchier in Pakistan/India; Google Places
will be added as a second source for better coverage.
"""
import re
import time
from urllib.parse import urlparse

import httpx

from ..config import OVERPASS_ROUNDS, OVERPASS_URLS, USER_AGENT
from ..geocode import geocode
from .base import CompanyResult, ProgressFn, Source, SourceError

# Common business categories -> OpenStreetMap tags.
CATEGORY_TAGS: dict[str, list[tuple[str, str]]] = {
    "restaurant": [("amenity", "restaurant")],
    "fast food": [("amenity", "fast_food")],
    "cafe": [("amenity", "cafe")],
    "coffee shop": [("amenity", "cafe")],
    "bar": [("amenity", "bar"), ("amenity", "pub")],
    "dentist": [("amenity", "dentist"), ("healthcare", "dentist")],
    "doctor": [("amenity", "doctors"), ("healthcare", "doctor")],
    "clinic": [("amenity", "clinic"), ("healthcare", "clinic")],
    "hospital": [("amenity", "hospital")],
    "pharmacy": [("amenity", "pharmacy")],
    "veterinary": [("amenity", "veterinary")],
    "vet": [("amenity", "veterinary")],
    "school": [("amenity", "school")],
    "college": [("amenity", "college")],
    "university": [("amenity", "university")],
    "bank": [("amenity", "bank")],
    "hotel": [("tourism", "hotel")],
    "guest house": [("tourism", "guest_house")],
    "gym": [("leisure", "fitness_centre")],
    "fitness": [("leisure", "fitness_centre")],
    "salon": [("shop", "hairdresser"), ("shop", "beauty")],
    "hair salon": [("shop", "hairdresser")],
    "barber": [("shop", "hairdresser")],
    "beauty": [("shop", "beauty")],
    "spa": [("leisure", "spa"), ("shop", "massage")],
    "lawyer": [("office", "lawyer")],
    "law firm": [("office", "lawyer")],
    "accountant": [("office", "accountant")],
    "real estate": [("office", "estate_agent")],
    "estate agent": [("office", "estate_agent")],
    "insurance": [("office", "insurance")],
    "it company": [("office", "it")],
    "software company": [("office", "it")],
    "marketing agency": [("office", "advertising_agency")],
    "travel agency": [("shop", "travel_agency"), ("office", "travel_agent")],
    "car dealer": [("shop", "car")],
    "car repair": [("shop", "car_repair")],
    "mechanic": [("shop", "car_repair")],
    "bakery": [("shop", "bakery")],
    "supermarket": [("shop", "supermarket")],
    "grocery": [("shop", "convenience"), ("shop", "supermarket")],
    "clothing store": [("shop", "clothes")],
    "clothes": [("shop", "clothes")],
    "electronics": [("shop", "electronics")],
    "mobile phone shop": [("shop", "mobile_phone")],
    "furniture": [("shop", "furniture")],
    "jewelry": [("shop", "jewelry")],
    "jeweller": [("shop", "jewelry")],
    "hardware store": [("shop", "hardware")],
    "optician": [("shop", "optician")],
    "florist": [("shop", "florist")],
    "plumber": [("craft", "plumber")],
    "electrician": [("craft", "electrician")],
    "carpenter": [("craft", "carpenter")],
    "photographer": [("craft", "photographer"), ("shop", "photo")],
}

BUSINESS_KEYS = ("amenity", "shop", "office", "craft", "healthcare", "tourism", "leisure")
SOCIAL_KEYS = {
    "facebook": ("contact:facebook", "facebook"),
    "instagram": ("contact:instagram", "instagram"),
    "twitter": ("contact:twitter", "twitter"),
    "linkedin": ("contact:linkedin", "linkedin"),
    "youtube": ("contact:youtube", "youtube"),
}


def _tags_for(query: str) -> list[tuple[str, str]] | None:
    q = query.strip().lower()
    candidates = [q, q.rstrip("s"), q[:-2] if q.endswith("es") else q]
    for c in candidates:
        if c in CATEGORY_TAGS:
            return CATEGORY_TAGS[c]
    return None


def _first(tags: dict, *keys: str) -> str | None:
    for k in keys:
        if tags.get(k):
            return tags[k].strip()
    return None


class OpenStreetMapSource(Source):
    name = "osm"
    label = "OpenStreetMap (free)"
    description = "Free business data. Best coverage in USA/Canada; partial in Pakistan/India."

    def __init__(self) -> None:
        self.headers = {"User-Agent": USER_AGENT}

    def _geocode(self, client: httpx.Client, location: str) -> dict:
        return geocode(client, location)  # shared Nominatim helper (app/geocode.py)

    def _area_filter(self, place: dict) -> tuple[str, str]:
        """Returns (setup statement, filter) - city boundary if available, else bounding box."""
        if place.get("osm_type") == "relation":
            area_id = 3600000000 + int(place["osm_id"])
            return f"area(id:{area_id})->.a;", "(area.a)"
        south, north, west, east = place["boundingbox"]
        return "", f"({south},{west},{north},{east})"

    def _build_query(self, query: str, place: dict, limit: int) -> str:
        setup, area = self._area_filter(place)
        tags = _tags_for(query)
        if tags:
            parts = [f'nwr["{k}"="{v}"]["name"]{area};' for k, v in tags]
        else:
            # Unknown category: match business names containing the words.
            pattern = re.escape(query.strip()).replace('"', "")
            parts = [f'nwr["name"~"{pattern}",i]["{k}"]{area};' for k in BUSINESS_KEYS]
        return f"[out:json][timeout:120];{setup}({''.join(parts)});out center {limit};"

    def _parse(self, el: dict, query: str) -> CompanyResult | None:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            return None
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        street = " ".join(p for p in [tags.get("addr:housenumber"), tags.get("addr:street")] if p)
        address = ", ".join(
            p for p in [street, tags.get("addr:suburb"), tags.get("addr:city"), tags.get("addr:postcode")] if p
        )
        category = next((tags[k] for k in BUSINESS_KEYS if tags.get(k)), query)
        socials = {}
        for net, keys in SOCIAL_KEYS.items():
            if v := _first(tags, *keys):
                socials[net] = v
        return CompanyResult(
            name=name,
            source_ref=f"{el['type']}/{el['id']}",
            category=category.replace("_", " "),
            phone=_first(tags, "phone", "contact:phone", "contact:mobile", "mobile"),
            email=_first(tags, "email", "contact:email"),
            website=_first(tags, "website", "contact:website", "url"),
            address=address or None,
            city=tags.get("addr:city"),
            country=tags.get("addr:country"),
            lat=lat,
            lon=lon,
            opening_hours=tags.get("opening_hours"),
            socials=socials,
            raw=tags,
        )

    def search(self, query: str, location: str, limit: int, progress: ProgressFn) -> list[CompanyResult]:
        with httpx.Client(headers=self.headers, timeout=180) as client:
            progress(f"Finding location '{location}'...")
            place = self._geocode(client, location)
            area_name = place.get("display_name", location)
            progress(f"Searching for '{query}' in {area_name}...")
            elements = self._overpass(client, self._build_query(query, place, limit), progress)
        results = [c for el in elements if (c := self._parse(el, query))]
        return results[:limit]

    def _overpass(self, client: httpx.Client, overpass_query: str, progress: ProgressFn) -> list[dict]:
        """Try each public Overpass server; if all are busy, wait and try again."""
        for round_no in range(1, OVERPASS_ROUNDS + 1):
            for url in OVERPASS_URLS:
                server = urlparse(url).hostname
                try:
                    r = client.post(url, data={"data": overpass_query})
                except httpx.HTTPError:
                    continue
                if r.status_code == 200:
                    try:
                        return r.json().get("elements", [])
                    except ValueError:
                        continue  # server returned an error page instead of JSON
                if r.status_code == 400:
                    raise SourceError("The search could not be understood by OpenStreetMap. Try different words.")
                progress(f"Map server {server} is busy, trying another (attempt {round_no}/{OVERPASS_ROUNDS})...")
            if round_no < OVERPASS_ROUNDS:
                wait = 20 * round_no
                progress(f"All map servers are busy. Retrying in {wait} seconds...")
                time.sleep(wait)
        raise SourceError(
            "All free OpenStreetMap servers are busy right now. Try again in a few minutes, "
            "or search a smaller area."
        )
