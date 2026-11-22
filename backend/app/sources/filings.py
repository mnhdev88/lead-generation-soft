"""New business filings: businesses registered with a US state recently.

Free, official open data (Socrata APIs; Colorado, Connecticut and Oregon are public domain):

    CO  data.colorado.gov  4ykn-tg5h  Business Entities in Colorado           (entityformdate)
    NY  data.ny.gov        n9v6-gdp6  Active Corporations: Beginning 1800      (initial_dos_filing_date)
    CT  data.ct.gov        n7gp-d28j  Connecticut Business Registry            (date_registration)
    OR  data.oregon.gov    tckn-sxa6  Active Businesses - ALL                  (registry_date)

Why: a business registered last month has no website, no agency and no Google reviews yet. The
best time to sell it a website.

    search("cleaning", "Denver, CO", 200, progress, days=30)
        -> businesses filed in Colorado in the last 30 days with "cleaning" in their name,
           in Denver ("Colorado" alone = the whole state). "all" / "any" = every kind of business.

A filing has no phone, website or reviews: the worker can look each one up on Google Maps
(params.match_google) and merge the two. What a filing does give is kept in CompanyResult.raw
["filing"] (and then Company.filing): the state, filing date, entity type, the registry's id and
the people named in it (registered agent, owner of an assumed business name...).

Left out: businesses formed in another state (they existed before), nonprofits, and holding /
investment / real-estate companies (EXCLUDE_NAME_WORDS: they rarely need a website).
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .. import config
from .base import CompanyResult, ProgressFn, Source, SourceError

log = logging.getLogger("sources.filings")

TIMEOUT = 60
DEFAULT_DAYS = 30
MAX_DAYS = 365
MAX_ROWS = 5000  # one request; Socrata allows up to 50,000
ALL_WORDS = {"all", "any", "*", "everything", "all businesses", "any business"}

# Name words of businesses that rarely buy a website (holding and property companies).
EXCLUDE_NAME_WORDS = re.compile(
    r"\b(?:holding|holdings|propert(?:y|ies)|investments?|investors?|capital|trust|equity|assets|"
    r"acquisitions?|real estate|realty holdings|rentals?|land|lending|fund|funding|portfolio|"
    r"irrevocable|revocable|family limited partnership|flp)\b",
    re.I,
)
# Words that mean a company (a registered-agent service), not a person.
COMPANY_WORDS = re.compile(
    r"\b(?:llc|l\.l\.c|inc|corp|corporation|company|co\.|ltd|services?|agents?|registered|"
    r"incorporat\w*|associates|group|partners|law|attorneys?|pllc|pc|p\.c|cpa|trust|bank)\b",
    re.I,
)
KEEP_UPPER = {"LLC", "PLLC", "LLP", "LP", "PC", "INC", "INC.", "CORP", "CORP.", "DBA", "USA", "CPA", "LTD", "II", "III",
              "IV", "MD", "DDS", "DMD", "HVAC", "AC", "BBQ", "PT"}

STATE_NAMES = {
    "colorado": "CO", "co": "CO", "new york": "NY", "ny": "NY", "connecticut": "CT", "ct": "CT",
    "oregon": "OR", "or": "OR",
}

# Tests set this to an httpx.MockTransport so no real network is used.
mock_transport: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class Registry:
    state: str
    label: str
    url: str


REGISTRIES = {
    "CO": Registry("CO", "Colorado Secretary of State", "https://data.colorado.gov/resource/4ykn-tg5h.json"),
    "NY": Registry("NY", "New York Department of State", "https://data.ny.gov/resource/n9v6-gdp6.json"),
    "CT": Registry("CT", "Connecticut Secretary of the State", "https://data.ct.gov/resource/n7gp-d28j.json"),
    "OR": Registry("OR", "Oregon Secretary of State", "https://data.oregon.gov/resource/tckn-sxa6.json"),
}


# ----- small helpers ------------------------------------------------------------------


def parse_location(location: str) -> tuple[str, str | None]:
    """'Denver, CO' -> ('CO', 'DENVER'); 'Colorado' -> ('CO', None). SourceError otherwise."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if parts and parts[-1].lower() in ("usa", "us", "united states"):
        parts = parts[:-1]
    state = STATE_NAMES.get(parts[-1].lower()) if parts else None
    if not state and parts:  # "Denver CO" without a comma
        words = parts[-1].rsplit(" ", 1)
        if len(words) == 2 and words[1].lower() in STATE_NAMES:
            state = STATE_NAMES[words[1].lower()]
            parts = parts[:-1] + [words[0]]
            parts.append(words[1])
    if not state:
        raise SourceError(
            f"New business filings are available for Colorado, New York, Connecticut and Oregon. "
            f"Type a place like \"Denver, CO\" or just \"Colorado\" (not \"{location}\")."
        )
    city = " ".join(parts[:-1]).strip().upper() if len(parts) > 1 else None
    return state, city or None


