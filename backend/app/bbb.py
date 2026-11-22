"""Better Business Bureau (BBB) facts about a business, read from web search results.

We never open bbb.org (its terms forbid scraping). One web search (app/providers/websearch.py)
is asked for e.g.

    site:bbb.org/us "Smile Dental" Austin TX

and the results' links, titles and snippets are read here:

    url     https://www.bbb.org/us/tx/austin/profile/dentist/smile-dental-0825-1000130628
    title   "Smile Dental | Better Business Bureau® Profile"
    snippet "BBB accredited since 5/30/2008. Dentist in Austin, TX. A+ rating. 3 complaints ..."
    -> {"found": True, "url": "...", "name": "Smile Dental", "accredited": True,
        "accredited_since": "5/30/2008", "rating": "A+", "complaints": 3, "started": 1998}

Why it helps a sale: complaints and a low rating are something to talk about, and a business that
pays for BBB accreditation spends money on its reputation.

Precision first: a fact is only kept when the result says it plainly. A profile only counts when
its link is a BBB business profile, its name is this business's name, and its state (from the
link) is the business's state when we know it. Snippets differ from search to search, so a fact
the snippet doesn't mention stays None (can't tell), never "no".
"""
import re
from datetime import datetime, timezone

# dedupe, people.linkedin and ppp are imported inside the functions: intent.py imports this
# module for talking_points(), and they import intent.py (through scoring.py).

