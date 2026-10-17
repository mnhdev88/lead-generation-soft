"""Every lead source (OpenStreetMap, Google Places, directories, B2B APIs...) is a
"source adapter" that follows this same interface, so new ones plug in easily."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

ProgressFn = Callable[[str], None]


@dataclass
class CompanyResult:
    name: str
    source_ref: str  # the ID of this business inside the source
    category: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    rating: float | None = None
    reviews_count: int | None = None
    opening_hours: str | None = None
    google_maps_url: str | None = None
    business_status: str | None = None
    socials: dict = field(default_factory=dict)
    # Google Maps profile only: how many photos (Google returns at most 10) and up to 5 reviews,
    # each {"rating": int|None, "text": str (<= 1000 chars), "publish_time": ISO str|None, "relative": str|None}
    photos_count: int | None = None
    rank: int | None = None  # Google only: position in its own results for the query (1 = first)
    reviews: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class SourceError(Exception):
    """A problem worth showing to the user (bad location, service down, ...)."""


class Source(ABC):
    name: str
    label: str
    description: str

    def unavailable_reason(self) -> str | None:
        """Return a message if this source can't be used right now (e.g. missing API key)."""
        return None

    @abstractmethod
    def search(self, query: str, location: str, limit: int, progress: ProgressFn) -> list[CompanyResult]:
        ...
