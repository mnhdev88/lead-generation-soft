"""Pure functions that pull contact details out of one HTML page.

Nothing here touches the network: every function takes the page HTML (and the
page URL, used to resolve relative links) and returns plain Python data.
The crawler (crawler.py) fetches pages and merges the results.

Main entry points:
    analyze_page(html, page_url)        -> PageInfo (everything below in one parse)
    extract_emails / extract_phones / extract_socials / extract_people
    detect_signals(html, page_url)      -> dict (platform, copyright year, ...)
    find_candidate_links(html, base_url)-> same-site URLs worth crawling (contact, about, team...)
    rank_emails(emails, site)           -> emails sorted best-first for that website

Pages come from the open internet, so every regex here is written to run in linear
time even on a hostile 2 MB page (bounded repeats, no nested "+" inside "+"), and
the number of emails / phones / people taken from one page is capped.
"""
from __future__ import annotations

import html as html_lib
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse


@dataclass
class PersonFound:
    full_name: str
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    source_url: str | None = None


@dataclass
class PageInfo:
    """Everything extracted from one page."""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    people: list[PersonFound] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    links: list[str] = field(default_factory=list)  # candidate pages to crawl next, best first


# Limits per page: a real business page never has more; a page that does is a
# directory, a review feed or junk, and taking everything would only add noise.
MAX_EMAILS_PER_PAGE = 50
MAX_PHONES_PER_PAGE = 20
MAX_PEOPLE_PER_PAGE = 40


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

# Second-level labels used under country domains, e.g. example.co.uk, example.com.pk
_SECOND_LEVEL = {
    "co", "com", "org", "net", "gov", "edu", "ac", "gob", "nic", "or", "ne", "go", "mil",
    "ltd", "plc", "sch", "nhs", "web", "info", "biz", "gen", "firm", "ind", "res",
}


def registered_domain(host: str | None) -> str:
    """'www.shop.example.co.uk' -> 'example.co.uk'. A simple approximation that is good
    enough to decide whether two URLs belong to the same website."""
    host = (host or "").lower().strip().strip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host  # IP address: compare as-is
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# HTML parsing (stdlib HTMLParser): one pass collects everything we need
# ---------------------------------------------------------------------------

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
              "param", "source", "track", "wbr"}
# Text inside these is never shown to visitors.
_HIDDEN_TAGS = {"script", "style", "noscript", "template", "svg", "head", "title"}
# Inline tags don't break a sentence when we build the page's full text.
_INLINE_TAGS = {"a", "span", "strong", "b", "em", "i", "small", "font", "u", "sup", "sub",
                "abbr", "mark", "code", "bdi", "time", "label"}
# Elements that can act as a "card" grouping one person's name, title and links.
_CONTAINER_TAGS = {"div", "li", "article", "section", "td", "th", "tr", "figure", "aside",
                   "header", "footer", "main", "ul", "ol", "table", "dl", "dd", "address",
                   "blockquote", "p", "form", "nav"}
# Deeper nesting only happens in broken or hostile HTML; extra levels are ignored so
# that every parser step stays fast.
_MAX_DEPTH = 400
# Real pages have a few hundred links at most; more is a link farm or junk.
_MAX_LINKS = 5000
# Marks the place of an inline tag in the page text (see _PageParser.text).
_INLINE_MARK = "\x00"


def _is_menu(tag: str, attrs: dict[str, str]) -> bool:
    """<nav> or an element whose class/id says it is a menu or dropdown."""
    if tag == "nav":
        return True
    names = (attrs.get("class", "") + " " + attrs.get("id", "")).lower()
    return "menu" in names or "nav" in names or "dropdown" in names


@dataclass
class _Chunk:
    """A run of text with no tag inside it, e.g. the text of <h3>Jane Smith</h3>."""
    text: str
    cards: tuple[int, ...]  # ids of enclosing container elements, outermost first
    in_link: bool = False  # inside an <a> (e.g. a menu item)
    in_menu: bool = False  # inside a <nav> / menu / dropdown


@dataclass
class _Link:
    href: str
    text: str
    cards: tuple[int, ...]


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # turns &amp; &#64; etc. into real characters
        # Open elements: (tag, unique id, len(self._cards_open) before it, is it a menu?)
        self.stack: list[tuple[str, int, int, bool]] = []
        self._open: dict[str, int] = {}  # how many elements of each tag are open right now
        self._cards_open: list[int] = []  # ids of the open container elements, outermost first
        self._menus_open = 0  # open <nav>/menu elements
        self._hidden_open = 0  # open <script>, <style>... elements
        self._next_id = 0
        self._buf: list[str] = []  # text of the current chunk
        self.chunks: list[_Chunk] = []
        self.text_parts: list[str] = []  # whole visible text (for regex searches)
        self.links: list[_Link] = []
        self._open_link: dict | None = None
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self._jsonld_buf: list[str] | None = None
        self.cf_hex: list[str] = []  # Cloudflare-protected emails (still encoded)
        self.has_contact_form = False

    # -- helpers --
    def _cards(self) -> tuple[int, ...]:
        return tuple(self._cards_open)

    def _hidden(self) -> bool:
        return self._hidden_open > 0

    def _inside(self, tag: str) -> bool:
        return bool(self._open.get(tag))

    def _push(self, tag: str, menu: bool) -> None:
        self._next_id += 1
        self.stack.append((tag, self._next_id, len(self._cards_open), menu))
        self._open[tag] = self._open.get(tag, 0) + 1
        if tag in _CONTAINER_TAGS:
            self._cards_open.append(self._next_id)
        if menu:
            self._menus_open += 1
        if tag in _HIDDEN_TAGS:
            self._hidden_open += 1

    def _pop_to(self, idx: int) -> None:
        """Close stack[idx] and everything opened after it."""
        for tag, _id, _cards, menu in self.stack[idx:]:
            self._open[tag] -= 1
            if menu:
                self._menus_open -= 1
            if tag in _HIDDEN_TAGS:
                self._hidden_open -= 1
        del self._cards_open[self.stack[idx][2]:]
        del self.stack[idx:]

    def _flush(self) -> None:
        if self._buf:
            text = " ".join("".join(self._buf).split())
            if text:
                self.chunks.append(_Chunk(text, self._cards(), self._open_link is not None,
                                          self._menus_open > 0))
            self._buf = []

    def _close_link(self) -> None:
        if self._open_link is not None:
            link = self._open_link
            if len(self.links) < _MAX_LINKS:
                text = " ".join(" ".join(link["text"]).split())
                self.links.append(_Link(link["href"], text, link["cards"]))
            self._open_link = None

    # -- HTMLParser callbacks --
    def handle_starttag(self, tag, attrs):
        self._flush()
        self.text_parts.append(_INLINE_MARK if tag in _INLINE_TAGS else "\n")
        a = {k.lower(): (v or "") for k, v in attrs}

        if "data-cfemail" in a:
            self.cf_hex.append(a["data-cfemail"])

        if tag == "a":
            self._close_link()
            href = a.get("href", "").strip()
            if "/cdn-cgi/l/email-protection#" in href:
                self.cf_hex.append(href.split("#", 1)[1])
            self._open_link = {"href": href, "text": [], "cards": self._cards()}
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "input" and self._inside("form"):
            kind = a.get("type", "").lower()
            name = (a.get("name", "") + " " + a.get("id", "")).lower()
            if kind == "email" or "email" in name or "e-mail" in name:
                self.has_contact_form = True
        elif tag == "textarea" and self._inside("form"):
            self.has_contact_form = True
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._jsonld_buf = []

        if tag in _VOID_TAGS:
            return
        # A new <li>/<p> implicitly closes an unclosed sibling of the same kind.
        if tag in ("li", "p") and self.stack and self.stack[-1][0] == tag:
            self._pop_to(len(self.stack) - 1)
        if len(self.stack) < _MAX_DEPTH:
            self._push(tag, _is_menu(tag, a))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self._flush()
        self.text_parts.append(_INLINE_MARK if tag in _INLINE_TAGS else "\n")
        if tag == "a":
            self._close_link()
        if tag == "script" and self._jsonld_buf is not None:
            self.jsonld.append("".join(self._jsonld_buf))
            self._jsonld_buf = None
        if not self._open.get(tag):
            return  # stray end tag
        # Close the matching open tag (and anything left open inside it).
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx][0] == tag:
                self._pop_to(idx)
                break

    def handle_data(self, data):
        if self._jsonld_buf is not None:
            self._jsonld_buf.append(data)
            return
        if self.stack and self.stack[-1][0] == "title":
            self.title_parts.append(data)
            return
        if self._hidden():
            return
        self._buf.append(data)
        self.text_parts.append(data)
        if self._open_link is not None:
            self._open_link["text"].append(data)

    def close(self):
        try:
            super().close()
        finally:
            self._flush()
            self._close_link()

    @property
    def text(self) -> str:
        """All visible text. Block tags become line breaks. Inline tags join their text
        directly ("info<span>@</span>shop.com" -> "info@shop.com"), except between two
        words ("<b>Email</b><a>info@shop.com</a>" -> "Email info@shop.com")."""
        text = "".join(self.text_parts)
        text = re.sub(r"(?<=\w)\x00+(?=\w)", " ", text)
        return text.replace(_INLINE_MARK, "")


