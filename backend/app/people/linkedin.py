"""Read web search results about LinkedIn and pick out a business's owner / decision-maker.

We never open linkedin.com. A web search (app/providers/websearch.py) is asked for e.g.

    site:linkedin.com/in ("owner" OR "founder" OR ...) "Smile Dental" Austin

and each result's title and snippet are read here:

    title   "Jane Doe - Owner - Smile Dental | LinkedIn"
    snippet "Owner at Smile Dental · Experience: Smile Dental · Location: Austin, Texas, United States"
    -> FoundPerson("Jane Doe", "Owner", "https://www.linkedin.com/in/janedoe", ...)

Precision first: a wrong person is worse than no person. A result is accepted ONLY when
  1. its link is a LinkedIn person page (linkedin.com/in/<name>), not a post, article or company;
  2. it says the person works at THIS company now: "Owner at Smile Dental" - not "Smile Dental
     Studio", not "Former owner at Smile Dental", not "Previously: Smile Dental";
  3. their job title there is a decision-maker's (app/people/seniority.py: owner, CEO, director...);
  4. it is not a student or recruiter, and it doesn't clearly say they live in another city;
  5. the name looks like a person's name.
"""
import html
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

from ..dedupe import NAME_NOISE, same_name
from .seniority import classify

MAX_PEOPLE = 3  # people kept per company

# Job titles asked for in the search.
QUERY_TITLES = ("owner", "founder", "president", "CEO", "managing partner", "principal")

# Words at the end of a business name that LinkedIn usually leaves out ("Smile Dental, PLLC").
LEGAL_SUFFIXES = {
    "llc", "l.l.c.", "inc", "inc.", "incorporated", "ltd", "ltd.", "limited", "pvt", "pvt.", "private",
    "corp", "corp.", "corporation", "co", "co.", "company", "pllc", "pc", "p.c.", "plc", "llp", "lp",
}

# Credentials written after a name ("Jane Doe, DDS"): they go to the job title instead.
CREDENTIALS = {
    "dds", "dmd", "md", "do", "phd", "cpa", "esq", "jd", "mba", "rn", "np", "pe", "dvm", "od", "dc",
    "pharmd", "bds", "mds", "mbbs", "fagd", "magd", "cfp", "pmp", "msc", "ms", "ma", "bsc", "ba", "bs",
    "llb", "llm", "facs", "fcps", "frcs", "mph", "mha", "msn", "dnp", "pa", "pac", "lmt", "dpt", "pt",
    "otr", "lcsw", "lpc", "cfa", "acca", "aca", "fca", "cissp", "shrm", "cpc", "ricp", "clu", "chfc",
    "ea", "aia", "leed", "cpo", "realtor", "gri", "crs", "abr",
}
HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "engr", "eng", "sir"}

# Words that mean the person no longer works there.
FORMER_RE = re.compile(r"\b(?:former|formerly|ex|previously|previous|past|retired|until)\b", re.I)
# Not a decision-maker of the business, whatever else the profile says.
STUDENT_RE = re.compile(r"\b(?:student|students|undergrad|undergraduate|graduate student|aspiring)\b", re.I)
RECRUITER_RE = re.compile(
    r"\b(?:recruiter|recruiters|recruiting|recruitment|headhunter|talent acquisition|staffing|hiring for)\b", re.I)
# Words that show a sentence about somebody else ("I work with Jane, owner at ..."), not a job title.
SENTENCE_WORDS = {
    "i", "im", "we", "my", "our", "you", "your", "he", "she", "his", "her", "they", "their", "with",
    "work", "worked", "working", "met", "congratulations", "congrats", "thanks", "thank", "proud",
    "hiring", "join", "joined", "looking", "seeking", "by", "for", "to", "is", "was", "who",
    "about", "meet",
}
ROLE_MAX_WORDS = 8

