from .base import CompanyResult, Source, SourceError
from .google_places import GooglePlacesSource
from .osm import OpenStreetMapSource

# Register new sources here (directories, B2B providers...). The first one is the default.
SOURCES: dict[str, Source] = {s.name: s for s in [OpenStreetMapSource(), GooglePlacesSource()]}

__all__ = ["SOURCES", "CompanyResult", "Source", "SourceError"]
