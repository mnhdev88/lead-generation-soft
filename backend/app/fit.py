"""Your ideal customer: what a good lead looks like for you, set once on the Quality page.

    {"business_types": ["dentist", "orthodontist"], "exclude_types": ["hospital"],
     "places": ["austin", "texas"], "sizes": ["small", "medium"],
     "min_rating": 4.0, "min_reviews": 10}

Every part is optional (empty = anything goes). A business is a "good" fit when it breaks
none of them and a "poor" fit otherwise, with the reasons ("Size 51+ people, you want ...").
Something we don't know (no size estimate, no rating) never makes a poor fit: we can't tell.
No profile set = no fit at all (None), and nothing changes in the scores.

The profile is kept in memory (init_db() loads it at start-up, save() updates it), so
scoring a business needs no database query.
"""
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Company, Setting

SETTING_KEY = "ideal_customer"
SIZE_LABELS = {"solo": "1 person", "small": "2-10 people", "medium": "11-50 people", "large": "51+ people"}
EMPTY = {"business_types": [], "exclude_types": [], "places": [], "sizes": [], "min_rating": None,
         "min_reviews": None}

_lock = threading.Lock()
_profile: dict = dict(EMPTY)


def clean(profile: dict | None) -> dict:
    """Keep the known keys; lower-case words without blanks or repeats."""
    profile = profile or {}
    out = dict(EMPTY)
    for key in ("business_types", "exclude_types", "places"):
        words: list[str] = []
        for w in profile.get(key) or []:
            w = " ".join(str(w).split()).lower()
            if w and w not in words:
                words.append(w)
        out[key] = words[:100]
    out["sizes"] = [s for s in SIZE_LABELS if s in (profile.get("sizes") or [])]
    out["min_rating"] = profile.get("min_rating")
    out["min_reviews"] = profile.get("min_reviews")
    return out


def is_set(profile: dict) -> bool:
    lists = any(profile.get(k) for k in ("business_types", "exclude_types", "places", "sizes"))
    return lists or profile.get("min_rating") is not None or profile.get("min_reviews") is not None


def current() -> dict:
    with _lock:
        return dict(_profile)


def _remember(profile: dict) -> None:
    global _profile
    with _lock:
        _profile = dict(profile)


def reload(session: Session) -> dict:
    """Read the profile from the database into memory."""
    row = session.scalar(select(Setting).where(Setting.key == SETTING_KEY))
    profile = clean(row.value if row else None)
    _remember(profile)
    return profile


def save(session: Session, profile: dict) -> dict:
    """Store a new profile (the caller commits; rescoring then uses it)."""
    profile = clean(profile)
    row = session.scalar(select(Setting).where(Setting.key == SETTING_KEY))
    if row is None:
        row = Setting(key=SETTING_KEY)
        session.add(row)
    row.value = profile
    _remember(profile)
    return profile


def _contains_any(text: str, words: list[str]) -> str | None:
    text = " ".join(text.lower().split())
    return next((w for w in words if w in text), None)


def evaluate(company: Company, profile: dict | None = None) -> tuple[str | None, list[str]]:
    """("good" | "poor" | None, the reasons it is a poor fit)."""
    profile = current() if profile is None else profile
    if not is_set(profile):
        return None, []
    reasons: list[str] = []
    kind = " ".join(filter(None, [company.category, company.name]))
    if profile["business_types"] and not _contains_any(kind, profile["business_types"]):
        reasons.append(f"Not a business type you target ({company.category or 'type unknown'})")
    excluded = _contains_any(kind, profile["exclude_types"]) if profile["exclude_types"] else None
    if excluded:
        reasons.append(f"A business type you leave out ({excluded})")
    if profile["places"]:
        where = " ".join(filter(None, [company.address, company.city, company.country]))
        if where and not _contains_any(where, profile["places"]):
            reasons.append(f"Outside your places ({company.city or company.address})")
    if profile["sizes"] and company.size_band and company.size_band not in profile["sizes"]:
        wanted = ", ".join(SIZE_LABELS[s] for s in profile["sizes"])
        reasons.append(f"Size {SIZE_LABELS.get(company.size_band, company.size_band)}, you want {wanted}")
    min_rating = profile.get("min_rating")
    if min_rating is not None and company.rating is not None and company.rating < min_rating:
        reasons.append(f"Rating {company.rating:g}, below your {min_rating:g}")
    min_reviews = profile.get("min_reviews")
    if min_reviews is not None and company.reviews_count is not None and company.reviews_count < min_reviews:
        reasons.append(f"{company.reviews_count} reviews, below your {min_reviews}")
    return ("poor" if reasons else "good"), reasons


def apply_fit(company: Company) -> None:
    company.fit, company.fit_reasons = evaluate(company)