# A "<" that starts a tag which never closes: another "<" (or the end of the page) comes
# before the next ">". Python's HTMLParser gets extremely slow on runs of these
# ("<a <a <a ..."), so they are written as plain text. Runs in linear time.
_UNCLOSED_TAG_RE = re.compile(r"<(?=[^<>]*(?:<|$))")


def _strip_comments(html: str) -> str:
    """Remove <!-- comments --> (an unclosed one hides the rest of the page, as in a
    browser). Done with str.find, so it stays fast on hostile pages."""
    parts: list[str] = []
    pos = 0
    while True:
        start = html.find("<!--", pos)
        if start < 0:
            parts.append(html[pos:])
            break
        parts.append(html[pos:start])
        end = html.find("-->", start + 4)
        if end < 0:
            break
        pos = end + 3
    return "".join(parts)


def _parse(html: str) -> _PageParser:
    parser = _PageParser()
    # A "<" that can't start a tag is plain text anyway. Writing it as "&lt;" lets the
    # parser handle a run of them in one go instead of one character at a time.
    html = re.sub(r"<(?![a-zA-Z/!?])", "&lt;", html or "")
    # Comments first: tags inside them ("<!-- <p>old</p> -->") must not count as unclosed.
    html = _UNCLOSED_TAG_RE.sub("&lt;", _strip_comments(html))
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # HTMLParser is lenient, but never let one odd page crash a crawl
        parser._flush()
    return parser


def _resolve(href: str, page_url: str) -> str:
    try:
        return urljoin(page_url, href) if page_url else href
    except ValueError:
        return href


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

# The look-behind means an email must start at the beginning of a "word": this stops
# "%20info@x.com" (a URL-encoded space) from giving "20info@x.com", and keeps the regex
# fast on long runs of letters. All repeats are bounded, so it runs in linear time.
# "%" is not allowed in the address itself: real addresses practically never contain it, and
# "x%20info@shop.com" is a URL-encoded space, not an email.
EMAIL_RE = re.compile(
    r"(?<![a-zA-Z0-9_.%+\-])[a-zA-Z0-9][a-zA-Z0-9._+\-]{0,63}@(?:[a-zA-Z0-9\-]{1,63}\.){1,8}[a-zA-Z]{2,24}")
_VALID_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._+\-]{0,63}@(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,24}$")

# "logo@2x.png" looks like an email to a regex; these endings show it is a file name.
_FILE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "tif", "tiff", "avif",
              "css", "js", "json", "php", "html", "htm", "xml", "pdf", "mp4", "mp3", "webm",
              "woff", "woff2", "ttf", "eot", "otf", "map", "txt", "zip", "gz"}
_JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "email.com", "yourdomain.com",
    "yoursite.com", "yourcompany.com", "company.com", "website.com", "mysite.com", "test.com",
    "sample.com", "address.com", "yourwebsite.com", "mydomain.com", "domain.tld", "sentry.io",
    "wixpress.com", "sentry-next.wixpress.com", "sentry.wixpress.com", "ingest.sentry.io",
    "placeholder.com", "emailaddress.com",
}
_JUNK_LOCALS = {
    "user", "username", "yourname", "your.name", "youremail", "your.email", "your-email",
    "name", "email", "e-mail", "test", "firstname", "first.last", "firstname.lastname",
    "john.doe", "johndoe", "jane.doe", "janedoe", "someone", "somebody", "you", "me",
    "example", "sample", "abc", "xyz",
}
NOREPLY_RE = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|bounces?)([+\-_.].*)?$")


def clean_email(raw: str | None) -> str | None:
    """Normalize one candidate email, or return None if it is junk."""
    if not raw:
        return None
    email = raw.strip().strip(".,;:'\"()<>[]{}").lower()
    if len(email) > 80 or email.count("@") != 1 or ".." in email:
        return None
    if not _VALID_EMAIL_RE.match(email):
        return None
    local, domain = email.split("@")
    tld = domain.rsplit(".", 1)[-1]
    if tld in _FILE_TLDS:
        return None
    if domain in _JUNK_DOMAINS or any(domain.endswith("." + d) for d in _JUNK_DOMAINS):
        return None
    if local in _JUNK_LOCALS or local.startswith("u00") or NOREPLY_RE.match(local):
        return None
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # error-tracker keys such as Sentry DSNs
        return None
    return email


