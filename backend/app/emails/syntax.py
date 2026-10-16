"""Email address format checks (no network).

- check_syntax("  MAILTO:Info@Acme.com ") -> ("info@acme.com", None)
- is_role / is_free / is_disposable say what *kind* of address it is.

This is a practical "RFC-lite" check: it accepts what real mail servers accept and
rejects junk the website crawler sometimes picks up (e.g. "logo@2x.png").
"""
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

DATA_DIR = Path(__file__).resolve().parent

MAX_LENGTH = 254  # whole address (RFC 5321 path limit minus the <>)
MAX_LOCAL = 64  # part before the @
MAX_LABEL = 63  # each dot-separated part of the domain

# Characters allowed in the part before the @ (unquoted "dot-atom" form).
_LOCAL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")

# Things that look like a top-level domain but are really file names or test names.
BAD_TLDS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "tif", "tiff", "avif",
    "css", "js", "json", "xml", "html", "htm", "php", "asp", "aspx", "pdf", "doc", "docx",
    "txt", "zip", "mp4", "mp3", "woff", "woff2", "ttf",
    "local", "localhost", "localdomain", "invalid", "test", "example", "internal", "lan", "home",
}
# Reserved "placeholder" domains (RFC 2606), often left in website templates.
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com",
                       "yourdomain.com", "yoursite.com", "yourcompany.com"}

# Local parts that belong to a team or function, not to one person.
ROLE_LOCAL_PARTS = {
    "info", "information", "contact", "contacts", "contactus", "sales", "admin", "administrator",
    "office", "hello", "hi", "hey", "support", "help", "helpdesk", "billing", "accounts", "accounting",
    "finance", "invoice", "invoices", "payments", "payroll", "careers", "career", "jobs", "job", "hr",
    "recruiting", "recruitment", "hiring", "talent", "marketing", "team", "staff", "reception",
    "enquiries", "enquiry", "inquiries", "inquiry", "enquire", "frontdesk", "appointments",
    "appointment", "booking", "bookings", "book", "reservations", "reservation", "mail", "email",
    "webmaster", "postmaster", "hostmaster", "abuse", "noreply", "donotreply", "service",
    "services", "customerservice", "customercare", "customersupport", "care", "feedback", "press",
    "media", "pr", "news", "newsletter", "legal", "privacy", "compliance", "security", "it", "tech",
    "orders", "order", "shop", "store", "events", "partners", "partnership", "general", "main",
    "clinic", "patients", "patient", "practice", "management", "manager", "operations", "ops",
    "purchasing", "procurement", "quote", "quotes", "estimates", "admissions", "membership",
    "studio", "kontakt", "contacto", "contato", "web", "website", "online", "social", "all",
    "everyone", "director", "concierge", "catering", "pharmacy", "lab", "dispatch", "export",
    "import", "trade", "agency", "desk", "query", "queries", "request", "requests", "welcome",
}
# Common separators people put inside role addresses ("front.desk", "customer-service").
_SEPARATORS_RE = re.compile(r"[._\-]+")


def check_syntax(value: str | None) -> tuple[str | None, str | None]:
    """Clean up and validate an address.
    Returns (normalized_email, None) when OK, or (None, short reason) when not."""
    if not value:
        return None, "Empty email address"
    s = unquote(str(value)).strip()  # "mailto:%20info@x.com" -> "mailto: info@x.com"
    if s.lower().startswith("mailto:"):
        s = s[7:]
    s = s.split("?", 1)[0].strip().strip("<>").strip().lower()  # drop "?subject=..."
    if not s or " " in s or s.count("@") != 1:
        return None, "Not a valid email address"
    if len(s) > MAX_LENGTH:
        return None, "Email address is too long"

    local, domain = s.split("@")
    domain = domain.rstrip(".")
    if not local or len(local) > MAX_LOCAL or ".." in local or not _LOCAL_RE.match(local):
        return None, "Not a valid email address"

    if not domain.isascii():  # international domain -> punycode ("xn--...")
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return None, "Not a valid email domain"
    labels = domain.split(".")
    if len(labels) < 2 or any(len(lbl) > MAX_LABEL or not _LABEL_RE.match(lbl) for lbl in labels):
        return None, "Not a valid email domain"
    tld = labels[-1]
    if not _TLD_RE.match(tld) or tld in BAD_TLDS:
        return None, "Not a valid email domain"
    if domain in PLACEHOLDER_DOMAINS:
        return None, "Placeholder address (not a real one)"
    return f"{local}@{domain}", None


def normalize_email(value: str | None) -> str | None:
    """'  MAILTO:Info@Acme.com ' -> 'info@acme.com'. None when it isn't a valid address."""
    return check_syntax(value)[0]


def split_email(email: str) -> tuple[str, str]:
    """'info@acme.com' -> ('info', 'acme.com'). Expects a normalized address."""
    local, _, domain = email.rpartition("@")
    return local, domain


def is_role(email: str) -> bool:
    """info@, sales@, front.desk@, info.karachi@, support+web@ ... (a team, not a person)."""
    local = split_email(email)[0].split("+", 1)[0]
    if local in ROLE_LOCAL_PARTS or _SEPARATORS_RE.sub("", local) in ROLE_LOCAL_PARTS:
        return True
    first_word = _SEPARATORS_RE.split(local)[0]
    return first_word in ROLE_LOCAL_PARTS


@lru_cache(maxsize=None)
def _load_domains(filename: str) -> frozenset[str]:
    """One domain per line; '#' starts a comment."""
    text = (DATA_DIR / filename).read_text(encoding="utf-8")
    domains = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            domains.add(line)
    return frozenset(domains)


def _matches(domain: str, domains: frozenset[str]) -> bool:
    """True if `domain` or any parent domain is in the set (mail.mailinator.com -> mailinator.com)."""
    parts = domain.split(".")
    return any(".".join(parts[i:]) in domains for i in range(len(parts) - 1))


def is_free(email: str) -> bool:
    """Free webmail (Gmail, Yahoo, Outlook, Rediffmail...)."""
    return split_email(email)[1] in _load_domains("free_domains.txt")


def is_disposable(email: str) -> bool:
    """Throw-away inbox services (Mailinator, 10minutemail...)."""
    return _matches(split_email(email)[1], _load_domains("disposable_domains.txt"))
