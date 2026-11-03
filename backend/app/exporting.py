"""Turn companies / people into table rows and download them as CSV or Excel (.xlsx).

company_table / people_table return (headers, rows); the same rows feed the CSV and Excel
downloads and the Google Sheets export (app/sheets.py).
"""
import csv
import io
import re
from collections.abc import Iterable

from fastapi.responses import Response, StreamingResponse

from .models import Company, Contact
from .people import SENIORITY_RANK
from .phones import country_code, parse_phone
from .ppp import _google_part, us_location

Cell = str | int | float | None

# Same columns and order as the original CSV export (Steps 1-3), then the Step 4-6 extras.
COMPANY_HEADERS = [
    "name", "category", "phone", "email", "emails", "website", "domain", "address", "city", "country",
    "lat", "lon", "rating", "reviews_count", "opening_hours", "google_maps_url", "business_status",
    "facebook", "instagram", "linkedin", "platform", "https", "copyright_year", "contacts", "source",
    # Step 4-6
    "email_status", "lead_status", "score", "score_reasons",
    "decision_maker", "decision_maker_title", "decision_maker_email",
    # Phone check (app/phones.py)
    "phone_type",
    # Company size (app/sizing.py) and the US PPP loan (app/ppp.py)
    "size_band", "employees", "employees_source", "founded_year", "locations", "hiring",
    "ppp_jobs", "ppp_loan_date",
    # Buying intent per service (app/intent.py); reasons = those of the best service
    "intent_best", "intent_website", "intent_seo", "intent_software", "intent_marketing", "intent_reasons",
]

PEOPLE_HEADERS = [
    "name", "first_name", "last_name", "title", "seniority", "decision_maker", "email", "email_status",
    "email_confidence", "phone", "linkedin", "company", "company_website", "company_phone", "city",
    "country", "category", "lead_status", "score",
]


def _yes_no(value) -> str:
    return "" if value is None else ("yes" if value else "no")


def _contacts_text(company: Company) -> str:
    """'Ali Khan (Director) <ali@x.com>; Sara <sara@x.com>'"""
    parts = []
    for c in company.contacts or []:
        text = c.full_name or ""
        if c.title:
            text += f" ({c.title})"
        if c.email:
            text += f" <{c.email}>"
        if text.strip():
            parts.append(text.strip())
    return "; ".join(parts)


def _unique_emails(company: Company) -> list[str]:
    """Best email first, no duplicates (case-insensitive)."""
    seen, result = set(), []
    for e in [company.email, *(company.emails or [])]:
        if e and e.lower() not in seen:
            seen.add(e.lower())
            result.append(e)
    return result


def best_decision_maker(company: Company) -> Contact | None:
    """The most senior decision-maker, preferring people with an email."""
    deciders = [c for c in company.contacts or [] if c.is_decision_maker]
    if not deciders:
        return None
    return min(deciders, key=lambda c: (not c.email, SENIORITY_RANK.get(c.seniority, 9), c.id or 0))


def _intent_reasons(company: Company) -> str:
    """Reasons of the company's best service: 'No SSL (http only) +15; No website +30'."""
    best = (company.intent or {}).get(company.intent_best) if company.intent_best else None
    return "; ".join(best.get("reasons") or []) if isinstance(best, dict) else ""


def company_table(companies: Iterable[Company]) -> tuple[list[str], list[list[Cell]]]:
    rows = []
    for c in companies:
        socials = c.socials or {}
        signals = c.website_signals or {}
        crawled = c.enrich_status == "done"  # website facts are only reliable after a good crawl
        # Size facts (hiring) stay true when a later re-crawl fails: same rule as app/sizing.py.
        size_facts = bool(signals.get("pages_crawled"))
        statuses = c.email_statuses or {}
        dm = best_decision_maker(c)
        ppp = c.ppp or {}
        rows.append([
            c.name, c.category, c.phone, c.email, "; ".join(_unique_emails(c)), c.website, c.domain,
            c.address, c.city, c.country, c.lat, c.lon, c.rating, c.reviews_count, c.opening_hours,
            c.google_maps_url, c.business_status,
            socials.get("facebook"), socials.get("instagram"), socials.get("linkedin"),
            signals.get("platform") if crawled else None,
            _yes_no(signals.get("https")) if crawled else "",
            signals.get("copyright_year") if crawled else None,
            _contacts_text(c), c.source,
            statuses.get(c.email) if c.email else None,
            c.lead_status or "new", c.score, "; ".join(c.score_reasons or []),
            dm.full_name if dm else None, dm.title if dm else None, dm.email if dm else None,
            c.phone_type,
            c.size_band, c.employees, c.employees_source, c.founded_year, c.locations_count,
            _yes_no(bool(signals.get("has_careers"))) if size_facts else "",
            ppp.get("jobs"), ppp.get("date_approved"),
            c.intent_best, c.intent_website, c.intent_seo, c.intent_software, c.intent_marketing,
            _intent_reasons(c),
        ])
    return list(COMPANY_HEADERS), rows


