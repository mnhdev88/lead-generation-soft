"""Phone numbers: standard format, country detection, line type."""
import pytest

from app.models import Company, Contact
from app.phones import apply_company_phone, best_phone, country_code, normalize_saved_phones, parse_phone

KARACHI = (24.86, 67.0)
AUSTIN = (30.27, -97.74)


@pytest.mark.parametrize("raw, country, expected", [
    ("0300 1234567", "PK", ("+923001234567", "mobile")),
    ("0300-1234567", "Pakistan", ("+923001234567", "mobile")),
    ("+92 300 1234567", None, ("+923001234567", "mobile")),
    ("0092 300 1234567", None, ("+923001234567", "mobile")),
    ("98765 43210", "IN", ("+919876543210", "mobile")),
    ("+1-512-351-8010", None, ("+15123518010", "landline_or_mobile")),
    ("(512) 351-8010", "United States", ("+15123518010", "landline_or_mobile")),
    ("1-800-555-0199", "US", ("+18005550199", "toll_free")),
])
def test_reads_numbers(raw, country, expected):
    info = parse_phone(raw, country)
    assert (info.e164, info.type) == expected


def test_display_is_international_format():
    assert parse_phone("03001234567", "PK").display == "+92 300 1234567"


def test_impossible_number_is_invalid_and_kept_as_written():
    info = parse_phone("0300 12", "PK")
    assert info.type == "invalid" and info.e164 is None and info.display == "0300 12"


def test_ambiguous_number_uses_the_map_position():
    # A Karachi landline written without the 0... and also a valid Los Angeles number.
    assert parse_phone("(21) 3582 1955", lat=KARACHI[0], lon=KARACHI[1]).e164 == "+922135821955"
    assert parse_phone("(21) 3582 1955", lat=AUSTIN[0], lon=AUSTIN[1]).e164 == "+12135821955"


def test_ambiguous_number_without_any_hint_stays_unknown():
    info = parse_phone("(21) 3582 1955")
    assert info.e164 is None and info.type is None and info.display == "(21) 3582 1955"


def test_number_valid_in_only_one_target_country_needs_no_hint():
    assert parse_phone("03152106954").e164 == "+923152106954"


def test_empty():
    assert parse_phone(None) is None and parse_phone("  ") is None


def test_country_code():
    assert country_code("pk") == "PK" and country_code("United States") == "US" and country_code("Narnia") is None


def test_best_phone_skips_impossible_numbers():
    assert best_phone(["0300 12", "0300 1234567"], "PK") == "0300 1234567"
    assert best_phone(["0300 12"], "PK") is None


def test_apply_company_phone():
    c = Company(name="A", source="osm", source_ref="1", phone="0300-1234567", country="PK")
    apply_company_phone(c)
    assert (c.phone, c.phone_e164, c.phone_type) == ("+92 300 1234567", "+923001234567", "mobile")


def test_normalize_saved_phones_and_rescore(db):
    company = Company(name="A", source="osm", source_ref="1", phone="0300 1234567", lat=KARACHI[0], lon=KARACHI[1])
    company.contacts.append(Contact(full_name="Ali Khan", phone="0321 7654321"))
    db.add(company)
    db.commit()

    assert normalize_saved_phones(db) == (1, 1)
    assert company.phone_e164 == "+923001234567" and company.phone_type == "mobile"
    assert company.contacts[0].phone == "+92 321 7654321" and company.contacts[0].phone_type == "mobile"
    assert "Mobile number +5" in company.score_reasons
    assert normalize_saved_phones(db) == (0, 0)  # nothing left to do


def test_invalid_phone_scores_nothing(db):
    from app.scoring import score_company

    company = Company(name="A", source="osm", source_ref="1", phone="0300 12", country="PK")
    apply_company_phone(company)
    _, reasons = score_company(company)
    assert not any(r.startswith("Phone") for r in reasons)
