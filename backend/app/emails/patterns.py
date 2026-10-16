"""Company email patterns: how a company builds its staff addresses.

If we know john.smith@acme.com belongs to John Smith, Acme probably uses "{first}.{last}",
so Jane Doe is probably jane.doe@acme.com.

Pattern placeholders (the same syntax Hunter.io uses):
    {first} = first name      {f} = first letter of the first name
    {last}  = last name       {l} = first letter of the last name
e.g. "{first}.{last}" -> john.smith, "{f}{last}" -> jsmith, "{first}" -> john

- detect_pattern(domain, known)  -> which pattern explains the known (first, last, email) triples
- learn_pattern(domain, known)   -> same, and saves it on the domain (domains table)
- candidate_emails(first, last, domain, pattern) -> likely addresses, best first
"""
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db import SessionLocal
from ..models import DomainInfo
from .syntax import normalize_email, split_email

# Every supported pattern, most common first (rough global popularity, used for ranking
# guesses and for breaking ties).
PATTERNS = [
    "{first}",
    "{first}.{last}",
    "{f}{last}",
    "{first}{last}",
    "{first}{l}",
    "{last}",
    "{f}.{last}",
    "{first}_{last}",
    "{last}.{first}",
    "{first}.{l}",
    "{first}-{last}",
    "{last}{f}",
    "{last}{first}",
    "{f}{l}",
    "{f}",
    "{l}",
]
MAX_CANDIDATES = 8

_PATTERN_RE = re.compile(r"^(?:\{(?:first|last|f|l)\}|[._-])+$")
# Words that are not part of a person's name.
_TITLES = {"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "professor", "sir", "dame", "madam",
           "eng", "engr", "rev", "hon", "capt", "col", "sheikh", "syed", "sayed", "hafiz", "haji"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "dds", "dmd", "mds", "bds", "fcps", "mbbs",
             "rn", "esq", "cpa", "mba", "msc", "bsc", "frcs", "facs"}
# Letters that don't split into "letter + accent" in Unicode.
_SPECIAL_LETTERS = str.maketrans({"ß": "ss", "ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d",
                                  "ð": "d", "þ": "th", "ı": "i"})


def is_valid_pattern(pattern: str | None) -> bool:
    return bool(pattern) and bool(_PATTERN_RE.match(pattern)) and "{" in pattern


def _name_tokens(text: str | None) -> list[list[str]]:
    """Split a name into words; each word into its hyphen parts. Accents are stripped.
    "Dr. Anne-Marie  O'Brien" -> [['dr'], ['anne', 'marie'], ['obrien']]"""
    if not text:
        return []
    text = unicodedata.normalize("NFKD", text.lower().translate(_SPECIAL_LETTERS))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("'", "").replace("’", "")  # O'Brien -> obrien
    tokens = []
    for word in re.split(r"[\s,.;/()]+", text):
        parts = [p for p in re.split(r"[^a-z]+", word) if p]
        if parts:
            tokens.append(parts)
    return tokens


def split_name(first: str | None, last: str | None) -> tuple[list[str], list[str]]:
    """Clean a person's name. Returns (first-name parts, last-name parts).
    'Dr. Anne-Marie', 'van der Berg' -> (['anne', 'marie'], ['van', 'der', 'berg'])
    'John Smith', None               -> (['john'], ['smith'])   (full name given as first name)
    A second given name ('John Paul') is dropped; a hyphenated one ('Anne-Marie') is kept."""
    first_tokens = [t for t in _name_tokens(first) if "".join(t) not in _TITLES]
    last_tokens = [t for t in _name_tokens(last) if "".join(t) not in _SUFFIXES]
    if not last_tokens and len(first_tokens) > 1:
        first_tokens, last_tokens = first_tokens[:1], [t for t in first_tokens[1:] if "".join(t) not in _SUFFIXES]
    first_parts = first_tokens[0] if first_tokens else []
    last_parts = [p for t in last_tokens for p in t]
    return first_parts, last_parts


def _variants(parts: list[str], *, with_last_part: bool = True) -> list[str]:
    """['smith', 'jones'] -> ['smithjones', 'smith-jones', 'smith', 'jones'] (main form first)."""
    if not parts:
        return []
    variants = ["".join(parts)]
    if len(parts) > 1:
        variants += ["-".join(parts), parts[0]] + ([parts[-1]] if with_last_part else [])
    return list(dict.fromkeys(variants))


def render(pattern: str, first: str, last: str | None) -> str | None:
    """Fill a pattern with an already-cleaned name. None when the pattern needs a last name."""
    if ("{last}" in pattern or "{l}" in pattern) and not last:
        return None
    out = (pattern.replace("{first}", first).replace("{f}", first[0])
           .replace("{last}", last or "").replace("{l}", (last or " ")[0]))
    return out.strip("._-") or None


