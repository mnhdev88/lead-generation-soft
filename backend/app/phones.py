"""Phone numbers: one standard format, a validity check and the line type.

Uses Google's free `phonenumbers` library (the same rules Android uses).

    parse_phone("0300 1234567", country="PK")
      -> PhoneInfo(e164="+923001234567", display="+92 300 1234567", type="mobile")

The country matters: "(21) 3582 1955" is a Karachi landline AND a valid Los Angeles
number. We decide using, in order:
  1. a "+" in front of the number (it carries its own country code),
  2. the company's country ("PK", "Pakistan", "United States"...),
  3. where the business is on the map (rough boxes around our target countries),
  4. otherwise our target countries, but only when exactly one of them fits.
If we still can't tell, the number is kept as it is (type None = "don't know").

type: mobile | landline | landline_or_mobile (US/Canada can't be told apart) |
      toll_free | voip | other | invalid (a real-looking number that can't exist) | None
"""
from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

# Countries we search in most. Used when nothing else tells us the country.
TARGET_REGIONS = ("US", "PK", "IN")  # Canada shares +1 with the US, so "US" covers it

# Country names as the sources write them -> two-letter code.
COUNTRY_NAMES = {
    "united states": "US", "united states of america": "US", "usa": "US", "u.s.a.": "US", "u.s.": "US",
    "america": "US", "canada": "CA", "pakistan": "PK", "india": "IN", "bharat": "IN",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA", "australia": "AU",
    "bangladesh": "BD", "germany": "DE", "france": "FR", "ireland": "IE", "new zealand": "NZ",
}

# Rough (south, north, west, east) boxes. Only a hint: Lahore sits in both the Pakistan and
# the India box, so a point in two boxes with different country codes decides nothing.
REGION_BOXES = {
    "US": [(24.4, 49.4, -125.0, -66.9), (51.0, 71.5, -180.0, -129.9), (18.9, 22.3, -160.3, -154.8)],
    "CA": [(41.6, 83.2, -141.0, -52.6)],
    "PK": [(23.6, 37.1, 60.8, 77.9)],
    "IN": [(6.7, 35.6, 68.1, 97.4)],
}

TYPE_NAMES = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "landline",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "landline_or_mobile",
    PhoneNumberType.TOLL_FREE: "toll_free",
    PhoneNumberType.VOIP: "voip",
}


@dataclass
class PhoneInfo:
    e164: str | None  # "+923001234567" - the same number always looks the same here
    display: str  # "+92 300 1234567", or the original text when we couldn't read it
    type: str | None


def country_code(country: str | None) -> str | None:
    """'Pakistan' / 'pk' / 'PK' -> 'PK'. None when unknown."""
    if not country:
        return None
    text = country.strip()
    if len(text) == 2 and text.upper() in phonenumbers.SUPPORTED_REGIONS:
        return text.upper()
    return COUNTRY_NAMES.get(text.lower())


def regions_at(lat: float | None, lon: float | None) -> list[str]:
    """Target countries whose box contains the point (may be several, or none)."""
    if lat is None or lon is None:
        return []
    return [
        region for region, boxes in REGION_BOXES.items()
        if any(s <= lat <= n and w <= lon <= e for s, n, w, e in boxes)
    ]


def _parse(raw: str, region: str | None) -> phonenumbers.PhoneNumber | None:
    try:
        return phonenumbers.parse(raw, region)
    except NumberParseException:
        return None


def _info(number: phonenumbers.PhoneNumber) -> PhoneInfo:
    return PhoneInfo(
        e164=phonenumbers.format_number(number, PhoneNumberFormat.E164),
        display=phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL),
        type=TYPE_NAMES.get(phonenumbers.number_type(number), "other"),
    )


