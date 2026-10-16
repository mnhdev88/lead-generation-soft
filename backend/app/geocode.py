"""Shared helper: turn a place name ("Karachi, Pakistan") into map coordinates using
OpenStreetMap's free Nominatim service. Used by the OpenStreetMap and Google sources."""
import httpx

from .config import NOMINATIM_URL
from .sources.base import SourceError


def geocode(client: httpx.Client, location: str) -> dict:
    """Return Nominatim's best match for `location` (a dict with "boundingbox",
    "display_name", "osm_type", "osm_id", "address": {"country_code": "pk", ...}, ...). Raises SourceError if nothing matches.
    The client should send a User-Agent header (Nominatim requires one)."""
    r = client.get(NOMINATIM_URL, params={"q": location, "format": "json", "limit": 1, "addressdetails": 1})
    r.raise_for_status()
    hits = r.json()
    if not hits:
        raise SourceError(f"Could not find the location '{location}'. Try 'City, Country'.")
    return hits[0]


def bounding_box(place: dict) -> tuple[float, float, float, float]:
    """Nominatim gives ["south", "north", "west", "east"] as strings -> floats."""
    south, north, west, east = (float(v) for v in place["boundingbox"])
    return south, north, west, east