def keyword(query: str) -> str | None:
    """'Cleaning' -> 'CLEANING'; 'all' -> None (every kind of business)."""
    text = " ".join((query or "").split())
    return None if text.lower() in ALL_WORDS else text.upper()


def _quote(text: str) -> str:
    """A SoQL string literal: single quotes doubled."""
    return "'" + text.replace("'", "''") + "'"


def _like(column: str, word: str) -> str:
    # % and _ in the keyword would be wildcards: drop them (business types never need them).
    clean = re.sub(r"[%_]", " ", word).strip()
    return f"upper({column}) like {_quote('%' + clean + '%')}"


def nice_name(name: str) -> str:
    """'JOSIAH'S HANDYMAN & CONSTRUCTION LLC' -> "Josiah's Handyman & Construction LLC".
    Names already in mixed case stay as they are."""
    name = " ".join((name or "").split())
    if not name.isupper():
        return name
    words = []
    for w in name.split(" "):
        bare = w.strip(",.")
        words.append(w if bare in KEEP_UPPER else w.capitalize())
    return " ".join(words)


def _person(*parts) -> str | None:
    text = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    if not text or COMPANY_WORDS.search(text):
        return None
    return nice_name(text.upper())


def _day(value) -> str | None:
    """'2026-11-21T00:00:00.000' -> '2026-11-21'"""
    return value[:10] if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value) else None


def _address(*parts) -> str | None:
    return ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip()) or None


def _naics(text) -> str | None:
    """'Snack and Nonalcoholic Beverage Bars (722515)' -> 'Snack and Nonalcoholic Beverage Bars'"""
    return re.sub(r"\s*\(\d+\)\s*$", "", text).strip() or None if isinstance(text, str) else None


def _excluded(name: str) -> bool:
    return bool(EXCLUDE_NAME_WORDS.search(name or ""))


# ----- one function per state: SoQL query + row -> CompanyResult --------------------------


def _co(since: str, city: str | None, word: str | None, limit: int):
    where = [f"entityformdate >= {_quote(since)}", "entitytype like 'D%'", "entitytype != 'DNC'",
             "entitystatus = 'Good Standing'"]
    if city:
        where.append(f"upper(principalcity) = {_quote(city)}")
    if word:
        where.append(_like("entityname", word))
    return {"$where": " AND ".join(where), "$order": "entityformdate DESC", "$limit": limit}


def _co_result(row: dict) -> CompanyResult | None:
    name = row.get("entityname")
    if not name or not row.get("entityid"):
        return None
    people = []
    agent = _person(row.get("agentfirstname"), row.get("agentmiddlename"), row.get("agentlastname"))
    if agent:
        people.append({"name": agent, "role": "Registered agent"})
    city = row.get("principalcity")
    return CompanyResult(
        name=nice_name(name), source_ref=f"CO-{row['entityid']}",
        address=_address(row.get("principaladdress1"), row.get("principaladdress2"), city,
                         f"{row.get('principalstate') or 'CO'} {row.get('principalzipcode') or ''}".strip()),
        city=city.title() if city and city.isupper() else city, country="US",
        raw={"filing": {"state": "CO", "filed": _day(row.get("entityformdate")), "entity_type": row.get("entitytype"),
                        "registry_id": row["entityid"], "people": people,
                        "zip": row.get("principalzipcode"), "region": row.get("principalstate") or "CO"}},
    )


def _ny(since: str, city: str | None, word: str | None, limit: int):
    where = [f"initial_dos_filing_date >= {_quote(since)}", "entity_type like 'DOMESTIC%'",
             "entity_type not like '%NOT-FOR-PROFIT%'"]
    if city:
        where.append(f"(upper(location_city) = {_quote(city)} OR upper(dos_process_city) = {_quote(city)})")
    if word:
        where.append(_like("current_entity_name", word))
    return {"$where": " AND ".join(where), "$order": "initial_dos_filing_date DESC", "$limit": limit}