def people_table(contacts: Iterable[Contact]) -> tuple[list[str], list[list[Cell]]]:
    rows = []
    for p in contacts:
        co = p.company
        rows.append([
            p.full_name, p.first_name, p.last_name, p.title, p.seniority, _yes_no(bool(p.is_decision_maker)),
            p.email, p.email_status, p.email_confidence, p.phone, p.linkedin_url,
            co.name if co else None, co.website if co else None, co.phone if co else None,
            co.city if co else None, co.country if co else None, co.category if co else None,
            p.lead_status or "new", p.score,
        ])
    return list(PEOPLE_HEADERS), rows


# --- "Custom export": the columns of our CRM import template --------------------------

CUSTOM_HEADERS = [
    "company_name", "name", "phone", "phone_2", "email", "email_2", "website_url", "social_url",
    "whatsapp_number", "address", "city", "state", "zip_code", "country", "source", "gmb_review_rating",
    "number_of_reviews", "gmb_url", "gmb_category", "priority", "notes",
]

# social_url holds one link: the first of these the company has.
SOCIAL_ORDER = ("instagram", "facebook", "linkedin", "tiktok", "twitter", "youtube")
SOURCE_LABELS = {"google": "GMB", "osm": "OpenStreetMap"}
# Other phone fields in the source data (OpenStreetMap can hold several, split by ";").
RAW_PHONE_KEYS = ("internationalPhoneNumber", "phone", "contact:phone", "mobile", "contact:mobile")
PRIORITY_HIGH_SCORE = 70
PRIORITY_NORMAL_SCORE = 40


def _custom_phones(company: Company, person: Contact | None) -> list[str]:
    """The person's number first, then the company's, without duplicates or impossible numbers."""
    raw = company.raw if isinstance(company.raw, dict) else {}
    candidates = [person.phone if person else None, company.phone]
    for key in RAW_PHONE_KEYS:
        candidates += re.split(r"[;,]", str(raw.get(key) or ""))
    seen, phones = set(), []
    for text in candidates:
        info = parse_phone(text, company.country, company.lat, company.lon)
        if not info or info.type == "invalid":
            continue
        key = info.e164 or info.display
        if key not in seen:
            seen.add(key)
            phones.append(info.display)
    return phones


def _custom_emails(company: Company, person: Contact | None) -> list[str]:
    """The person's email first (it belongs to `name`), then the company's. Known-bad ones are left out."""
    statuses = company.email_statuses or {}
    emails = [e for e in _unique_emails(company) if statuses.get(e) != "invalid"]
    if person and person.email and person.email_status != "invalid":
        emails = [person.email, *(e for e in emails if e.lower() != person.email.lower())]
    return emails


def _social_url(company: Company, person: Contact | None, person_first: bool) -> str | None:
    socials = company.socials or {}
    own = next((socials[k] for k in SOCIAL_ORDER if socials.get(k)), None)
    linkedin = person.linkedin_url if person else None
    return (linkedin or own) if person_first else (own or linkedin)


def _whatsapp_number(company: Company) -> str | None:
    """Only a real WhatsApp link from the website (https://wa.me/923001234567) counts."""
    link = (company.socials or {}).get("whatsapp")
    digits = re.sub(r"\D", "", link or "")
    info = parse_phone(f"+{digits}") if digits else None
    return info.display if info and info.e164 else None


def _street_address(address: str | None, city: str | None, zip_code: str | None = None) -> str | None:
    """The part before the city: '9835 SW 40th St, Miami, FL 33165, USA' -> '9835 SW 40th St'.
    The full text stays when the city isn't one of its parts, so nothing gets lost; only a
    ZIP at the end goes, as it has its own column: '1000 West Lynn St, 78703'."""
    if not address:
        return None
    parts = [p.strip() for p in address.split(",")]
    if city:
        for i in range(len(parts) - 1, -1, -1):  # from the end: a street can be named after the city
            if parts[i].lower() == city.strip().lower():
                return ", ".join(parts[:i]) or None
    if zip_code and parts[-1] == zip_code:
        parts.pop()
    return ", ".join(parts) or None


def _custom_category(category: str | None) -> str | None:
    """'fast_food' -> 'Fast food', like the template. 'Dental Clinic' stays as it is."""
    if not category or not category.islower():
        return category
    return category.replace("_", " ").capitalize()


def _state_zip_country(company: Company) -> tuple[str | None, str | None, str | None]:
    """('TX', '78704', 'US'). These aren't columns of their own: they come from the source data."""
    raw = company.raw if isinstance(company.raw, dict) else {}
    us_state, us_zip = us_location(company)
    state = (us_state or _google_part(raw, "administrative_area_level_1")
             or raw.get("addr:state") or raw.get("addr:province"))
    zip_code = us_zip or _google_part(raw, "postal_code") or raw.get("addr:postcode")
    country = (country_code(company.country) or country_code(_google_part(raw, "country"))
               or country_code(raw.get("addr:country")))
    if not country and us_state and us_zip:
        country = "US"  # a US state + ZIP in the address
    return state, zip_code, country or company.country