def _deobfuscate(text: str) -> str:
    """'jane [at] example [dot] com' / 'jane(at)example.com' -> 'jane@example.com'."""
    # (spaces are limited to {0,3} so a page full of blanks can't make the regex slow)
    text = re.sub(r"\s{0,3}[\[\(\{<]\s{0,3}at\s{0,3}[\]\)\}>]\s{0,3}", "@", text, flags=re.I)
    text = re.sub(r"\s{0,3}[\[\(\{<]\s{0,3}dot\s{0,3}[\]\)\}>]\s{0,3}", ".", text, flags=re.I)
    # "jane at example dot com" (only when at least one " dot " follows, to stay safe)
    text = re.sub(
        r"(?<![\w.+\-])([\w.+\-]{1,64})\s{1,3}at\s{1,3}([\w\-]{1,63}(?:\s{1,3}dot\s{1,3}[\w\-]{1,63}){1,4})\b",
        lambda m: m.group(1) + "@" + re.sub(r"\s+dot\s+", ".", m.group(2), flags=re.I),
        text,
        flags=re.I,
    )
    return text


def decode_cfemail(hex_str: str) -> str | None:
    """Decode a Cloudflare 'email protection' string (first byte is the XOR key)."""
    try:
        data = bytes.fromhex(hex_str.strip())
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    try:
        return bytes(b ^ key for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _mailto_address(href: str) -> str | None:
    """'mailto:%20Info@Shop.com?subject=Hi' -> 'Info@Shop.com' (percent-codes decoded)."""
    if not href.lower().startswith("mailto:"):
        return None
    addr = unquote(href[7:].split("?", 1)[0]).split(",")[0]
    # Find the address itself, so "%20info@x.com" or "Jane <jane@x.com>" work too.
    match = EMAIL_RE.search(addr)
    return match.group(0) if match else (addr.strip() or None)


def _emails_from(parser: _PageParser, html: str) -> list[str]:
    candidates: list[str] = []
    for link in parser.links:
        addr = _mailto_address(link.href)
        if addr:
            candidates.append(addr)
    for hex_str in parser.cf_hex:
        decoded = decode_cfemail(hex_str)
        if decoded:
            candidates.append(decoded)
    candidates += EMAIL_RE.findall(_deobfuscate(parser.text))
    # Raw HTML too (emails inside attributes or page-builder JSON). First replace
    # JS escapes (">") and URL percent-codes ("%20") with spaces, so that
    # ">info@x.com" doesn't become "u003einfo@x.com" and "mailto:%20info@x.com"
    # doesn't become "20info@x.com".
    raw = re.sub(r"\\u00[0-9a-fA-F]{2}|%[0-9a-fA-F]{2}", " ", html_lib.unescape(html or ""))
    candidates += EMAIL_RE.findall(raw)

    result: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        email = clean_email(cand)
        if email and email not in seen:
            seen.add(email)
            result.append(email)
            if len(result) >= MAX_EMAILS_PER_PAGE:
                break
    return result


def extract_emails(html: str, page_url: str = "") -> list[str]:
    """All plausible emails on the page, lowercase, in the order found (mailto links first)."""
    return _emails_from(_parse(html), html)


# Role addresses: the "front door" of a business, best for outreach.
PREFERRED_GENERIC = {
    "info", "contact", "contacts", "hello", "hi", "office", "sales", "admin", "enquiries",
    "enquiry", "inquiries", "inquiry", "mail", "team", "reception", "frontdesk", "general",
    "booking", "bookings", "appointments", "appointment", "service", "orders", "studio",
}
# Role addresses that rarely reach a decision maker.
LOW_GENERIC = {
    "support", "help", "billing", "accounts", "accounting", "invoices", "careers", "jobs", "hr",
    "recruitment", "privacy", "legal", "webmaster", "abuse", "press", "media", "marketing",
    "newsletter", "feedback", "compliance", "dpo", "gdpr", "security", "wordpress", "wp",
    "hostmaster", "unsubscribe", "customerservice", "customercare",
}
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "gmx.com", "gmx.net",
    "mail.com", "yandex.com", "yandex.ru", "zoho.com", "rediffmail.com",
}


def is_generic_email(email: str) -> bool:
    local = email.split("@")[0]
    return local in PREFERRED_GENERIC or local in LOW_GENERIC or bool(NOREPLY_RE.match(local))


def rank_emails(emails: list[str], site: str | None) -> list[str]:
    """Sort emails best-first for a website (site = its URL or domain); drops no-reply.

    Order: same-domain role address (info@, contact@, sales@...) > same-domain personal >
    same-domain support/careers/etc > free-mail (gmail...) > any other domain
    (often the web designer). Ties keep the order the emails were found in.
    """
    site_host = _host_of(site) if site and "://" in site else (site or "").lower()
    site_domain = registered_domain(site_host)

    def tier(email: str) -> int:
        local, domain = email.split("@")
        if site_domain and registered_domain(domain) == site_domain:
            if local in PREFERRED_GENERIC:
                return 0
            if local in LOW_GENERIC:
                return 2
            return 1
        if domain in FREE_MAIL_DOMAINS:
            return 3
        return 4

    seen: list[str] = []
    for e in emails:
        e = (e or "").strip().lower()
        if e and "@" in e and e not in seen and not NOREPLY_RE.match(e.split("@")[0]):
            seen.append(e)
    return sorted(seen, key=tier)  # sorted() is stable, so discovery order is kept within a tier


# ---------------------------------------------------------------------------
# Phones
# ---------------------------------------------------------------------------

# Conservative patterns: a number must look *formatted* like a phone number, so that
# prices, dates, years and order IDs are not picked up.
_PHONE_TEXT_RE = re.compile(
    r"(?<![\w+/.\-])(?:"
    # +92 300 1234567, +923001234567, +44 (0)20 7946 0958. After the first group every
    # group needs a separator, so a long run of digits can't be split in many ways.
    r"\+\d{1,3}(?:[\s.\-]?\(\d{1,4}\))?[\s.\-]?\d{1,12}(?:[\s.\-]\d{1,8}){0,4}"
    r"|1[\s.\-]\d{3}[\s.\-]\d{3}[\s.\-]\d{4}"                      # 1-800-555-1234
    r"|\(\d{3}\)\s?\d{3}[\s.\-]\d{4}"                               # (555) 123-4567
    r"|\d{3}[.\-]\d{3}[.\-]\d{4}"                                   # 555-123-4567
    r"|0\d{3}[\s\-]\d{3}[\s\-]\d{4}"                                # 0300 123 4567 (PK mobile)
    r"|0\d{2,4}[\s\-]\d{6,8}"                                       # 0300-1234567, 042-35761234
    r"|[6-9]\d{4}[\s\-]\d{5}"                                       # 98765 43210 (IN mobile)
    r")(?![\w])(?![.\-/]\d)"
)