# Places that are only a country / state / province: saying one of these is not "another city".
REGIONS = {
    # countries
    "united states", "usa", "us", "united states of america", "canada", "pakistan", "india",
    "united kingdom", "uk", "england", "scotland", "wales", "ireland", "australia", "new zealand",
    "united arab emirates", "uae", "saudi arabia", "qatar", "germany", "france", "mexico",
    # US states (+ DC)
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
    # Canadian provinces and territories
    "ontario", "quebec", "british columbia", "alberta", "manitoba", "saskatchewan", "nova scotia",
    "new brunswick", "newfoundland and labrador", "prince edward island", "yukon",
    "northwest territories", "nunavut",
    # Pakistan
    "punjab", "sindh", "khyber pakhtunkhwa", "balochistan", "islamabad capital territory",
    "gilgit baltistan", "azad kashmir",
    # India
    "andhra pradesh", "assam", "bihar", "chhattisgarh", "goa", "gujarat", "haryana", "himachal pradesh",
    "jharkhand", "karnataka", "kerala", "madhya pradesh", "maharashtra", "odisha", "rajasthan",
    "tamil nadu", "telangana", "uttar pradesh", "uttarakhand", "west bengal", "delhi",
    "jammu and kashmir",
}
# The country at the end of a location ("Austin, Texas, United States"), to spot location text.
LOCATION_COUNTRIES = (
    "united states", "canada", "pakistan", "india", "united kingdom", "australia",
    "united arab emirates", "saudi arabia",
)
PLACE_NOISE = {"greater", "area", "metropolitan", "metro", "region", "metroplex", "bay", "county", "district"}
PLACE_SHORT = {"saint": "st", "mount": "mt", "fort": "ft"}

# https://www.linkedin.com/in/jane-doe-123, https://pk.linkedin.com/in/jane/, ...?trk=x
_PROFILE_URL_RE = re.compile(
    r"^https://(?:www\.|[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9%_\-]{2,100})/?(?:[a-z]{2}/?)?(?:[?#].*)?$")
_COMPANY_URL_RE = re.compile(
    r"^https://(?:www\.|[a-z]{2,3}\.)?linkedin\.com/company/([A-Za-z0-9%_.\-]{2,100})/?(?:[?#].*)?$")
_LINKEDIN_SUFFIX_RE = re.compile(r"\s*[|\-–—·:]\s*linkedin\s*$", re.I)
_TITLE_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:·|•|\||•|\.\s+|\n)\s*")
_AT_RE = re.compile(r"^(?P<role>.+?)\s+(?:at|@)\s+(?P<emp>.+)$", re.I)
_LABEL_RE = re.compile(r"^(experience|location|previously|past|education|current|present)\s*:\s*(.*)$", re.I)


@dataclass
class FoundPerson:
    full_name: str
    title: str
    linkedin_url: str  # https://www.linkedin.com/in/<name>
    source_url: str  # the search result's own link


# ----- small text helpers -------------------------------------------------------------


def _plain(text) -> str:
    return " ".join(html.unescape(str(text or "")).split())