def _priority(score: int | None) -> str:
    if score is None or score < PRIORITY_NORMAL_SCORE:
        return "Low"
    return "High" if score >= PRIORITY_HIGH_SCORE else "Normal"


def _custom_notes(title: str | None, score: int | None, reasons: list | None, best_service: str | None) -> str | None:
    """'Owner | Score 55: Verified email +25; Phone +10 | Best fit: website'"""
    parts = [title] if title else []
    if score is not None:
        parts.append(f"Score {score}: {'; '.join(reasons)}" if reasons else f"Score {score}")
    if best_service:
        parts.append(f"Best fit: {best_service}")
    return " | ".join(parts) or None


def _custom_row(company: Company | None, person: Contact | None, lead: Company | Contact) -> list[Cell]:
    """One template row. `lead` is the company or the person the row is about: its score sets the
    priority and the notes. Numbers are written as text, the way the template holds them."""
    co = company or Company()  # a person without a company still gets a row
    phones = _custom_phones(co, person)
    emails = _custom_emails(co, person)
    state, zip_code, country = _state_zip_country(co)
    return [
        co.name, person.full_name if person else None,
        phones[0] if phones else None, phones[1] if len(phones) > 1 else None,
        emails[0] if emails else None, emails[1] if len(emails) > 1 else None,
        co.domain or co.website, _social_url(co, person, person_first=lead is person), _whatsapp_number(co),
        _street_address(co.address, co.city, zip_code), co.city, state, zip_code, country,
        SOURCE_LABELS.get(co.source, co.source),
        None if co.rating is None else str(co.rating),
        None if co.reviews_count is None else str(co.reviews_count),
        co.google_maps_url, _custom_category(co.category), _priority(lead.score),
        _custom_notes(person.title if person else None, lead.score, lead.score_reasons, co.intent_best),
    ]


def custom_company_table(companies: Iterable[Company]) -> tuple[list[str], list[list[Cell]]]:
    """One row per business; `name` is its most senior decision-maker."""
    return list(CUSTOM_HEADERS), [_custom_row(c, best_decision_maker(c), c) for c in companies]


def custom_people_table(contacts: Iterable[Contact]) -> tuple[list[str], list[list[Cell]]]:
    """One row per person."""
    return list(CUSTOM_HEADERS), [_custom_row(p.company, p, p) for p in contacts]


# --- spreadsheet safety --------------------------------------------------------------

# Text scraped from websites could start with "=" and run as a formula when the file is
# opened in Excel ("CSV injection"). Phone numbers like "+1 555-0100" are left alone.
_SAFE_NUMBERISH = re.compile(r"[+\-]?[\d\s().+\-/]*")


def safe_cell(value: Cell) -> Cell:
    if not isinstance(value, str) or not value:
        return value
    first = value[0]
    if first in "=@\t\r" or (first in "+-" and not _SAFE_NUMBERISH.fullmatch(value)):
        return "'" + value
    return value


def _download_headers(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


def csv_response(headers: list[str], rows: list[list[Cell]], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["" if v is None else safe_cell(v) for v in row])
    # BOM so Excel opens Urdu/Hindi characters correctly
    data = "﻿" + buf.getvalue()
    return StreamingResponse(
        iter([data]), media_type="text/csv; charset=utf-8", headers=_download_headers(filename)
    )


LINK_COLUMNS = {"website", "company_website", "google_maps_url", "linkedin", "facebook", "instagram"}
MAX_COLUMN_WIDTH = 50


def xlsx_bytes(headers: list[str], rows: list[list[Cell]], sheet_title: str = "Leads") -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"  # keep the header row visible while scrolling

    widths = [len(h) for h in headers]
    link_font = Font(color="0563C1", underline="single")
    for r, row in enumerate(rows, start=2):
        for col, value in enumerate(row, start=1):
            if isinstance(value, str):
                # Excel files can't hold control characters; strip them.
                value = safe_cell(ILLEGAL_CHARACTERS_RE.sub("", value))
            cell = ws.cell(row=r, column=col, value=value)
            if isinstance(value, str):
                cell.data_type = "s"  # always text, never a formula
                widths[col - 1] = max(widths[col - 1], len(value))
                if headers[col - 1] in LINK_COLUMNS and value.startswith(("http://", "https://")):
                    cell.hyperlink = value
                    cell.font = link_font
            elif value is not None:
                widths[col - 1] = max(widths[col - 1], len(str(value)))
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = min(width + 2, MAX_COLUMN_WIDTH)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_response(headers: list[str], rows: list[list[Cell]], filename: str) -> Response:
    return Response(
        content=xlsx_bytes(headers, rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=_download_headers(filename),
    )