def clean_phone(raw: str | None) -> str | None:
    """Tidy a phone number for display, or None if it doesn't look like one."""
    if not raw:
        return None
    phone = unquote(raw).strip()
    phone = re.sub(r"^(tel|callto|phone):", "", phone, flags=re.I).strip()
    phone = re.sub(r"[^\d+()\-.\s/]", "", phone)  # drop letters, "ext" etc.
    phone = " ".join(phone.split()).strip(" -./")
    digits = re.sub(r"\D", "", phone)
    if not 7 <= len(digits) <= 15 or len(set(digits)) <= 2:
        return None
    return phone


def _phone_key(phone: str) -> str:
    """Numbers are 'the same' when their last 10 digits match (+92 300... == 0300...)."""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:]


def _phones_from(parser: _PageParser) -> list[str]:
    candidates = [link.href for link in parser.links if link.href.lower().startswith(("tel:", "callto:"))]
    candidates += _PHONE_TEXT_RE.findall(parser.text)
    result: list[str] = []
    keys: set[str] = set()
    for cand in candidates:
        phone = clean_phone(cand)
        if phone and _phone_key(phone) not in keys:
            keys.add(_phone_key(phone))
            result.append(phone)
            if len(result) >= MAX_PHONES_PER_PAGE:
                break
    return result


def extract_phones(html: str, page_url: str = "") -> list[str]:
    """Phone numbers: tel: links first, then clearly formatted numbers in the page text."""
    return _phones_from(_parse(html))


# ---------------------------------------------------------------------------
# Social profiles
# ---------------------------------------------------------------------------

SOCIAL_NETWORKS = ("facebook", "instagram", "linkedin", "twitter", "youtube", "tiktok", "whatsapp")

# First path parts that are share buttons, posts or app pages rather than a profile.
_SOCIAL_SKIP = {
    "facebook": {"sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login",
                 "login.php", "events", "photo", "photo.php", "photos", "watch", "story.php",
                 "permalink.php", "posts", "hashtag", "groups", "help", "policies", "privacy", "l.php"},
    "instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "share"},
    "twitter": {"intent", "share", "home", "search", "hashtag", "i", "login", "privacy", "tos"},
    "youtube": {"watch", "embed", "shorts", "results", "playlist", "feed", "redirect"},
}