def _matching_patterns(first: str | None, last: str | None, email: str, domain: str) -> set[str]:
    """Which patterns produce this person's address."""
    email = normalize_email(email)
    if not email:
        return set()
    local, email_domain = split_email(email)
    if email_domain != domain and not email_domain.endswith("." + domain):
        return set()
    first_parts, last_parts = split_name(first, last)
    firsts = _variants(first_parts, with_last_part=False)
    if not firsts:
        return set()
    matches = set()
    for pattern in PATTERNS:
        lasts = _variants(last_parts) if ("{last}" in pattern or "{l}" in pattern) else [None]
        if any(render(pattern, f, lv) == local for f in firsts for lv in lasts):
            matches.add(pattern)
    return matches


def _clean_domain(domain: str) -> str:
    domain = domain.strip().lower().rstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def detect_pattern(domain: str, known: list[tuple[str, str, str]]) -> tuple[str | None, int]:
    """Find the pattern that explains most of the known (first, last, email) examples.
    Returns (pattern, confidence 0-100), or (None, 0) when nothing matches.
    One matching person -> 60; two -> 85; three -> 90; four or more -> 95.
    Each person who matches a *different* pattern lowers it by 15 (minimum 30)."""
    domain = _clean_domain(domain)
    votes: dict[str, int] = {}
    people_matched = 0
    for first, last, email in known:
        matches = _matching_patterns(first, last, email, domain)
        if not matches:
            continue
        people_matched += 1
        for pattern in matches:
            votes[pattern] = votes.get(pattern, 0) + 1
    if not votes:
        return None, 0
    # Most votes wins; ties go to the more common pattern.
    best = max(votes, key=lambda p: (votes[p], -PATTERNS.index(p)))
    agree = votes[best]
    confidence = {1: 60, 2: 85, 3: 90}.get(agree, 95)
    disagree = people_matched - agree
    confidence = max(30, confidence - 15 * disagree)
    return best, confidence


def learn_pattern(domain: str, known: list[tuple[str, str, str]], source: str = "website") -> tuple[str | None, int]:
    """detect_pattern() and save the result on the domain when it beats what is stored
    (higher confidence, or the stored one was only a guess).
    Returns the domain's pattern after the update: (pattern, confidence), or (None, 0)."""
    domain = _clean_domain(domain)
    pattern, confidence = detect_pattern(domain, known)
    for attempt in range(2):  # retry once if another thread created the row at the same time
        try:
            with SessionLocal() as session:
                info = session.scalar(select(DomainInfo).where(DomainInfo.domain == domain))
                if pattern is None:
                    if info is None or not info.email_pattern:
                        return None, 0
                    return info.email_pattern, info.pattern_confidence or 0
                if info is None:
                    info = DomainInfo(domain=domain)
                    session.add(info)
                stored, stored_conf = info.email_pattern, info.pattern_confidence or 0
                if stored == pattern:
                    if confidence > stored_conf:
                        info.pattern_confidence = confidence
                        info.pattern_source = source
                elif not stored or confidence > stored_conf or info.pattern_source == "guess":
                    info.email_pattern, info.pattern_confidence, info.pattern_source = pattern, confidence, source
                session.commit()
                return info.email_pattern, info.pattern_confidence or 0
        except IntegrityError:
            if attempt:
                raise
    return pattern, confidence  # not reached


def pattern_email(first: str, last: str | None, domain: str, pattern: str | None) -> str | None:
    """The address `pattern` gives for this person, or None when it can't be used
    (no/invalid pattern, or it needs a last name the person doesn't have)."""
    domain = _clean_domain(domain)
    first_parts, last_parts = split_name(first, last)
    if not is_valid_pattern(pattern) or not first_parts or not domain:
        return None
    local = render(pattern, "".join(first_parts), "".join(last_parts) or None)
    if not local or len(local) < 2:
        return None
    return normalize_email(f"{local}@{domain}")


def candidate_emails(first: str, last: str | None, domain: str, pattern: str | None = None) -> list[str]:
    """Likely addresses for a person, best first (max 8). `pattern` is the domain's known
    pattern (e.g. from learn_pattern or Hunter) and is tried first."""
    domain = _clean_domain(domain)
    first_parts, last_parts = split_name(first, last)
    if not first_parts or not domain:
        return []
    clean_first = "".join(first_parts)
    last_name = "".join(last_parts) or None
    order = ([pattern] if is_valid_pattern(pattern) else []) + PATTERNS
    out: list[str] = []
    for p in order:
        local = render(p, clean_first, last_name)
        if not local or len(local) < 2:  # skip one-letter guesses like "j@"
            continue
        email = normalize_email(f"{local}@{domain}")
        if email and email not in out:
            out.append(email)
        if len(out) >= MAX_CANDIDATES:
            break
    return out