def _words(text: str | None) -> list[str]:
    """'Smile Dental, PLLC' -> ['smile', 'dental'] (accents, punctuation and legal words dropped)."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = t.replace("&", " and ").replace("'", "").replace("’", "")
    return [w for w in re.split(r"[^a-z0-9]+", t) if w and w not in NAME_NOISE]


def _main_name(name: str | None) -> str:
    """The business name without a branch part or legal words:
    'Smile Dental - South Austin' -> 'Smile Dental'; 'Acme Plumbing, LLC' -> 'Acme Plumbing'."""
    name = _plain(name)
    main = re.split(r"\s+[-–—|]\s+|\s*\(", name)[0].strip()
    if len(_words(main)) >= 2 or len(_words(main)) == len(_words(name)):
        name = main
    words = name.replace(",", " ").split()
    while len(words) > 1 and words[-1].lower() in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words).strip(" ,")


def _contains(haystack: list[str], needle: list[str]) -> bool:
    """True when the words `needle` appear one after another in `haystack`."""
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def same_company(employer: str | None, company) -> bool:
    """Strict: does `employer` (as written on LinkedIn) name this company?
    The same words (ignoring case, punctuation and LLC/Inc), possibly written together
    ("SmileDental"), possibly with the company's city added or left out.
    "Smile Dental Studio" is NOT "Smile Dental" (a different business)."""
    emp = _words(_clean_employer(employer))
    if not emp:
        return False
    city = _words(getattr(company, "city", None))
    for name in {getattr(company, "name", None) or "", _main_name(getattr(company, "name", None))}:
        own = _words(name)
        if not own:
            continue
        if emp == own or "".join(emp) == "".join(own):
            return True
        if city:  # "Smile Dental Austin" vs "Smile Dental" (in Austin), either way round
            e2 = _strip_words(emp, city)
            o2 = _strip_words(own, city)
            if len(e2) >= 2 and len(o2) >= 2 and (e2 == o2 or "".join(e2) == "".join(o2)):
                return True
    return False


def _strip_words(words: list[str], remove: list[str]) -> list[str]:
    n = len(remove)
    for i in range(len(words) - n + 1):
        if words[i:i + n] == remove:
            return words[:i] + words[i + n:]
    return words


def _clean_employer(text: str | None) -> str:
    """Trailing dots/commas and '...' of a cut-off snippet are removed (the cut name then
    simply fails to match)."""
    text = _plain(text)
    if re.search(r"(?:\.\.\.|…)\s*$", text):
        return ""  # cut off ("Smile Dental Stu..."): we can't tell which business it is
    return text.strip(" .,;:")


# ----- search queries -----------------------------------------------------------------


def _quoted(text: str) -> str:
    return '"' + text.replace('"', "") + '"'


def people_query(company) -> str:
    """The web search that finds the owner's LinkedIn profile."""
    titles = " OR ".join(_quoted(t) for t in QUERY_TITLES)
    query = f"site:linkedin.com/in ({titles}) {_quoted(_main_name(company.name))}"
    city = _plain(getattr(company, "city", None))
    return f"{query} {city}" if city else query


def company_page_query(company) -> str:
    """The web search that finds the company's own LinkedIn page."""
    query = f"site:linkedin.com/company {_quoted(_main_name(company.name))}"
    city = _plain(getattr(company, "city", None))
    return f"{query} {city}" if city else query


# ----- links --------------------------------------------------------------------------


def profile_url(url: str | None) -> str | None:
    """'https://pk.linkedin.com/in/Jane-Doe/?trk=x' -> 'https://www.linkedin.com/in/jane-doe'.
    None for anything that isn't a person's profile (posts, articles, company pages...)."""
    m = _PROFILE_URL_RE.match((url or "").strip())
    if not m:
        return None
    slug = unquote(m.group(1)).lower()
    if not re.search(r"[a-z]", slug):
        return None
    return f"https://www.linkedin.com/in/{m.group(1).lower()}"


def company_url(url: str | None) -> str | None:
    m = _COMPANY_URL_RE.match((url or "").strip())
    return f"https://www.linkedin.com/company/{m.group(1).lower()}" if m else None


# ----- names --------------------------------------------------------------------------


def _clean_name(raw: str) -> tuple[str | None, list[str]]:
    """'Dr. Jane Doe, DDS 🦷' -> ('Jane Doe', ['DDS']). (None, []) when it isn't a person's name."""
    text = _plain(raw)
    # Emojis and symbols out; letters, spaces and - ' . , ( ) stay.
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] in "LM" or ch in " -'’.,()")
    text = re.sub(r"\([^)]*\)", " ", text)  # "Robert (Bob) Smith"
    credentials: list[str] = []
    if "," in text:
        text, rest = text.split(",", 1)
        credentials = [c for c in re.split(r"[\s,]+", rest) if c.strip(" .")]
    words = text.replace("(", " ").replace(")", " ").split()
    while len(words) > 1 and words[0].lower().strip(".") in HONORIFICS:
        words = words[1:]
    # "Jane Doe DDS": a credential written in capitals at the end.
    while len(words) > 2 and words[-1].replace(".", "").isupper() and words[-1].replace(".", "").lower() in CREDENTIALS:
        credentials.insert(0, words.pop())
    words = [w.strip(".") if len(w.strip(".")) > 1 else w for w in words]
    words = [w for w in words if w.strip(".-'’")]
    if not (2 <= len(words) <= 5):
        return None, []
    if any(not any(ch.isalpha() for ch in w) for w in words):
        return None, []
    lowered = {w.lower() for w in words}
    if lowered & {"linkedin", "profile", "profiles", "jobs", "hiring", "page"}:
        return None, []
    name = " ".join(words)
    if len(name) > 60:
        return None, []
    if name.isupper() or name.islower():
        name = " ".join(w.capitalize() for w in words)
    # Credentials only when they really are credentials (else e.g. ", Texas" would be one).
    credentials = [c.strip(".") for c in credentials if c.replace(".", "").lower() in CREDENTIALS]
    return name, credentials