def social_profile(href: str) -> tuple[str, str] | None:
    """Return (network, clean profile URL) if the link points to a business profile."""
    try:
        parsed = urlparse(href.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith(("www.", "m.", "mobile.", "web.")):
        host = host.split(".", 1)[1]
    parts = [p for p in parsed.path.split("/") if p]
    first = parts[0].lower() if parts else ""

    if host in ("facebook.com", "fb.com", "fb.me"):
        if not parts or first in _SOCIAL_SKIP["facebook"]:
            return None
        if first == "profile.php":
            pid = parse_qs(parsed.query).get("id", [""])[0]
            return ("facebook", f"https://www.facebook.com/profile.php?id={pid}") if pid.isdigit() else None
        if first == "pages":  # facebook.com/pages/Name/12345
            return "facebook", "https://www.facebook.com/" + "/".join(parts[:3])
        return "facebook", f"https://www.facebook.com/{parts[0]}"
    if host == "instagram.com":
        if not parts or first in _SOCIAL_SKIP["instagram"]:
            return None
        return "instagram", f"https://www.instagram.com/{parts[0]}"
    if host.endswith("linkedin.com"):
        if len(parts) >= 2 and first in ("company", "in", "school", "showcase"):
            return "linkedin", f"https://www.linkedin.com/{first}/{parts[1]}"
        return None
    if host in ("twitter.com", "x.com"):
        if not parts or first in _SOCIAL_SKIP["twitter"] or "status" in [p.lower() for p in parts]:
            return None
        return "twitter", f"https://x.com/{parts[0]}"
    if host == "youtube.com":
        if not parts or first in _SOCIAL_SKIP["youtube"]:
            return None
        if first in ("channel", "c", "user") and len(parts) >= 2:
            return "youtube", f"https://www.youtube.com/{first}/{parts[1]}"
        if first.startswith("@"):
            return "youtube", f"https://www.youtube.com/{parts[0]}"
        return None
    if host == "tiktok.com":
        if parts and first.startswith("@") and len(parts) == 1:
            return "tiktok", f"https://www.tiktok.com/{parts[0]}"
        return None
    if host == "wa.me":
        digits = re.sub(r"\D", "", parts[0]) if parts else ""
        return ("whatsapp", f"https://wa.me/{digits}") if len(digits) >= 7 else None
    if host == "api.whatsapp.com":
        digits = re.sub(r"\D", "", parse_qs(parsed.query).get("phone", [""])[0])
        return ("whatsapp", f"https://wa.me/{digits}") if len(digits) >= 7 else None
    return None


def _socials_from(parser: _PageParser, page_url: str) -> dict[str, str]:
    socials: dict[str, str] = {}
    for link in parser.links:
        found = social_profile(_resolve(link.href, page_url))
        if not found:
            continue
        network, url = found
        if network not in socials:  # the first link for each network wins...
            socials[network] = url
        elif network == "linkedin" and "/in/" in socials[network] and "/company/" in url:
            socials[network] = url  # ...except a company page beats a person's profile
    return socials


def extract_socials(html: str, page_url: str = "") -> dict[str, str]:
    """{"facebook": url, "instagram": url, ...} - share buttons and post links are ignored."""
    return _socials_from(_parse(html), page_url)


# ---------------------------------------------------------------------------
# People (owners, founders, team members)
# ---------------------------------------------------------------------------

ROLE_RE = re.compile(
    r"\b(co-?founder|founder|co-?owner|owner|proprietor|ceo|cfo|coo|cto|cmo|"
    r"chief\s+\w+(?:\s+\w+)?\s+officer|managing\s+director|director|general\s+manager|manager|"
    r"managing\s+partner|partner|principal|president|vice\s+president|vp|"
    r"chairman|chairwoman|chairperson|dentist|orthodontist|endodontist|periodontist|hygienist|"
    r"physician|surgeon|doctor|attorney|lawyer|solicitor|barrister|advocate|broker|realtor|agent|"
    r"head\s+of\s+\w+|executive|accountant|cpa|architect|therapist|chiropractor|optometrist|"
    r"veterinarian|pharmacist|consultant|coordinator|administrator|secretary|treasurer|"
    r"dds|dmd|md|esq)\b",
    re.I,
)
# Honorifics in front of a name. Dr./Prof. are kept as the title when nothing better is found.
_PREFIX_RE = re.compile(r"^(?:meet\s+)?(dr|dra|prof|mr|mrs|ms|miss|engr)\.?\s+", re.I)
_DOCTOR_PREFIXES = {"dr", "dra", "prof"}

# Letters after a name ("Jane Smith, DDS"). Written in lowercase without dots.
# These are always credentials, whatever their case ("Jr.", "Ph.D.", "Esq.")...
_SUFFIXES = {"phd", "md", "dds", "dmd", "msd", "esq", "cpa", "jr", "sr", "ii", "iii", "iv", "mba",
             "rn", "np", "pa-c", "dvm", "facs", "llb", "llm", "rdh", "mph", "fagd", "magd", "bds",
             "mds", "jd", "dpm", "fics", "ficoi", "aprn", "fnp", "crna", "cfp", "cfa", "lmt", "dpt"}
# ...and these only when written in capitals ("MS", "DO"), because "Do" or "Ma" can be names.
_UPPERCASE_SUFFIXES = {"ms", "ma", "ba", "bs", "bsc", "msc", "od", "do", "pa", "pt", "pe", "ra", "dc",
                       "rda", "cda", "aia"}
# Generation suffixes: part of the name, never a job title.
_GENERATION_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

_PARTICLES = {"van", "von", "de", "der", "den", "da", "di", "del", "la", "le", "bin", "bint", "al",
              "el", "ul", "ud", "du", "dos"}
# Words that show a phrase is NOT a person's name (compared lowercase, without accents).
# Includes the service / category words that sit next to job titles in website menus
# ("Oral Surgery" next to "Emergency Dentist").
_NOT_NAME_WORDS = set("""
meet our the team about us contact home services service read more learn call today book now get
in touch privacy policy terms welcome company office offices location locations hours free quote
news blog gallery view profile click here menu shop cart login sign up info email phone address
street road avenue suite floor center centre group inc llc ltd limited dental dentistry clinic law
firm real estate insurance associates management solutions consulting care health medical family
best new all rights reserved copyright follow facebook instagram twitter linkedin youtube tiktok
monday tuesday wednesday thursday friday saturday sunday january february march april may june july
august september october november december and or of for with by to at on from my your we you is
are open closed hospital school university college academy restaurant cafe hotel store market agency
studio salon spa bank city state county north south east west central international global private
public pvt co corp corporation trust foundation society association committee department board staff
people leadership members member testimonials reviews review faq appointment appointments schedule
request submit send message subscribe newsletter search back top next previous page site website web
design designed powered development marketing digital media online our story mission vision values
why choose work careers jobs apply portfolio projects project products product pricing plans
support help account dashboard sale offer offers special specials gift card cards order delivery
pickup catering lunch dinner breakfast hair nails beauty fitness gym yoga pilates auto repair
plumbing electric electrical roofing cleaning lawn pest legal tax financial wealth advisors
surgery surgical oral orthodontics endodontics periodontics prosthodontics implant implants
emergency emergencies cosmetic teeth tooth whitening extraction extractions root canal canals crown
crowns bridge veneer veneers invisalign braces aligner aligners denture dentures sedation periodontal
gum gums cleanings exam exams checkup checkups filling fillings bonding sealants fluoride hygiene
preventive preventative restorative general pediatric pediatrics kids children smile smiles makeover
laser sleep apnea botox tmj financing finance options option payment payments patient patients forms
form directions map hours technology tour virtual results before after realty properties property
homes listings rentals mortgage loans injury accident divorce criminal defense immigration planning
bankruptcy personal see find started english espanol spanish language clinics centers groups
tu su sus nuestro nuestra nuestros nuestras horas horario laborales servicios contacto contactanos
equipo inicio cita citas llame llamar ahora sobre nosotros odontologia odontologa odontologo
ortodoncia implantes limpieza blanqueamiento clinica centro familia familiar estetica dientes
sonrisa dentista doctora
blvd boulevard drive highway hwy parkway pkwy plaza ave ln ct pl rd expressway freeway turnpike
""".split())

# Text that starts like a button or heading is never a job title ("Meet Our Doctor",
# "Learn More", "Book Now", "View Profile", "Find a dentist near you").
_CTA_RE = re.compile(
    r"^(meet|learn|read|book|view|see|find|schedule|call|contact|request|get|click|visit|discover|"
    r"explore|choose|ask|text|email|watch|start|join|why|how|what|who|where|when|welcome|hire|talk|"
    r"chat|speak|make|become|apply|search|browse|check|follow|share|download|claim|save|try|go|"
    r"back|next|more)\b", re.I)
# "Our Founder" / "Your Dentist" -> "Founder" / "Dentist"
_TITLE_POSSESSIVE_RE = re.compile(r"^(?:our|your|the|my)\s+", re.I)
# Words that show the text is a sentence (often a review), not a job title.
_SENTENCE_WORDS = {
    "i", "we", "you", "he", "she", "they", "it", "is", "are", "was", "were", "am", "be", "been", "has",
    "have", "had", "will", "would", "can", "could", "should", "does", "did", "highly", "recommend",
    "recommended", "very", "super", "so", "love", "loved", "thank", "thanks", "near", "here", "now",
    "today", "best", "top", "great", "amazing", "excellent", "good", "nice", "awesome", "wonderful",
}
# "MSDr. Kalb" or "Dr. Kalb" inside a title: text of another person glued on.
_GLUED_DOCTOR_RE = re.compile(r"[A-Za-z]Dr\.|\bDr\.?\s+[A-Z]")


def _plain(text: str) -> str:
    """Lowercase without accents: 'Odontóloga' -> 'odontologa'."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _is_credential(word: str) -> bool:
    """'DDS', 'Ph.D.', 'Jr.', 'MS' -> True; 'Do' (a surname) -> False."""
    core = word.strip(",.;:()").replace(".", "")
    if core.lower() in _SUFFIXES:
        return True
    return core.isupper() and core.lower() in _UPPERCASE_SUFFIXES


def _is_name_word(word: str) -> bool:
    if re.fullmatch(r"[A-Z]\.", word):  # middle initial "J."
        return True
    core = word.replace("-", "").replace("'", "").replace("’", "")
    return (
        len(word) >= 2
        and word[0].isupper()
        and core.isalpha()
        and any(c.islower() for c in word)
        and _plain(word) not in _NOT_NAME_WORDS
        and not any(_plain(part) in _NOT_NAME_WORDS for part in word.split("-") if part)
    )


def looks_like_name(text: str, max_words: int = 4) -> bool:
    """True for 'Jane Smith', 'Ayesha Al-Khan', 'Jan van der Berg'; False for headings
    and menu items such as 'Oral Surgery' or 'Teeth Whitening'."""
    words = text.split()
    if not 2 <= len(words) <= max_words or len(text) > 40 or ROLE_RE.search(text):
        return False
    if words[0].lower() in _PARTICLES or words[-1].lower() in _PARTICLES:
        return False
    return all(_is_name_word(w) or w.lower() in _PARTICLES for w in words) and not re.fullmatch(
        r"[A-Z]\.", words[-1])


def _role_text(text: str) -> str | None:
    """Return the text if it reads like a job title ('Founder & CEO'), else None.
    Buttons ('Meet Our Doctor'), review sentences and glued-together text are rejected."""
    text = _TITLE_POSSESSIVE_RE.sub("", text.strip(" -–—|,:()"))
    if not text or len(text) > 60 or len(text.split()) > 8 or "@" in text:
        return None
    if re.search(r"[?!]|\d{3,}", text) or not ROLE_RE.search(text):
        return None
    if _CTA_RE.match(text) or _GLUED_DOCTOR_RE.search(text):
        return None
    words = {w.lower() for w in re.findall(r"[^\W\d_]+", text)}
    if words & _SENTENCE_WORDS or (text.endswith(".") and len(text.split()) >= 3):
        return None
    if looks_like_name(text):
        return None
    return text


def _leading_credentials(text: str) -> tuple[list[str], str]:
    """'DMD, MS' -> (['DMD', 'MS'], ''); 'DDS - Owner' -> (['DDS'], 'Owner');
    'Owner' -> ([], 'Owner')."""
    credentials: list[str] = []
    end = 0
    for match in re.finditer(r"[^\s,/&|]+", text):
        if not _is_credential(match.group()):
            break
        credentials.append(match.group().strip(",;:()"))
        end = match.end()
    return credentials, text[end:].strip(" ,/&|-–—")


def _credentials_title(credentials: list[str]) -> str | None:
    """['DMD', 'MS'] -> 'DMD, MS' (used as a title when nothing better is found);
    ['Jr.'] -> None, because 'Jr.' is part of the name, not a qualification."""
    useful = [c.strip(",;:()") for c in credentials
              if c.strip(",;:()").replace(".", "").lower() not in _GENERATION_SUFFIXES]
    return ", ".join(useful) or None


def _split_name_title(text: str) -> tuple[str | None, str | None, str | None]:
    """Split one line of text into (name, title, fallback title).

    'Jane Smith - Owner'      -> ('Jane Smith', 'Owner', None)
    'Dr. Jane Smith'          -> ('Jane Smith', None, 'Dr.')
    'Jane Smith, DMD, MS'     -> ('Jane Smith', None, 'DMD, MS')
    'Lahore, Pakistan'        -> (None, None, None)
    The fallback (Dr. or the credentials) is used only when no real job title is found
    nearby on the page."""
    text = text.strip()
    prefix = _PREFIX_RE.match(text)
    fallback = "Dr." if prefix and prefix.group(1).lower() in _DOCTOR_PREFIXES else None
    if prefix:
        text = text[prefix.end():]
    parts = re.split(r"\s+[-–—|•]\s+|\s*,\s*|\s*\(\s*|:\s+", text, maxsplit=1)
    name_part = parts[0].strip()
    title = None
    if len(parts) == 2:
        credentials, rest = _leading_credentials(parts[1].strip(" )"))
        fallback = fallback or _credentials_title(credentials)
        if rest:
            title = _role_text(rest)
            if not title and not credentials:
                return None, None, None  # "Lahore, Pakistan" etc.
            # With credentials first ("DMD, MSDr. Kalb") the person is real; the rest is
            # glued-on text that we just ignore.
    # Drop trailing credentials without a comma: "Jane Smith DDS"
    words = name_part.split()
    trailing: list[str] = []
    while words and _is_credential(words[-1]):
        trailing.insert(0, words.pop())
    fallback = fallback or _credentials_title(trailing)
    name = " ".join(words)
    if not looks_like_name(name):
        return None, None, None
    return name, title, fallback


def _common_depth(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    depth = 0
    for x, y in zip(a, b):
        if x != y:
            break
        depth += 1
    return depth


def _people_from_jsonld(parser: _PageParser, page_url: str) -> list[PersonFound]:
    people: list[PersonFound] = []
    skip_keys = {"author", "creator", "editor", "publisher", "reviewedBy", "review", "contributor",
                 "translator", "accountablePerson", "copyrightHolder"}
    member_keys = {"founder": "Founder", "founders": "Founder", "employee": None, "employees": None,
                   "member": None, "members": None, "owner": "Owner", "owners": "Owner"}

    def as_text(value) -> str | None:
        if isinstance(value, list):
            value = value[0] if value else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def add_person(node: dict, default_title: str | None) -> None:
        raw_name = as_text(node.get("name"))
        if not raw_name:
            given, family = as_text(node.get("givenName")), as_text(node.get("familyName"))
            raw_name = f"{given} {family}" if given and family else None
        if not raw_name or len(raw_name) > 80:
            return
        # "Dr. David J. Weiner, DMD" -> "David J. Weiner"
        name, split_title, fallback = _split_name_title(" ".join(raw_name.split()))
        if not name:
            return
        same_as = node.get("sameAs") or []
        same_as = [same_as] if isinstance(same_as, str) else same_as
        linkedin = next((u for u in same_as if isinstance(u, str) and "linkedin.com/in/" in u), None)
        email = clean_email((as_text(node.get("email")) or "").replace("mailto:", ""))
        phone = clean_phone(as_text(node.get("telephone")))
        job_title = as_text(node.get("jobTitle"))
        title = (job_title[:100] if job_title else None) or split_title or default_title or fallback
        people.append(PersonFound(name, title, email, phone, linkedin, page_url or None))

    def walk(node, key: str | None, depth: int = 0) -> None:
        if depth > 30 or len(people) >= MAX_PEOPLE_PER_PAGE:
            return  # deeper data is never about the business's people
        if isinstance(node, list):
            for item in node:
                walk(item, key, depth + 1)
            return
        if isinstance(node, str) and key in member_keys:
            name, _title, _fallback = _split_name_title(node.strip())
            if name:
                people.append(PersonFound(name, member_keys[key], source_url=page_url or None))
            return
        if not isinstance(node, dict):
            return
        types = node.get("@type") or []
        types = [types] if isinstance(types, str) else types
        if "Person" in types and key not in skip_keys:
            # A bare top-level Person is usually a blog author; require a job title then.
            if key in member_keys or node.get("jobTitle"):
                add_person(node, member_keys.get(key or ""))
        for k, v in node.items():
            if k not in skip_keys and isinstance(v, (dict, list, str)):
                walk(v, k, depth + 1)

    for block in parser.jsonld:
        try:
            data = json.loads(block.strip(), strict=False)
        except (ValueError, TypeError, RecursionError):  # broken or absurdly nested JSON
            continue
        walk(data, None)
    return people


def _people_from_text(parser: _PageParser, page_url: str) -> list[PersonFound]:
    """Find 'name next to a job title' patterns on team / about pages."""
    chunks = parser.chunks
    found: list[tuple[PersonFound, _Chunk]] = []
    for i, chunk in enumerate(chunks):
        if len(found) >= MAX_PEOPLE_PER_PAGE:
            break
        if len(chunk.text) > 70 or not chunk.text[0].isupper():  # names start with a capital
            continue
        name, title, fallback = _split_name_title(chunk.text)
        if not name:
            continue
        if not title and not chunk.in_menu:
            # Look at the text right before and after; prefer the one in the same card.
            # Menu items are skipped: two links side by side in a menu ("Oral Surgery",
            # "Emergency Dentist") are not a name and its job title.
            best_depth = -1
            for j in (i + 1, i - 1):
                if not 0 <= j < len(chunks):
                    continue
                other = chunks[j]
                if other.in_menu or (chunk.in_link and other.in_link):
                    continue
                role = _role_text(other.text)
                if role and _split_name_title(other.text)[0]:
                    role = None  # that line is another person ("Jane Doe, CEO")
                depth = _common_depth(chunk.cards, other.cards)
                if role and depth > best_depth:
                    title, best_depth = role, depth
        title = title or fallback
        if title:
            found.append((PersonFound(name, title, source_url=page_url or None), chunk))
    if not found:
        return []

    # Attach a personal email / LinkedIn profile from the same "card" (a container that
    # holds this person and nobody else). Only the 3 innermost containers are checked.
    # The links and texts of those cards are collected in one pass, so this stays fast
    # even on a huge page.
    people_in_card: dict[int, int] = {}
    for _, chunk in found:
        for card in chunk.cards:
            people_in_card[card] = people_in_card.get(card, 0) + 1
    wanted = {card for _, chunk in found for card in chunk.cards[-3:] if people_in_card[card] == 1}
    links_in_card: dict[int, list[_Link]] = {}
    texts_in_card: dict[int, list[str]] = {}
    if wanted:
        for link in parser.links:
            for card in wanted.intersection(link.cards):
                links_in_card.setdefault(card, []).append(link)
        for c in chunks:
            if "@" in c.text:
                for card in wanted.intersection(c.cards):
                    texts_in_card.setdefault(card, []).append(c.text)

    for person, chunk in found:
        for card in reversed(chunk.cards[-3:]):
            if people_in_card[card] > 1:
                break
            for link in links_in_card.get(card, []):
                addr = clean_email(_mailto_address(link.href) or "")
                if addr and not is_generic_email(addr) and not person.email:
                    person.email = addr
                if "linkedin.com/in/" in link.href and not person.linkedin_url:
                    profile = social_profile(link.href)
                    person.linkedin_url = profile[1] if profile else None
            if not person.email:
                for text in texts_in_card.get(card, []):
                    for cand in EMAIL_RE.findall(text):
                        addr = clean_email(cand)
                        if addr and not is_generic_email(addr):
                            person.email = addr
                            break
                    if person.email:
                        break
            if person.email or person.linkedin_url:
                break
    return [p for p, _ in found]


def _name_words(full_name: str) -> list[str]:
    """'Dr. David J. Weiner, DMD' -> ['david', 'j', 'weiner']; 'Raúl García' -> ['raul', 'garcia'];
    'David Weiner Jr.' -> ['david', 'weiner', 'jr'].
    Lowercase words without titles, credentials or accents. Middle initials and Jr./Sr./II
    are kept: they tell a father and son (or two different Johns) apart."""
    text = _PREFIX_RE.sub("", full_name.strip())
    words = []
    for w in re.split(r"[\s,\-]+", text):
        plain = _plain(w).strip(".'’").replace(".", "")
        if not plain:
            continue
        if plain in _GENERATION_SUFFIXES or not _is_credential(w):
            words.append(plain)
    return words


def _split_name_words(words: list[str]) -> tuple[list[str], set[str], set[str]]:
    """(name words, middle initials, generation suffixes) of a _name_words() list."""
    core = [w for w in words if len(w) > 1 and w not in _GENERATION_SUFFIXES]
    initials = {w for w in words if len(w) == 1}
    generations = {w for w in words if w in _GENERATION_SUFFIXES}
    return core, initials, generations


def same_person(name_a: str, name_b: str) -> bool:
    """True when two names are the same person written differently:
    'David Weiner' / 'David J. Weiner' / 'Dr. David Weiner, DMD' (same first + last name),
    'Raul Garcia' / 'Raúl García' (accents), 'Mario Garcia' / 'Mario Garcia Caceres'
    (one name contains the other). Not the same: 'David Weiner Sr.' / 'David Weiner Jr.',
    'John A. Smith' / 'John B. Smith'."""
    return _same_words(_name_words(name_a), _name_words(name_b))


def _same_words(a_words: list[str], b_words: list[str]) -> bool:
    a, a_initials, a_generations = _split_name_words(a_words)
    b, b_initials, b_generations = _split_name_words(b_words)
    # Both have a middle initial / Jr.-Sr. and they differ: two different people.
    if (a_initials and b_initials and a_initials != b_initials) or (
            a_generations and b_generations and a_generations != b_generations):
        return False
    if not a or not b or a[0] != b[0]:
        return False
    if a[-1] == b[-1]:
        return True
    smaller, bigger = sorted((set(a), set(b)), key=len)
    return len(smaller) >= 2 and smaller <= bigger


def _weak_title(title: str | None) -> bool:
    """True for no title, the bare 'Dr.' fallback or credentials only ('DMD, MS')."""
    if not title or title == "Dr.":
        return True
    return all(_is_credential(w) for w in re.split(r"[\s,/&|]+", title) if w)


def merge_people(people: list[PersonFound]) -> list[PersonFound]:
    """Dedupe people (see same_person), keeping the most complete record: the fuller name
    ('David J. Weiner' beats 'David Weiner'), a real job title over 'Dr.', and any
    email / phone / LinkedIn found on either record. Order of first appearance is kept."""
    merged: list[PersonFound] = []
    merged_words: list[list[str]] = []  # _name_words() of each merged person
    for person in people:
        words = _name_words(person.full_name)
        index = next((i for i, other in enumerate(merged_words) if _same_words(other, words)), None)
        if index is None:
            merged.append(PersonFound(**vars(person)))
            merged_words.append(words)
            continue
        existing = merged[index]
        if len(person.full_name.split()) > len(existing.full_name.split()):
            existing.full_name = person.full_name
            merged_words[index] = words
        if person.title and _weak_title(existing.title) and not _weak_title(person.title):
            existing.title = person.title
        for attr in ("title", "email", "phone", "linkedin_url", "source_url"):
            if not getattr(existing, attr) and getattr(person, attr):
                setattr(existing, attr, getattr(person, attr))
    return merged


def _people_from(parser: _PageParser, page_url: str) -> list[PersonFound]:
    people = merge_people(_people_from_jsonld(parser, page_url) + _people_from_text(parser, page_url))
    return people[:MAX_PEOPLE_PER_PAGE]


def extract_people(html: str, page_url: str = "") -> list[PersonFound]:
    """People with a role (owner, founder, dentist...). Precision over recall: a name is
    only returned when it sits next to a job title or comes from schema.org data."""
    return _people_from(_parse(html), page_url)


# ---------------------------------------------------------------------------
# Website signals (useful for selling web design / marketing services)
# ---------------------------------------------------------------------------

# (platform, substrings of the lowercase HTML), checked in this order.
_PLATFORM_FINGERPRINTS = [
    ("WordPress", ["wp-content/", "wp-includes/", "wp-json"]),
    ("Shopify", ["cdn.shopify.com", "shopify.theme", "myshopify.com"]),
    ("Wix", ["static.wixstatic.com", "wix.com website builder", "_wixcssimports", "wix-warmup-data"]),
    ("Squarespace", ["static1.squarespace.com", "squarespace.com", "squarespace-cdn"]),
    ("Webflow", ["data-wf-page", "data-wf-site", "assets.website-files.com", "webflow.com"]),
    ("GoDaddy", ["img1.wsimg.com", "go daddy website builder", "godaddy website builder"]),
    ("Joomla", ["/media/jui/", "/components/com_", "joomla!"]),
    ("Drupal", ["drupal-settings-json", "/sites/default/files/", "drupal.settings"]),
]


_GENERATOR_NAMES = [("wordpress", "WordPress"), ("shopify", "Shopify"), ("wix", "Wix"),
                    ("squarespace", "Squarespace"), ("webflow", "Webflow"), ("go daddy", "GoDaddy"),
                    ("godaddy", "GoDaddy"), ("joomla", "Joomla"), ("drupal", "Drupal")]


def _detect_platform(html_lower: str, generator: str) -> str | None:
    # <meta name="generator" content="WordPress 6.5"> is the most reliable hint
    generator = generator.lower()
    for needle, platform in _GENERATOR_NAMES:
        if needle in generator:
            return platform
    for platform, needles in _PLATFORM_FINGERPRINTS:
        if any(n in html_lower for n in needles):
            return platform
    return None


def _copyright_year(text: str) -> int | None:
    max_year = datetime.now(timezone.utc).year + 1
    years: list[int] = []
    for match in re.finditer(r"(?:©|\(c\)|copyright)(.{0,60})", text, flags=re.I | re.S):
        for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", match.group(1)):
            if 1995 <= int(y) <= max_year:
                years.append(int(y))
    return max(years) if years else None


def _signals_from(parser: _PageParser, html: str) -> dict:
    title = " ".join("".join(parser.title_parts).split()) or None
    description = parser.meta.get("description") or parser.meta.get("og:description")
    description = " ".join(description.split()) if description else None
    return {
        "platform": _detect_platform((html or "").lower(), parser.meta.get("generator", "")),
        "copyright_year": _copyright_year(parser.text),
        "has_contact_form": parser.has_contact_form,
        "title": title[:300] if title else None,
        "description": description[:500] if description else None,
    }


def detect_signals(html: str, page_url: str = "") -> dict:
    """{"platform", "copyright_year", "has_contact_form", "title", "description"}"""
    return _signals_from(_parse(html), html)


# ---------------------------------------------------------------------------
# Which pages to crawl next
# ---------------------------------------------------------------------------

# (score, keywords looked for in the URL path and the link text)
_LINK_KEYWORDS = [
    (100, ["contact", "kontakt", "contacto", "contato", "contatti", "contactez", "get-in-touch",
           "reach-us", "find-us"]),
    (90, ["team", "staff", "people", "leadership", "management", "equipe", "equipo",
          "mitarbeiter", "board-of-directors"]),
    (85, ["doctors", "dentists", "physicians", "providers", "attorneys", "lawyers", "agents",
          "realtors", "brokers", "practitioners", "our-partners"]),
    (70, ["about", "who-we-are", "uber-uns", "ueber-uns", "a-propos", "quienes-somos",
          "chi-siamo", "sobre", "over-ons", "our-story"]),
    (60, ["impressum", "imprint", "legal-notice", "mentions-legales", "aviso-legal"]),
    (10, ["privacy", "datenschutz"]),
]
# Paths that match a keyword by accident (blog posts, shop pages, files...).
_SKIP_PATH_RE = re.compile(
    r"/(blog|news|tag|tags|category|categories|product|products|shop|cart|checkout|author|"
    r"feed|wp-content|wp-admin|wp-json|event|events|(19|20)\d\d)(/|$)", re.I)
_SKIP_EXT_RE = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3|css|js|xml|json|txt)$", re.I)


# One regex per score, for the link text ("get-in-touch" also matches "Get in touch").
_LINK_TEXT_RES = [
    (score, re.compile(r"\b(?:" + "|".join(re.escape(w.replace("-", " ")) for w in words) + r")\b"))
    for score, words in _LINK_KEYWORDS
]


def _link_score(path: str, text: str) -> int:
    path = path.lower()
    text = text.lower()[:200]
    best = 0
    for (score, words), (_score, text_re) in zip(_LINK_KEYWORDS, _LINK_TEXT_RES):
        if score > best and (any(word in path for word in words) or text_re.search(text)):
            best = score
    if best and path.count("/") > 3:
        best -= 10  # deep pages are less likely to be the main contact page
    return best


def url_key(url: str) -> str:
    """Compare URLs ignoring scheme, 'www.', a trailing slash and the #fragment."""
    url = urldefrag(url)[0]
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return f"{host}{path}?{parsed.query}" if parsed.query else f"{host}{path}"


def _candidate_links(parser: _PageParser, base_url: str) -> list[str]:
    base_domain = registered_domain(_host_of(base_url))
    base_key = url_key(base_url)
    scored: dict[str, tuple[int, int, str]] = {}  # key -> (score, order, url)
    for order, link in enumerate(parser.links):
        href = link.href
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urldefrag(_resolve(href, base_url))[0]
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if registered_domain(parsed.hostname) != base_domain:
            continue
        if _SKIP_PATH_RE.search(parsed.path) or _SKIP_EXT_RE.search(parsed.path):
            continue
        key = url_key(url)
        if key == base_key:
            continue
        score = _link_score(parsed.path, link.text)
        if score <= 0:
            continue
        if key not in scored or score > scored[key][0]:
            keep_order = scored[key][1] if key in scored else order
            scored[key] = (score, keep_order, url)
    ranked = sorted(scored.values(), key=lambda item: (-item[0], item[1]))
    return [url for _, _, url in ranked]


def find_candidate_links(html: str, base_url: str) -> list[str]:
    """Same-website links likely to list contacts (contact > team > about > impressum > privacy)."""
    return _candidate_links(_parse(html), base_url)


# ---------------------------------------------------------------------------
# Everything at once
# ---------------------------------------------------------------------------

def analyze_page(html: str, page_url: str) -> PageInfo:
    """Parse the page once and run every extractor on it."""
    parser = _parse(html)
    return PageInfo(
        emails=_emails_from(parser, html),
        phones=_phones_from(parser),
        socials=_socials_from(parser, page_url),
        people=_people_from(parser, page_url),
        signals=_signals_from(parser, html),
        links=_candidate_links(parser, page_url),
    )
