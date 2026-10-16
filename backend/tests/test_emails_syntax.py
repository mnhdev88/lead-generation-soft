"""Email format checks and address kinds (role / free / disposable). No network."""
import pytest

from app.emails import normalize_email
from app.emails.syntax import check_syntax, is_disposable, is_free, is_role


@pytest.mark.parametrize("raw, expected", [
    ("info@acme.com", "info@acme.com"),
    ("  Info@ACME.com ", "info@acme.com"),
    ("MAILTO:sales@acme.com", "sales@acme.com"),
    ("mailto:%20info@acme.com", "info@acme.com"),  # percent-encoded space from a website link
    ("mailto:info@acme.com?subject=Hello", "info@acme.com"),
    ("<john.smith@acme.co.uk>", "john.smith@acme.co.uk"),
    ("john+news@acme.com", "john+news@acme.com"),
    ("o'brien@acme.ie", "o'brien@acme.ie"),
    ("info@acme.com.", "info@acme.com"),  # trailing dot of a fully-qualified domain
    ("jose@exämple.de", "jose@xn--exmple-cua.de"),  # international domain -> punycode
])
def test_normalize_valid(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ", "not an email", "john@", "@acme.com", "john@@acme.com", "a b@acme.com",
    "john..smith@acme.com", ".john@acme.com", "john.@acme.com",
    "john@acme", "john@acme.c", "john@acme.123", "john@-acme.com", "john@acme-.com", "john@acme..com",
    "logo@2x.png", "icon@3x.webp", "style@site.css", "x@localhost", "x@server.local",
    "you@example.com", "name@domain.com",
    "a" * 65 + "@acme.com",  # local part over 64 characters
    "a@" + ("b" * 60 + ".") * 5 + "com",  # whole address over 254 characters
])
def test_normalize_invalid(raw):
    assert normalize_email(raw) is None


def test_check_syntax_gives_reason():
    assert check_syntax("logo@2x.png") == (None, "Not a valid email domain")
    assert check_syntax("john..smith@acme.com") == (None, "Not a valid email address")
    assert check_syntax("you@example.com")[1] == "Placeholder address (not a real one)"


@pytest.mark.parametrize("email, expected", [
    ("info@acme.com", True),
    ("sales@acme.com", True),
    ("front.desk@acme.com", True),
    ("frontdesk@acme.com", True),
    ("customer-service@acme.com", True),
    ("info.karachi@acme.com", True),
    ("support+web@acme.com", True),
    ("appointments@clinic.com", True),
    ("enquiries@acme.co.uk", True),
    ("john@acme.com", False),
    ("john.smith@acme.com", False),
    ("dr.ali@clinic.com", False),
])
def test_is_role(email, expected):
    assert is_role(email) is expected


@pytest.mark.parametrize("email, expected", [
    ("someone@gmail.com", True),
    ("someone@yahoo.co.in", True),
    ("someone@rediffmail.com", True),
    ("someone@hotmail.co.uk", True),
    ("someone@outlook.com", True),
    ("someone@proton.me", True),
    ("someone@zoho.com", False),  # Zoho is mostly used by businesses with their own domain
    ("someone@acme.com", False),
])
def test_is_free(email, expected):
    assert is_free(email) is expected


@pytest.mark.parametrize("email, expected", [
    ("x@mailinator.com", True),
    ("x@guerrillamail.com", True),
    ("x@10minutemail.com", True),
    ("x@yopmail.com", True),
    ("x@sub.mailinator.com", True),  # sub-domains count too
    ("x@gmail.com", False),
    ("x@acme.com", False),
])
def test_is_disposable(email, expected):
    assert is_disposable(email) is expected