def _ny_result(row: dict) -> CompanyResult | None:
    name = row.get("current_entity_name")
    if not name or not row.get("dos_id"):
        return None
    # The business's own location when given, else where the state sends its mail (often the owner).
    prefix = "location" if row.get("location_address_1") else "dos_process"
    city = row.get(f"{prefix}_city")
    people = []
    chairman = _person(row.get("chairman_name"))
    if chairman:
        people.append({"name": chairman, "role": "Chairman"})
    contact = _person(row.get("dos_process_name"))
    if contact and contact != chairman:
        people.append({"name": contact, "role": "Contact for state mail"})
    return CompanyResult(
        name=nice_name(name), source_ref=f"NY-{row['dos_id']}",
        address=_address(row.get(f"{prefix}_address_1"), row.get(f"{prefix}_address_2"), city,
                         f"{row.get(f'{prefix}_state') or 'NY'} {row.get(f'{prefix}_zip') or ''}".strip()),
        city=city.title() if city and city.isupper() else city, country="US",
        raw={"filing": {"state": "NY", "filed": _day(row.get("initial_dos_filing_date")),
                        "entity_type": row.get("entity_type"), "registry_id": row["dos_id"], "people": people,
                        "county": row.get("county"), "zip": row.get(f"{prefix}_zip"),
                        "region": row.get(f"{prefix}_state") or "NY"}},
    )


def _ct(since: str, city: str | None, word: str | None, limit: int):
    where = [f"date_registration >= {_quote(since)}", "status = 'Active'", "business_type != 'Non-Stock'",
             "upper(state_or_territory_formation) in ('CONNECTICUT', 'CT')"]
    if city:
        where.append(f"upper(billingcity) = {_quote(city)}")
    if word:
        where.append(f"({_like('name', word)} OR {_like('naics_code', word)})")
    return {"$where": " AND ".join(where), "$order": "date_registration DESC", "$limit": limit}


def _ct_result(row: dict) -> CompanyResult | None:
    name = row.get("name")
    if not name or not row.get("id"):
        return None
    city = row.get("billingcity")
    email = row.get("business_email_address")
    return CompanyResult(
        name=nice_name(name), source_ref=f"CT-{row['id']}", category=_naics(row.get("naics_code")),
        email=email if isinstance(email, str) and "@" in email else None,
        address=_address(row.get("billingstreet"), row.get("billing_unit"), city,
                         f"{row.get('billingstate') or ''} {row.get('billingpostalcode') or ''}".strip()),
        city=city, country="US",
        raw={"filing": {"state": "CT", "filed": _day(row.get("date_registration")),
                        "entity_type": row.get("business_type"), "registry_id": row.get("accountnumber") or row["id"],
                        "people": [], "industry": _naics(row.get("naics_code")),
                        "zip": row.get("billingpostalcode"), "region": row.get("billingstate") or "CT"}},
    )


def _or(since: str, city: str | None, word: str | None, limit: int):
    """Oregon has one row per name / address of a business: this first query finds the
    businesses by their principal place of business; _or_rows then reads all their rows."""
    where = [f"registry_date >= {_quote(since)}", "associated_name_type = 'PRINCIPAL PLACE OF BUSINESS'",
             "entity_type not like 'FOREIGN%'", "entity_type not like '%NONPROFIT%'"]
    if city:
        where.append(f"upper(city) = {_quote(city)}")
    if word:
        where.append(_like("business_name", word))
    return {"$where": " AND ".join(where), "$order": "registry_date DESC", "$limit": limit}


OR_ROLES = {"REGISTERED AGENT": "Registered agent", "AUTHORIZED REPRESENTATIVE": "Authorized representative"}


def _or_result(rows: list[dict]) -> CompanyResult | None:
    """All rows of one Oregon business -> one result."""
    place = next((r for r in rows if r.get("associated_name_type") == "PRINCIPAL PLACE OF BUSINESS"), None)
    if place is None or not place.get("business_name") or not place.get("registry_number"):
        return None
    assumed = (place.get("entity_type") or "").upper() == "ASSUMED BUSINESS NAME"
    people = []
    for r in rows:
        role = OR_ROLES.get(r.get("associated_name_type") or "")
        person = _person(r.get("first_name"), r.get("middle_name"), r.get("last_name"))
        if role and person and all(p["name"] != person for p in people):
            # The registrant of an assumed business name (a "DBA") is its owner.
            people.append({"name": person, "role": "Owner" if assumed and role == "Authorized representative" else role})
    city = place.get("city")
    details = place.get("business_details")
    return CompanyResult(
        name=nice_name(place["business_name"]), source_ref=f"OR-{place['registry_number']}",
        address=_address(place.get("address"), place.get("address_continued"), city,
                         f"{place.get('state') or 'OR'} {place.get('zip') or ''}".strip()),
        city=city.title() if city and city.isupper() else city, country="US",
        raw={"filing": {"state": "OR", "filed": _day(place.get("registry_date")), "entity_type": place.get("entity_type"),
                        "registry_id": place["registry_number"], "people": people,
                        "url": details.get("url") if isinstance(details, dict) else None,
                        "zip": place.get("zip"), "region": place.get("state") or "OR"}},
    )