# ----- reading one result -------------------------------------------------------------


def _segments(text: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_SPLIT_RE.split(_plain(text)) if s and s.strip()]


def _looks_like_role(role: str) -> bool:
    words = _words(role.replace("@", " "))
    raw_words = set(re.split(r"[^a-z']+", role.lower().replace("'", "")))
    return 0 < len(words) <= ROLE_MAX_WORDS and not (raw_words & SENTENCE_WORDS)


def _role_at(text: str) -> tuple[str, str] | None:
    """'Owner at Smile Dental' -> ('Owner', 'Smile Dental')."""
    m = _AT_RE.match(text.strip())
    if not m:
        return None
    return m.group("role").strip(" ,;:|"), m.group("emp").strip()


def _location_of(snippet_segments: list[str]) -> str | None:
    """'Location: Austin, Texas, United States' or a segment ending in a country."""
    for seg in snippet_segments:
        m = _LABEL_RE.match(seg)
        if m and m.group(1).lower() == "location":
            return m.group(2).strip() or None
    for seg in snippet_segments:
        low = seg.lower().rstrip(". ")
        if "," in low and any(low.endswith(", " + c) for c in LOCATION_COUNTRIES) and len(seg) <= 80:
            return seg
    return None


def _place_words(text: str) -> list[str]:
    return [PLACE_SHORT.get(w, w) for w in _words(text) if w not in PLACE_NOISE and w != "city"]


def other_city(location: str | None, city: str | None) -> bool:
    """True when `location` clearly names a place other than `city`.
    'Dallas, Texas, United States' vs Austin -> True; 'Greater Austin Area' -> False;
    'Texas, United States' or 'United States' -> False (only a state / country: can't tell)."""
    city_words = _place_words(city or "")
    if not location or not city_words:
        return False
    place = location.split(",")[0]
    place_words = _place_words(place)
    if not place_words:
        return False
    if _contains(place_words, city_words) or _contains(city_words, place_words):
        return False
    if " ".join(_words(place)) in REGIONS or " ".join(place_words) in REGIONS:
        return False
    return True


def _pairs(title_parts: list[str], snippet_segs: list[str]) -> tuple[list[tuple[str, str]], list[str], str | None]:
    """All (job title, employer) statements of one result, plus the employers of
    "Experience:" lines and the bare headline (a 2-part title like "Jane Doe - Owner")."""
    pairs: list[tuple[str, str]] = []
    experience: list[str] = []
    headline = None
    rest = title_parts[1:]
    if rest:
        found = _role_at(rest[0])
        if found:
            pairs.append(found)
        elif len(rest) >= 2:
            pairs.append((rest[0], rest[1]))  # "Name - Title - Company"
        else:
            headline = rest[0]  # "Name - Owner" or "Name - Smile Dental": decided later
    for seg in snippet_segs:
        label = _LABEL_RE.match(seg)
        if label:
            kind, value = label.group(1).lower(), label.group(2).strip()
            if kind in ("experience", "current", "present") and value:
                experience.append(value)
            elif kind in ("previously", "past") and value:
                pairs.append(("Former", value))  # never accepted: marks a past employer
            continue
        found = _role_at(seg)
        if found:
            pairs.append(found)
    return pairs, experience, headline