def parse_phone(
    raw: str | None, country: str | None = None, lat: float | None = None, lon: float | None = None
) -> PhoneInfo | None:
    """Read a phone number. None for an empty value. See the module docstring."""
    raw = (raw or "").strip()
    if not raw:
        return None
    unknown = PhoneInfo(e164=None, display=raw, type=None)

    # 1. "+92 ..." or "0092 ...": the number says which country it is.
    if raw.startswith(("+", "00")):
        number = _parse("+" + raw[2:] if raw.startswith("00") else raw, None)
        if number is None:
            return unknown
        return _info(number) if phonenumbers.is_valid_number(number) else PhoneInfo(None, raw, "invalid")

    # 2. The company's country is known: the number must be valid there.
    region = country_code(country)
    if region:
        number = _parse(raw, region)
        if number is None:
            return unknown
        return _info(number) if phonenumbers.is_valid_number(number) else PhoneInfo(None, raw, "invalid")

    # 3./4. Try the countries the map suggests (or all target countries) and accept the
    # answer only when they agree on one number.
    candidates = regions_at(lat, lon) or list(TARGET_REGIONS)
    found: dict[str, phonenumbers.PhoneNumber] = {}
    for region in candidates:
        number = _parse(raw, region)
        if number is not None and phonenumbers.is_valid_number(number):
            found[phonenumbers.format_number(number, PhoneNumberFormat.E164)] = number
    if len(found) == 1:
        return _info(next(iter(found.values())))
    return unknown


# ----- applying it to saved leads ------------------------------------------------------


def apply_company_phone(company) -> None:
    """Tidy company.phone and fill phone_e164 / phone_type (the caller commits)."""
    info = parse_phone(company.phone, company.country, company.lat, company.lon)
    company.phone = info.display if info else None
    company.phone_e164 = info.e164 if info else None
    company.phone_type = info.type if info else None


def apply_contact_phone(contact, company=None) -> None:
    """Same for a person; their company tells us the country."""
    info = parse_phone(
        contact.phone,
        getattr(company, "country", None), getattr(company, "lat", None), getattr(company, "lon", None),
    )
    contact.phone = info.display if info else None
    contact.phone_type = info.type if info else None


def is_usable(phone: str | None, phone_type: str | None) -> bool:
    """A phone worth calling: present and not proven impossible."""
    return bool(phone) and phone_type != "invalid"


def best_phone(candidates: list[str], country=None, lat=None, lon=None) -> str | None:
    """The first of `candidates` (e.g. phones found on a website) that isn't invalid."""
    for raw in candidates or []:
        info = parse_phone(raw, country, lat, lon)
        if info and info.type != "invalid":
            return raw
    return None


BACKFILL_BATCH = 200


def normalize_saved_phones(session) -> tuple[int, int]:
    """Read the phones of leads saved before this module existed (and those we couldn't read
    yet). Rescores the companies that changed. Returns (companies, people) updated."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from .models import Company, Contact
    from .scoring import rescore

    n_companies = n_contacts = 0
    stmt = (
        select(Company).options(selectinload(Company.contacts))
        .where(Company.phone.is_not(None), Company.phone_e164.is_(None), Company.phone_type.is_(None))
    )
    last_id = 0
    while True:
        batch = list(session.scalars(stmt.where(Company.id > last_id).order_by(Company.id).limit(BACKFILL_BATCH)))
        if not batch:
            break
        for company in batch:
            before = (company.phone, company.phone_e164, company.phone_type)
            apply_company_phone(company)
            if (company.phone, company.phone_e164, company.phone_type) != before:
                n_companies += 1
                rescore(company)
        last_id = batch[-1].id
        session.commit()

    people = select(Contact).options(selectinload(Contact.company)).where(
        Contact.phone.is_not(None), Contact.phone_type.is_(None)
    )
    for contact in session.scalars(people):
        before = (contact.phone, contact.phone_type)
        apply_contact_phone(contact, contact.company)
        if (contact.phone, contact.phone_type) != before:
            n_contacts += 1
            if contact.company is not None:
                rescore(contact.company)
    session.commit()
    return n_companies, n_contacts