# https://www.bbb.org/us/tx/austin/profile/dentist/smile-dental-0825-1000130628[/...]
PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?bbb\.org/us/([a-z]{2})/([a-z0-9-]+)/profile/([a-z0-9-]+)/([a-z0-9-]+?-\d{4}-\d+)(?:[/?#]|$)",
    re.I,
)
TITLE_SPLIT_RE = re.compile(r"\s+[|–—-]\s+")  # "Smile Dental | BBB Business Profile"

NOT_ACCREDITED_RE = re.compile(r"\bnot\s+(?:a\s+)?BBB[\s-]*accredited\b|\bnon[\s-]?accredited\b|\bnot accredited\b", re.I)
ACCREDITED_SINCE_RE = re.compile(r"\baccredited\s+since\s*:?\s*(\d{1,2}/\d{1,2}/\d{4}|\d{4})", re.I)
ACCREDITED_RE = re.compile(r"\bBBB\s+accredited\s+business\b|\baccredited\s+since\b", re.I)
# Letter grades. Case-sensitive so "a rating" in a sentence is not an A.
GRADE = r"(A\+|A-|A|B\+|B-|B|C\+|C-|C|D\+|D-|D|F|NR)"
RATING_RES = [
    re.compile(r"\bBBB\s+[Rr]ating\s*(?:is|of|:)?\s*" + GRADE + r"(?![\w+-])"),
    re.compile(r"(?<![\w+-])" + GRADE + r"\s+(?:BBB\s+)?[Rr]at(?:ing|ed)\b"),
]
NOT_RATED_RE = re.compile(r"\bnot\s+rated\b", re.I)
COMPLAINTS_RE = re.compile(r"\b(\d[\d,]*)\s+(?:customer\s+)?complaints?\b", re.I)
NO_COMPLAINTS_RE = re.compile(r"\bno\s+(?:customer\s+)?complaints\b", re.I)
STARTED_RE = re.compile(
    r"\b(?:business\s+started|in\s+business\s+since|started\s+in|established|founded)\s*:?\s*"
    r"(?:in\s+)?(?:\d{1,2}/\d{1,2}/)?((?:19|20)\d{2})\b",
    re.I,
)

LOW_GRADES = {"C+", "C", "C-", "D+", "D", "D-", "F"}
MANY_COMPLAINTS = 3  # this many complaints or more is worth mentioning on a call


def query(company) -> str:
    """'site:bbb.org/us "Smile Dental" Austin TX'"""
    from .ppp import us_location

    state, _ = us_location(company)
    place = " ".join(p for p in (company.city, state) if p)
    name = " ".join((company.name or "").replace('"', " ").split())
    return " ".join(p for p in ("site:bbb.org/us", f'"{name}"', place) if p)


def _title_name(title: str) -> str:
    """'Smile Dental | Better Business Bureau® Profile' -> 'Smile Dental'"""
    return TITLE_SPLIT_RE.split(title or "", maxsplit=1)[0].strip()


def _slug_name(slug: str) -> str:
    """'smile-dental-0825-1000130628' -> 'smile dental'"""
    return re.sub(r"-\d{4}-\d+$", "", slug).replace("-", " ")


def _slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _rating(text: str) -> str | None:
    for pattern in RATING_RES:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return "NR" if NOT_RATED_RE.search(text) else None


def _complaints(text: str) -> int | None:
    if NO_COMPLAINTS_RE.search(text):
        return 0
    m = COMPLAINTS_RE.search(text)
    return int(m.group(1).replace(",", "")) if m else None


def _accredited(text: str) -> tuple[bool | None, str | None]:
    if NOT_ACCREDITED_RE.search(text):
        return False, None
    m = ACCREDITED_SINCE_RE.search(text)
    if m:
        return True, m.group(1)
    return (True, None) if ACCREDITED_RE.search(text) else (None, None)


def _started(text: str) -> int | None:
    m = STARTED_RE.search(text)
    if not m:
        return None
    year = int(m.group(1))
    return year if 1800 <= year <= datetime.now(timezone.utc).year else None


def _same_business(name: str, slug: str, company) -> bool:
    from .dedupe import same_name
    from .people.linkedin import same_company

    return any(
        candidate and (same_name(candidate, company.name) or same_company(candidate, company))
        for candidate in (name, _slug_name(slug))
    )


def parse_results(results: list[dict], company) -> dict:
    """The BBB profile of `company` among web search results: the facts dict described above,
    or {"found": False} when no result is clearly this business's profile."""
    from .ppp import us_location

    state, _ = us_location(company)
    city = _slug(company.city)
    matches = []
    for result in results or []:
        m = PROFILE_RE.match(str(result.get("url") or ""))
        if not m:
            continue
        r_state, r_city, _category, slug = m.groups()
        if state and r_state.upper() != state:
            continue
        name = _title_name(result.get("title") or "")
        if not _same_business(name, slug, company):
            continue
        matches.append((r_city.lower() == city, m, name, result))
    if not matches:
        return {"found": False}
    # Same city first (a chain has a profile per town); otherwise the search's own order.
    _, m, name, result = sorted(matches, key=lambda t: not t[0])[0]
    r_state, r_city, category, slug = m.groups()
    # Facts from every result about this same profile (the profile, its reviews page...).
    text = " ".join(
        f"{r.get('title') or ''} . {r.get('snippet') or ''}"
        for _, pm, _, r in matches if pm.group(4).lower() == slug.lower()
    )
    accredited, since = _accredited(text)
    return {
        "found": True,
        "url": f"https://www.bbb.org/us/{r_state.lower()}/{r_city.lower()}/profile/{category.lower()}/{slug.lower()}",
        "name": name or None,
        "accredited": accredited,
        "accredited_since": since,
        "rating": _rating(text),
        "complaints": _complaints(text),
        "started": _started(text),
    }


def talking_points(bbb: dict | None) -> list[str]:
    """Plain-English facts worth saying on a call, e.g. ['BBB rating C-', '5 BBB complaints']."""
    if not bbb or not bbb.get("found"):
        return []
    points = []
    if bbb.get("rating") in LOW_GRADES:
        points.append(f"BBB rating {bbb['rating']}")
    complaints = bbb.get("complaints")
    if isinstance(complaints, int) and complaints >= MANY_COMPLAINTS:
        points.append(f"{complaints} BBB complaints")
    if bbb.get("accredited") is True:
        since = f" since {bbb['accredited_since']}" if bbb.get("accredited_since") else ""
        points.append(f"Pays for BBB accreditation{since}")
    return points
