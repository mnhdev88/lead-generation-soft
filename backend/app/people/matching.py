"""Is this the same person as one already saved? One rule for every place that saves people
(website crawl, Hunter.io, web search, Apollo) and for merging duplicates (app/dedupe.py).

In order:
  1. the same LinkedIn profile
  2. the same email address
  3. the same first and last name, ignoring titles ("Dr."), middle names and initials
     ("John A. Smith"), suffixes ("Jr", "MD", "DDS"), accents and punctuation.
Two people with different LinkedIn profiles are never the same person.
"""
import re
import unicodedata

# Words before a name that are not part of it.
NAME_PREFIXES = {"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "eng", "engr", "sir", "rev", "hon"}
# Words after a name: generation and degrees/credentials.
NAME_SUFFIXES = {
    "jr", "sr", "ii", "iii", "iv", "md", "do", "dds", "dmd", "phd", "mba", "cpa", "esq", "rn", "np",
    "pa", "dc", "od", "dvm", "pharmd", "pe", "ra", "lcsw", "lpc", "pt", "dpt", "facs", "faap",
}


def _words(name: str | None) -> list[str]:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\(.*?\)", " ", text)  # "John (Johnny) Smith"
    return [w for w in re.split(r"[^a-z0-9']+", text) if w.strip("'")]


def name_key(name: str | None) -> tuple[str, str] | None:
    """('john', 'smith') for 'Dr. John A. Smith, MD'. None when there's no first AND last name."""
    words = _words(name)
    while words and words[0].rstrip(".") in NAME_PREFIXES:
        words = words[1:]
    while words and words[-1] in NAME_SUFFIXES:
        words = words[:-1]
    if len(words) < 2:
        return None
    return words[0], words[-1]


def same_name(a: str | None, b: str | None) -> bool:
    """The same first and last name (see name_key). Single names never match."""
    ka = name_key(a)
    return ka is not None and ka == name_key(b)


def same_linkedin(a: str | None, b: str | None) -> bool:
    """Two links to the same LinkedIn profile (www./pk., http/https, trailing / ignored)."""
    from . import linkedin  # here: linkedin imports app.dedupe, which imports this module

    def norm(url: str) -> str:
        return linkedin.profile_url(url.replace("http://", "https://", 1)) or url.strip().lower().rstrip("/")

    return bool(a and b) and norm(a) == norm(b)


def find_same_person(contacts, name: str | None, email: str | None, linkedin_url: str | None = None,
                     exclude=None):
    """The contact among `contacts` that is this person, or None. `exclude`: a contact to skip
    (the person itself, when looking for its duplicates)."""
    others = [c for c in contacts if c is not exclude]
    if linkedin_url:
        for c in others:
            if same_linkedin(c.linkedin_url, linkedin_url):
                return c
    email = (email or "").strip().lower()
    if email:
        for c in others:
            if (c.email or "").strip().lower() == email:
                return c
    full = " ".join((name or "").split()).lower()
    for c in others:
        if linkedin_url and c.linkedin_url and not same_linkedin(c.linkedin_url, linkedin_url):
            continue  # two different LinkedIn profiles: two people who share a name
        if full and " ".join((c.full_name or "").split()).lower() == full:
            return c
        if same_name(c.full_name, name):
            return c
    return None