QUERIES = {"CO": (_co, _co_result), "NY": (_ny, _ny_result), "CT": (_ct, _ct_result), "OR": (_or, None)}


class NewBusinessFilingsSource(Source):
    name = "filings"
    label = "New business filings"
    description = ("Businesses registered with the state recently (Colorado, New York, Connecticut, Oregon). "
                   "Free official data; no phone or website until matched on Google Maps.")

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def search(self, query: str, location: str, limit: int, progress: ProgressFn,
               days: int = DEFAULT_DAYS) -> list[CompanyResult]:
        state, city = parse_location(location)
        word = keyword(query)
        days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
        registry = REGISTRIES[state]
        build, to_result = QUERIES[state]
        # Ask for more rows than needed: holding companies etc. are dropped afterwards.
        params = build(since, city, word, min(MAX_ROWS, max(limit * 2, 50)))
        where = f"{city.title()}, {state}" if city else registry.label.split(" ")[0]
        progress(f"Reading new business filings ({registry.label}) for {where}, last {days} days...")
        headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
        if config.SOCRATA_APP_TOKEN:
            headers["X-App-Token"] = config.SOCRATA_APP_TOKEN
        with httpx.Client(transport=self.transport or mock_transport, timeout=TIMEOUT, headers=headers) as client:
            rows = self._get(client, registry, params)
            if state == "OR":
                results = self._oregon(client, registry, rows)
            else:
                results = [r for r in (to_result(row) for row in rows) if r]
        kept, skipped = [], 0
        for r in results:
            if _excluded(r.name):
                skipped += 1
            elif len(kept) < limit:
                if word and not r.category:
                    r.category = " ".join(query.split()).title()  # its name has the word searched for
                kept.append(r)
        if skipped:
            progress(f"Left out {skipped} holding / property / investment companies.")
        return kept

    def _get(self, client: httpx.Client, registry: Registry, params: dict) -> list[dict]:
        try:
            response = client.get(registry.url, params=params)
        except httpx.HTTPError as e:
            raise SourceError(f"Could not reach the {registry.label} data ({type(e).__name__}). "
                              "Check the internet connection and try again.") from e
        if response.status_code == 429:
            raise SourceError(f"The {registry.label} data says too many requests. Wait a minute and try again "
                              "(a free SOCRATA_APP_TOKEN in backend/.env raises the limit).")
        if response.status_code != 200:
            log.warning("%s: HTTP %s %s", registry.url, response.status_code, response.text[:300])
            raise SourceError(f"The {registry.label} data is not answering properly (HTTP {response.status_code}). "
                              "Try again later.")
        try:
            data = response.json()
        except ValueError as e:
            raise SourceError(f"The {registry.label} data sent an unreadable answer.") from e
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def _oregon(self, client: httpx.Client, registry: Registry, places: list[dict]) -> list[CompanyResult]:
        numbers = list(dict.fromkeys(str(p["registry_number"]) for p in places if p.get("registry_number")))
        by_number: dict[str, list[dict]] = {n: [] for n in numbers}
        for p in places:
            by_number.get(str(p.get("registry_number")), []).append(p)
        for i in range(0, len(numbers), 200):  # the other rows (agent, representative) of these businesses
            chunk = numbers[i:i + 200]
            rows = self._get(client, registry, {
                "$where": f"registry_number in ({', '.join(_quote(n) for n in chunk)}) "
                          "AND associated_name_type != 'PRINCIPAL PLACE OF BUSINESS'",
                "$limit": MAX_ROWS,
            })
            for row in rows:
                by_number.get(str(row.get("registry_number")), []).append(row)
        return [r for r in (_or_result(by_number[n]) for n in numbers) if r]