def _read_result(result: dict, company) -> FoundPerson | None:
    """One search result -> the person, or None when anything is doubtful."""
    url = str(result.get("url") or "").strip()
    linkedin_url = profile_url(url)
    if not linkedin_url:
        return None
    title = _plain(result.get("title"))
    snippet = _plain(result.get("snippet"))
    whole = f"{title} {snippet}"
    if STUDENT_RE.search(whole) or RECRUITER_RE.search(whole):
        return None

    title = _LINKEDIN_SUFFIX_RE.sub("", title).strip()
    parts = [p.strip() for p in _TITLE_SPLIT_RE.split(title) if p.strip()]
    if not parts:
        return None
    name, credentials = _clean_name(parts[0])
    if not name or same_company(name, company) or same_name(name, getattr(company, "name", None)):
        return None  # not a person's name (e.g. the business's own page)

    segs = _segments(snippet)
    pairs, experience, headline = _pairs(parts, segs)

    role = None
    for pair_role, employer in pairs:
        if not same_company(employer, company):
            continue
        if FORMER_RE.search(pair_role) or pair_role == "Former":
            return None  # says they USED to work there
        if role is None and _looks_like_role(pair_role) and not re.search(r"\s(?:at|@)\s", pair_role):
            role = pair_role
    # The headline alone ("Jane Doe - Owner | LinkedIn") counts only with "Experience: <company>"
    # (LinkedIn lists the current job first) and no other employer named anywhere.
    if role is None and headline and not pairs and experience and same_company(experience[0], company):
        if _looks_like_role(headline) and not same_company(headline, company):
            role = headline
    if role is None:
        return None
    # "Former owner of Smile Dental", "ex-Smile Dental" anywhere near the company's name.
    for seg in segs + parts[1:]:
        if FORMER_RE.search(seg) and any(same_company(chunk, company) for chunk in _employer_chunks(seg)):
            return None
    if not classify(role).is_decision_maker:
        return None
    if other_city(_location_of(segs), getattr(company, "city", None)):
        return None

    job_title = role if not credentials else f"{role}, {' '.join(credentials)}"
    return FoundPerson(full_name=name, title=job_title[:200], linkedin_url=linkedin_url, source_url=url[:500])


def _employer_chunks(segment: str) -> list[str]:
    """Possible business names in a sentence: what follows 'at', 'of', 'ex-', ':'..."""
    chunks = []
    for m in re.finditer(r"(?:\b(?:at|of|from|with|formerly|previously|former|ex)\b[-:\s]*|[@:(-]\s*)([^,;()|]+)", segment, re.I):
        chunks.append(m.group(1).strip())
    return chunks


def parse_profiles(results: list[dict], company) -> list[FoundPerson]:
    """The decision-makers of `company` among web search results (at most MAX_PEOPLE)."""
    people: list[FoundPerson] = []
    seen_urls, seen_names = set(), set()
    for result in results or []:
        if not isinstance(result, dict):
            continue
        person = _read_result(result, company)
        if person is None:
            continue
        name_key = person.full_name.lower()
        if person.linkedin_url in seen_urls or name_key in seen_names:
            continue
        seen_urls.add(person.linkedin_url)
        seen_names.add(name_key)
        people.append(person)
        if len(people) >= MAX_PEOPLE:
            break
    return people


def parse_company_page(results: list[dict], company) -> str | None:
    """The company's own LinkedIn page among web search results, or None.
    The result's title must name the company ("Smile Dental | LinkedIn")."""
    for result in results or []:
        if not isinstance(result, dict):
            continue
        url = company_url(str(result.get("url") or ""))
        if not url:
            continue
        title = _LINKEDIN_SUFFIX_RE.sub("", _plain(result.get("title"))).strip()
        title = re.sub(r"\s*:\s*(?:overview|about|about us|posts|jobs|people)\s*$", "", title, flags=re.I)
        title = _TITLE_SPLIT_RE.split(title)[0]
        if same_company(title, company):
            return url
    return None
