"""Pure functions that pull contact details out of one HTML page.

Nothing here touches the network: every function takes the page HTML (and the
page URL, used to resolve relative links) and returns plain Python data.
The crawler (crawler.py) fetches pages and merges the results.

Main entry points:
    analyze_page(html, page_url)        -> PageInfo (everything below in one parse)
    extract_emails / extract_phones / extract_socials / extract_people
    detect_signals(html, page_url)      -> dict (platform, copyright year, ...)
    detect_size_signals(html, page_url) -> dict (stated headcount, founded year, branches, careers link)
    detect_intent_signals(html, page_url) -> dict (analytics, ad pixels, booking, payment, chat, jobs...)
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
import math
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
# Script / iframe / form addresses and inline script code kept per page (for the
# "intent" signals: analytics tags, booking widgets, chat widgets...).
_MAX_SRCS = 500
_INLINE_SCRIPT_LIMIT = 300_000  # characters of inline script code, all scripts together
# <script type="..."> values that hold JavaScript ("text/plain" is what cookie-consent tools
# use for tags that wait for consent). JSON data blocks and templates are skipped: they can
# contain any text (blog posts, CMS content).
_JS_TYPES = {"", "text/javascript", "application/javascript", "module", "text/partytown",
             "text/plain", "application/x-javascript", "text/ecmascript", "text/x-javascript"}
# <script type="..."> values (or parts of them) that hold data or templates, not code.
_DATA_SCRIPT_TYPE_PARTS = ("json", "template", "tmpl", "html", "importmap", "speculationrules",
                           "shader", "handlebars", "mustache", "markdown", "text/x-")
# Speed plugins (WP Rocket, LiteSpeed, Perfmatters, Cloudflare Rocket Loader...) and consent
# tools rename a script's type ("rocketlazyloadscript", "litespeed/javascript",
# "4d1c...-text/javascript") and move its address to one of these attributes, so the browser
# only runs it later. It is still the site's code, so we read it like any other script.
_SCRIPT_SRC_ATTRS = ("src", "data-src", "data-rocket-src", "data-litespeed-src", "data-cmplz-src",
                     "data-lazy-src", "data-pagespeed-orig-src")
# Element ids that JavaScript apps (React, Vue, Next.js, Gatsby, Svelte...) render the page into.
_APP_MOUNT_IDS = {"root", "app", "__next", "___gatsby", "__nuxt", "svelte", "q-app", "main-app"}


# "add-to-cart", "add_to_cart", "addtocart", Shopify's "/cart/add" form
_ADD_TO_CART_RE = re.compile(r"add[-_ ]?to[-_ ]?cart|/cart/add\b")


def _script_type(attrs: dict[str, str]) -> str:
    """'js' (code that runs now), 'delayed' (code a speed plugin runs later, see
    _SCRIPT_SRC_ATTRS) or 'data' (JSON, templates: any text, never read as code)."""
    kind = attrs.get("type", "").strip().lower()
    if kind in _JS_TYPES:
        return "js"
    if "javascript" in kind or "ecmascript" in kind:
        return "delayed"  # Cloudflare Rocket Loader "<hash>-text/javascript", "litespeed/javascript"
    if any(part in kind for part in _DATA_SCRIPT_TYPE_PARTS):
        return "data"
    return "delayed"  # "rocketlazyloadscript", "pmdelayedscript", "text/rocketscript"...


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
        # For the intent signals (see _intent_signals_from)
        self.script_srcs: list[str] = []
        self.iframe_srcs: list[str] = []
        self.form_actions: list[str] = []
        self.itemtypes: list[str] = []  # microdata itemtype="https://schema.org/Dentist"
        self.inline_script_parts: list[str] = []
        self._inline_script_size = 0
        self._in_inline_script = False
        self.has_add_to_cart = False  # "add to cart" buttons / forms
        self.scripts_delayed = False  # a speed plugin renamed the scripts (see _script_type)
        self.has_mobile_alternate = False  # <link rel="alternate" media="...max-width..."> (m. site)
        # How much text a visitor sees, and whether a JavaScript app's mount element
        # (<div id="root"></div>) was left empty: then the page is built in the browser and
        # its HTML cannot tell us what the site lacks.
        self.visible_chars = 0
        self.empty_app_mount = False
        self._mounts_open: dict[int, int] = {}  # element id -> visible_chars when it opened

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

    def _close_mount(self, element_id: int) -> None:
        if element_id in self._mounts_open:
            if self._mounts_open.pop(element_id) == self.visible_chars:
                self.empty_app_mount = True

    def _pop_to(self, idx: int) -> None:
        """Close stack[idx] and everything opened after it."""
        for tag, _id, _cards, menu in self.stack[idx:]:
            self._close_mount(_id)
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
        elif tag == "script":
            kind = _script_type(a)
            src = next((a[k].strip() for k in _SCRIPT_SRC_ATTRS if a.get(k, "").strip()), "")
            if kind == "delayed" or (src and src != a.get("src", "").strip()):
                self.scripts_delayed = True
            if kind != "data" and src:
                if len(self.script_srcs) < _MAX_SRCS:
                    self.script_srcs.append(src[:2000])
            elif kind != "data":
                self._in_inline_script = True
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "alternate" in rel and "max-width" in a.get("media", "").lower() and a.get("href", "").strip():
                self.has_mobile_alternate = True
        elif tag == "iframe":
            src = (a.get("src") or a.get("data-src") or "").strip()
            if src and len(self.iframe_srcs) < _MAX_SRCS:
                self.iframe_srcs.append(src[:2000])
        if tag == "form" and a.get("action", "").strip() and len(self.form_actions) < _MAX_SRCS:
            self.form_actions.append(a["action"].strip()[:2000])
        if "itemtype" in a and len(self.itemtypes) < _MAX_SRCS:
            self.itemtypes.append(a["itemtype"][:500])
        if tag in ("a", "button", "input", "form") and not self.has_add_to_cart:
            marks = " ".join(a.get(k, "")[:300] for k in ("class", "name", "id", "href", "action",
                                                          "data-action")).lower()
            if _ADD_TO_CART_RE.search(marks):
                self.has_add_to_cart = True

        if tag in _VOID_TAGS:
            return
        # A new <li>/<p> implicitly closes an unclosed sibling of the same kind.
        if tag in ("li", "p") and self.stack and self.stack[-1][0] == tag:
            self._pop_to(len(self.stack) - 1)
        if len(self.stack) < _MAX_DEPTH:
            self._push(tag, _is_menu(tag, a))
            if a.get("id", "").strip().lower() in _APP_MOUNT_IDS and len(self._mounts_open) < 10:
                self._mounts_open[self._next_id] = self.visible_chars

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
        if tag == "script" and self._in_inline_script:
            self._in_inline_script = False
            self.inline_script_parts.append("\n")
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
        if self._in_inline_script:
            room = _INLINE_SCRIPT_LIMIT - self._inline_script_size
            if room > 0:
                self.inline_script_parts.append(data[:room])
                self._inline_script_size += min(len(data), room)
            return
        if self.stack and self.stack[-1][0] == "title":
            self.title_parts.append(data)
            return
        if self._hidden():
            return
        if self.visible_chars < 100_000:
            self.visible_chars += len(" ".join(data.split()))
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
            for element_id in list(self._mounts_open):  # mount elements never closed
                self._close_mount(element_id)

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
# Business size signals (headcount, founded year, number of branches, careers link)
# ---------------------------------------------------------------------------
#
# Precision over recall: a wrong number is worse than none. Every pattern is anchored on
# a keyword ("team of", "employ", "founded", "locations"...) and uses bounded repeats,
# so it runs in linear time. We match one sentence at a time, after hiding numbers that
# are obviously something else (phones, prices, "24/7", ratings, "Suite 200"...).

# Work limits per page: more text than this is a directory or junk.
_SIZE_TEXT_LIMIT = 200_000  # characters of visible text
_SIZE_MAX_SENTENCES = 4000
_SENTENCE_LIMIT = 400  # characters per sentence
_SNIPPET_LIMIT = 200  # characters of the sentence we keep to show the user
_JSONLD_LIMIT = 500_000  # characters per JSON-LD block
_JSONLD_MAX_NODES = 5000
MAX_EMPLOYEES = 100_000
MAX_LOCATIONS = 500
# Headcounts on one page that differ more than this many times: the page is unclear
# (one of them is probably about customers), so we store none.
HEADCOUNT_MAX_SPREAD = 3

# Spelled-out numbers: "twelve", "twenty-five", "two dozen" -> 12, 25, 24.
_UNITS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen"]
_TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_WORD_NUMBERS: dict[str, int] = {w: i + 1 for i, w in enumerate(_UNITS)}
_WORD_NUMBERS.update({w: i + 10 for i, w in enumerate(_TEENS)})
for _i, _ten in enumerate(_TENS):
    _WORD_NUMBERS[_ten] = (_i + 2) * 10
    for _j, _unit in enumerate(_UNITS):
        _WORD_NUMBERS[f"{_ten}-{_unit}"] = _WORD_NUMBERS[f"{_ten} {_unit}"] = (_i + 2) * 10 + _j + 1
_WORD_NUMBERS.update({"a dozen": 12, "two dozen": 24, "three dozen": 36, "half a dozen": 6,
                      "a hundred": 100, "one hundred": 100})
# Longest first, so "twenty-five" wins over "twenty" and "two dozen" over "two".
_WORDNUM = "|".join(re.escape(w).replace(r"\ ", r"\s") for w in sorted(_WORD_NUMBERS, key=len, reverse=True))

_NUM = r"(?:\d{1,3}(?:,\d{3}){1,2}|\d{1,6}|" + _WORDNUM + r")"
# A number on its own: not glued to letters, "$", "#", "/", a decimal point or more digits.
_NUM_END = r"(?![\w/:%]|[.,]\d)"
_NUMBER = r"(?<![\w,.$#/-])(?P<n>" + _NUM + r")" + _NUM_END
# "over 50", "about 40", "nearly 30": we keep the stated number.
_QUAL = (r"(?:(?:over|more\s{1,3}than|nearly|almost|about|around|approximately|approx\.?|roughly|"
         r"some|close\s{1,3}to|upwards\s{1,3}of|just\s{1,3}over|well\s{1,3}over|at\s{1,3}least)\s{1,3})?")
# "10-15", "50 to 60", "50+": the first number (the lower bound) is the one we keep.
# Atomic "(?>...)": once read, the range is never cut short to dodge a check after it
# ("team of 10-15 years" must not become "team of 10").
_RANGE = r"(?>" + _QUAL + _NUMBER + r"(?:\s{0,2}(?:-|–|—|to)\s{0,2}" + _NUM + _NUM_END + r")?\+?)"

# Numbers that are clearly something else are replaced by " § " before matching.
_TRAP_RE = re.compile(
    r"\b1[\s.\-]\d{3}[\s.\-]\d{3}[\s.\-]\d{4}\b"                    # 1-800-555-1234
    r"|\(?\b\d{3}\)?[\s.\-]{1,2}\d{3}[\s.\-]\d{4}\b"                # (555) 123-4567
    r"|\$\s?\d[\d,.]{0,12}"                                          # $99
    r"|\b\d{1,4}\s?[:/]\s?\d{1,4}\b"                                 # 24/7, 1:4, 9:00
    r"|\b\d(?:\.\d)?\s?-?\s?stars?\b"                                # 5-star, 4.9 stars
    r"|\b\d{1,6}\.\d{1,6}\b"                                         # 4.9
    r"|\b\d{1,6}\s?%"                                                # 98%
    r"|\b\d{1,4}\s?\(k\)"                                            # 401(k)
    r"|(?:\b(?:suite|ste|unit|apt|lic|license|licence)\b\.?|#)\s{0,2}#?\s{0,2}\d{1,10}(?:-?[a-z]{1,3}\b)?"
    r"|\b[a-z]{1,20}-\d{1,8}\b"                                      # COVID-19
    r"|\b\d{1,3}\s{1,3}of\s{1,3}(?:the\s{1,3})?(?:\d{1,4}|top)\b"    # 1 of 10
    r"|\btop\s{1,3}\d{1,4}\b",                                       # Top 100
    re.I,
)
# Footer lines: their years and names are never about the business size or age.
_COPYRIGHT_RE = re.compile(r"©|\bcopyright\b|all\s{1,3}rights\s{1,3}reserved|\(c\)\s{0,2}\d", re.I)
# A sentence ends at . ! or ? followed by a capital letter (so "EST. 1998" stays whole),
# but not after "Dr." / "Mrs." or an initial ("T.P. Howard's", "Walter J. Bradley").
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<![A-Z][a-z]\.)(?<!Mrs\.)(?<!\b[A-Z]\.)(?<=[.!?])\s{1,10}(?=[A-Z\"“(])")


def _number_value(text: str) -> int | None:
    """'1,200' -> 1200, 'twenty-five' -> 25, 'a dozen' -> 12."""
    text = " ".join(text.lower().split())
    if text[0].isdigit():
        return int(text.replace(",", ""))
    return _WORD_NUMBERS.get(text) or _WORD_NUMBERS.get(text.replace(" ", "-"))


def _is_year_like(raw: str, value: int) -> bool:
    """'1998' looks like a year, so it is never read as a headcount ('Since 1998 our team...')."""
    return raw.isdigit() and len(raw) == 4 and 1800 <= value <= datetime.now(timezone.utc).year + 1


def _size_sentences(parser: _PageParser) -> list[tuple[str, str]]:
    """(original sentence, sentence with trap numbers hidden) for the page's visible text.
    Copyright lines are left out."""
    result: list[tuple[str, str]] = []
    for line in parser.text[:_SIZE_TEXT_LIMIT].split("\n"):
        line = " ".join(line.split())
        if not line or (len(line) <= 300 and _COPYRIGHT_RE.search(line)):
            continue  # empty, or a short footer line such as "(c) 1999 Acme. Since 1999"
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = sentence[:_SENTENCE_LIMIT]
            if not sentence or _COPYRIGHT_RE.search(sentence):
                continue
            result.append((sentence, _TRAP_RE.sub(" § ", sentence)))
            if len(result) >= _SIZE_MAX_SENTENCES:
                return result
    return result


def _snippet(sentence: str) -> str:
    return sentence[:_SNIPPET_LIMIT]


# -- Headcount ---------------------------------------------------------------

# Nouns that count everybody who works there. "people" / "professionals" are weak:
# they also describe customers or guests, so they need "we" / "our" / "team" nearby.
_STRONG_STAFF = (r"employees?|staff\s{1,3}members|staff|team\s{1,3}members|crew\s{1,3}members|"
                 r"workers|teammates|personnel|work\s?force|associates")
_WEAK_STAFF = r"people|professionals|individuals|colleagues"
_STAFF_NOUN = r"(?P<noun>" + _STRONG_STAFF + "|" + _WEAK_STAFF + r")\b"
# Words allowed between the number and the noun ("40 full-time staff").
_STAFF_ADJ = (r"(?:(?:full[- ]time|part[- ]time|dedicated|talented|friendly|skilled|experienced|"
              r"caring|licensed|certified|hard[- ]working|highly\s{1,3}trained|trained|local|"
              r"amazing|wonderful|passionate|professional|knowledgeable|devoted|committed|"
              r"loyal|happy|great)\s{1,3}){0,2}")
# Role nouns only count together with "and staff": "30 attorneys and staff".
_ROLE_AND = (r"(?:(?:attorneys|lawyers|doctors|dentists|physicians|providers|hygienists|nurses|"
             r"technicians|stylists|engineers|designers|agents|advisors|accountants|therapists|"
             r"specialists|chefs|cooks|drivers|installers|electricians|plumbers|mechanics|managers|"
             r"partners|teachers|instructors|consultants|developers)\s{1,3}(?:and|&)\s{1,3})?")
# Words right after a number that show it counts something else.
_NOT_STAFF_AFTER = (r"(?!\s{0,3}(?:[a-z'-]{1,20}\s{1,3})?(?:years?|yrs|hours?|days?|minutes?|miles?|sq|"
                    r"reviews?|patients?|clients?|customers?|projects?|homes?|states?|counties|cities|"
                    r"locations?|offices?|volunteers?|students?|languages?|certifications?|million|"
                    r"thousand|billion|k|percent|stars?|awards?|clinics?|branches|stores)\b)")

_HEADCOUNT_RES = [
    # "team of 25 professionals", "staff of twelve", "a work force of 50-60 people"
    ("of", re.compile(r"\b(?:team|staff|crew|work\s?force)\s{1,3}of\s{1,3}" + _RANGE + _NOT_STAFF_AFTER, re.I)),
    # "employs approximately 60 people", "employ over 800 Wisconsinites"
    ("employ", re.compile(
        r"(?i:\bemploy(?:s|ing|ed)?\s{1,3}(?:a\s{1,3}(?:staff|team)\s{1,3}of\s{1,3})?" + _RANGE
        + r"\s{1,3}" + _STAFF_ADJ + r")(?:(?i:" + _STAFF_NOUN + r")|[A-Z][a-z]{1,20}(?:ians|ers|ites|ans)\b)")),
    # "over 50 employees", "9 friendly team members", "30 attorneys and staff"
    ("noun", re.compile(_RANGE + r"\s{1,3}" + _STAFF_ADJ + _ROLE_AND + _STAFF_NOUN, re.I)),
    # "35-person team", "a 12-member staff"
    ("hyphen", re.compile(
        r"(?<![\w,.$#/-])(?P<n>" + _NUM + r")-(?:person|member|employee|people|man|woman)\s{1,3}"
        r"(?:team|staff|firm|company|agency|crew|office|shop|practice|operation)\b", re.I)),
    # "Number of employees: 45", "Company size: 11-50 employees"
    ("label", re.compile(
        r"\b(?:number\s{1,3}of\s{1,3}employees|employees|company\s{1,3}size|team\s{1,3}size|"
        r"staff\s{1,3}size|headcount)\s{0,2}[:\-–]\s{0,3}" + _RANGE, re.I)),
    # "Our team has grown to 40 members"
    ("grown", re.compile(
        r"\b(?:team|staff)\s{1,3}(?:[a-z']{1,15}\s{1,3}){0,3}?grown\s{1,3}to\s{1,3}" + _RANGE
        + r"\s{1,3}" + _STAFF_ADJ + r"(?:members|people|employees|staff)\b", re.I)),
]
# Just before "40 people": the people are guests, customers or job openings.
_NOT_STAFF_BEFORE_RE = re.compile(
    r"\b(?:seats?|seating|up\s{1,3}to|party|parties|guests|attended|served|serves?|serving|"
    r"treated|treats?|helped|helps?|trusted|trained|taught|hosted|fed|reached|supported|insured|"
    r"hiring|hire|need|needs|seeking|looking|recruiting|accommodates?|capacity|holds?|table|"
    r"sees|saw|join)\b", re.I)
_NOT_STAFF_AFTER_RE = re.compile(
    r"^\s{0,3}(?:(?:attended|attend|came|visited|joined|enjoyed|participated|registered|signed|showed|"
    r"turned|a\s{1,3}(?:day|week|month|year)|per|each|every|daily|weekly|annually|"
    r"or\s{1,3}(?:more|less|fewer|under|over)|and\s{1,3}(?:up|under|over|above|below))\b|\?)", re.I)
# Within 40 characters before "50 employees": the number is the size of the business's
# customers, not its own team ("payroll for companies with 5 to 500 employees", "plans for
# under 50 employees", "coverage for your 25 employees", "we can host 150 people").
# B2B firms (payroll, insurance, HR, IT, accounting) and venues write this all the time.
_CLIENT_SIZE_BEFORE_RE = re.compile(
    r"\b(?:your|companies|businesses|organizations|organisations|firms|employers|clients|customers|"
    r"startups|offices|practices|groups\s{1,3}of|under|fewer\s{1,3}than|less\s{1,3}than|"
    r"no\s{1,3}more\s{1,3}than|host|hosts|hosting|accommodate|plans?|between|cover|covers|covering|"
    r"sized?)\b"
    # "for" right before the number: "Group health plans for 2-50 employees"
    r"|\bfor\s{1,3}$", re.I)
# "people" only counts with one of these words before the number.
_WE_RE = re.compile(r"\b(?:we|we're|we’re|we've|we’ve|our|us|company|firm|business|team|staff|practice|"
                    r"agency|shop|crew|employ\w{0,3})\b", re.I)


def _headcounts_in(masked: str) -> list[tuple[int, str]]:
    """Every headcount stated in one (masked) sentence, as (value, kind of pattern)."""
    found: dict[int, tuple[int, str]] = {}  # position of the number -> (value, kind)
    for kind, pattern in _HEADCOUNT_RES:
        for match in pattern.finditer(masked):
            raw = match.group("n")
            value = _number_value(raw)
            if not value or match.start("n") in found:
                continue
            if _is_year_like(raw, value) and kind != "label":
                continue
            if kind == "noun":
                before = masked[max(0, match.start() - 30):match.start()]
                if _NOT_STAFF_BEFORE_RE.search(before) or _NOT_STAFF_AFTER_RE.match(masked[match.end():]):
                    continue
                if _CLIENT_SIZE_BEFORE_RE.search(masked[max(0, match.start() - 40):match.start()]):
                    continue
                weak = match.group("noun").lower() in ("people", "professionals", "individuals", "colleagues")
                if weak and not _WE_RE.search(masked[max(0, match.start() - 60):match.start()]):
                    continue
            if 1 <= value <= MAX_EMPLOYEES:
                found[match.start("n")] = (value, kind)
    return list(found.values())


def _headcount_from_text(sentences: list[tuple[str, str]]) -> tuple[int | None, str | None]:
    """The page's stated headcount and the sentence it came from.

    "team of 4", "employs 60 people", "35-person team", "Employees: 45" and "grown to 40
    members" clearly describe the business itself, so when the page has any of those we
    use only them. A bare "50 employees" (the "noun" kind) is used only when nothing
    clearer is on the page: it is the wording most often about customers.
    Several numbers usually describe growth, so we take the largest; but when they differ
    more than HEADCOUNT_MAX_SPREAD times, the page is unclear and we take none."""
    strong: list[tuple[int, str]] = []  # (value, sentence)
    noun: list[tuple[int, str]] = []
    for sentence, masked in sentences:
        for value, kind in _headcounts_in(masked):
            if kind == "noun":
                noun.append((value, sentence))
            else:
                strong.append((value, sentence))
    found = strong or noun
    if not found:
        return None, None
    best = max(found, key=lambda item: item[0])
    smallest = min(value for value, _ in found)
    if best[0] > HEADCOUNT_MAX_SPREAD * smallest:
        return None, None
    return best[0], _snippet(best[1])


# -- Founded year ------------------------------------------------------------

_YEAR = r"(?<![\w#/.,$-])(?P<y>1[89]\d{2}|20\d{2})(?!\w|[.,]\d|\s?[-–]\s?\d)"
_MONTHS = (r"january|february|march|april|may|june|july|august|september|october|november|december|"
           r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
# After the verb: up to 6 words, then "in [January|early|the year] YEAR".
_FOUNDED_VERB_TAIL = (
    r"(?P<gap>(?:[\s,]{1,3}[\w'’&.-]{1,25}){0,6}?)[\s,]{1,3}in\s{1,3}"
    r"(?:(?:" + _MONTHS + r"|the\s{1,3}year|early|late|mid|the\s{1,3}(?:spring|summer|fall|autumn|winter)"
    r"|of)\s{1,3}){0,2}" + _YEAR)
_FOUNDED_RES = [
    # tier 1: "founded in 2010", "established in 1983", "founded in January 1952"
    (1, "verb", re.compile(
        r"\b(?:founded|established|incorporated|formed|organized)\b" + _FOUNDED_VERB_TAIL, re.I)),
    # tier 1: "since its founding in 1974"
    (1, "founding", re.compile(r"\bfounding\s{1,3}in\s{1,3}" + _YEAR, re.I)),
    # tier 1: "Founded: 2015"
    (1, "label", re.compile(r"\b(?:founded|established|year\s{1,3}founded|since|est)\s{0,2}[:\-–]\s{0,3}"
                            + _YEAR, re.I)),
    # tier 2: "started the practice in 1991", "opened our doors in 2004". Weaker than tier 1:
    # "The original store opened in 1924; our company was founded in 2010" -> 2010.
    (2, "verb", re.compile(r"\b(?:started|opened|launched|began)\b" + _FOUNDED_VERB_TAIL, re.I)),
    # tier 3: "Family-owned since 1952", "Serving Northern Michigan since 2000"
    (3, "since", re.compile(
        r"\b(?:family[- ]owned(?:\s{1,3}(?:and|&)\s{1,3}operated)?|family[- ]run|"
        r"locally[- ]owned(?:\s{1,3}(?:and|&)\s{1,3}operated)?|owned\s{1,3}(?:and|&)\s{1,3}operated|"
        r"in\s{1,3}business|(?:proudly\s{1,3})?serving(?:\s{1,3}[\w'’&.-]{1,25}){0,4}?|"
        r"a\s{1,3}(?:family\s{1,3})?tradition|operating|open|"
        r"been\s{1,3}(?:serving|providing|helping)(?:\s{1,3}[\w'’&.-]{1,25}){0,4}?)"
        r"\s{0,3},?\s{0,3}since\s{1,3}" + _YEAR, re.I)),
    # tier 4: "EST. 1998" (logo text), not "Est. 2,000 sq ft" or "Est. wait time"
    (4, "est", re.compile(r"(?<!\w)est\b\.?\s{0,2}" + _YEAR + r"(?!\s{0,2}(?:sq|ft|min|hours?|wait)\b)", re.I)),
    # tier 4: a badge "Since 1975 - Celebrating 50 Years!" or "Since 1998 we've been serving"
    (4, "badge", re.compile(
        r"^\W{0,3}since\s{1,3}" + _YEAR + r"\s{0,3}(?:$|[-–—|•·:!]|"
        r",?\s{0,3}we(?:'ve|’ve|\s{1,3}have)\s{1,3}been\s{1,3}(?:serving|providing|helping|proudly))", re.I)),
]
# In the words between the verb and "in YEAR": a person's career or a new branch/product.
_FOUNDED_GAP_REJECT_RE = re.compile(
    r"\b(?:her|his|career|school|college|residency|studies|training|degree|journey|working|work|"
    r"website|site|app|program|programme|service|product|blog|podcast|newsletter|campaign|"
    r"initiative|new|second|third|fourth|another)\b", re.I)
# Right after the verb, a word ending in -ing: "began offering Invisalign in 2008",
# "started accepting insurance in 2012" date a service, not the business.
# Lower case only, so a name like "started Sterling Plumbing in 1990" still counts.
_FOUNDED_GAP_GERUND_RE = re.compile(
    r"^[\s,]{1,3}(?!(?:during|something|everything|morning|evening|spring)\b)[a-z]{2,20}ing\b")
# Just before the verb: the sentence is about a person, a place or a later branch.
_FOUNDED_BEFORE_REJECT_RE = re.compile(
    r"\b(?:graduated|received|earned|licensed|practicing|joined|city|town|county|village|building|"
    r"church|university|college|school|hospital|new|second|third|fourth|another|latest|newest|"
    r"recently|renovated|remodeled|expanded|website|program|wing|member|voted|award|updated|revised|"
    r"class)\b", re.I)
# "Dr. Patel has been serving patients since 1990" is a person's career.
_PERSON_SUBJECT_RE = re.compile(r"\b(?:dr|mr|mrs|ms|he|she|his|her)\b", re.I)
# "1998 Main St": a street number, not a year ("opened in 2004 on Main Street" is fine).
_STREET_AFTER_RE = re.compile(
    r"^\s{1,3}(?!(?:on|in|at|by|and|from|near)\b)(?:[\w.]{1,20}\s{1,3}){0,2}?"
    r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|"
    r"ln|lane|way|ct|court|pkwy|parkway|hwy|highway|suite|ste)\b", re.I)


def _founded_from_text(sentences: list[tuple[str, str]]) -> tuple[int | None, str | None]:
    """The founding year the page states, and the sentence it came from."""
    this_year = datetime.now(timezone.utc).year
    candidates: list[tuple[int, int, str]] = []  # (tier, year, sentence)
    for sentence, masked in sentences:
        for tier, kind, pattern in _FOUNDED_RES:
            for match in pattern.finditer(masked):
                year = int(match.group("y"))
                if not 1800 <= year <= this_year:
                    continue
                before = masked[max(0, match.start() - 40):match.start()]
                if kind == "verb" and (_FOUNDED_GAP_REJECT_RE.search(match.group("gap"))
                                       or _FOUNDED_GAP_GERUND_RE.match(match.group("gap"))
                                       or _FOUNDED_BEFORE_REJECT_RE.search(before)):
                    continue
                if kind == "since" and (_PERSON_SUBJECT_RE.search(before)
                                        or _FOUNDED_BEFORE_REJECT_RE.search(before)):
                    continue
                if _STREET_AFTER_RE.match(masked[match.end():]):
                    continue
                candidates.append((tier, year, sentence))
    if not candidates:
        return None, None
    best_tier = min(tier for tier, _, _ in candidates)
    best = [(year, sentence) for tier, year, sentence in candidates if tier == best_tier]
    years = [year for year, _ in best]
    # Years that disagree: trust only the clearest wording ("founded in", "opened in"),
    # and take the earliest.
    if max(years) - min(years) > 2 and best_tier > 2:
        return None, None
    year, sentence = min(best, key=lambda item: item[0])
    return year, _snippet(sentence)


# -- Number of locations -----------------------------------------------------

_LOC_NOUN = (r"(?:locations|offices|clinics|branches|stores|shops|studios|salons|restaurants|showrooms|"
             r"centers|centres|practices|campuses|dealerships|cafes|bakeries|gyms|pharmacies|"
             r"hospitals|schools)\b")
_LOC_ADJ = (r"(?:(?:convenient|conveniently\s{1,3}located|great|full[- ]service|area|local|dental|"
            r"medical|beautiful|friendly|separate|different|physical|new|modern|family|veterinary|"
            r"chiropractic|retail\s{1,3}(?=stores|shops))\s{1,3}){0,2}")
_ORDINALS = {"second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6, "7th": 7,
             "8th": 8, "9th": 9, "10th": 10}
_LOCATIONS_RES = [
    # "our 5 convenient locations", "with 4 offices", "we now operate 12 stores", "grown to 14 clinics"
    re.compile(r"\b(?:our|with|have|has|operate|operates|operating|grown\s{1,3}to)\s{1,3}"
               r"(?:(?:now|over|more\s{1,3}than|nearly|almost|about|around|approximately|some)\s{1,3})?"
               + _NUMBER + r"\+?\s{1,3}" + _LOC_ADJ + _LOC_NOUN, re.I),
    # At the start of a sentence or heading: "Two locations to serve you"
    re.compile(r"^\W{0,3}" + _NUMBER + r"\s{1,3}" + _LOC_ADJ + _LOC_NOUN, re.I),
    # "offices in 3 cities" (a lower bound). "Serving 12 cities" is a service area: no match.
    re.compile(r"\b(?:offices|locations|clinics|branches|stores)\s{1,3}in\s{1,3}" + _NUMBER
               + r"\s{1,3}(?:cities|towns|states|countries|communities)\b", re.I),
    # "our second location"
    re.compile(r"\bour\s{1,3}(?P<n>" + "|".join(_ORDINALS) + r")\s{1,3}(?:new\s{1,3})?"
               r"(?:location|office|clinic|store|branch|restaurant|shop|studio|salon|showroom)\b", re.I),
]


def _locations_from_text(sentences: list[tuple[str, str]]) -> int | None:
    """The largest number of branches the page states (2..500), or None."""
    best: int | None = None
    for _sentence, masked in sentences:
        for pattern in _LOCATIONS_RES:
            for match in pattern.finditer(masked):
                raw = match.group("n").lower()
                value = _ORDINALS.get(raw) or _number_value(raw)
                if value and not _is_year_like(raw, value) and 2 <= value <= MAX_LOCATIONS:
                    best = max(best or 0, value)
    return best


# -- Careers link ------------------------------------------------------------

# The whole link text must be one of these, so "Jobs We Completed", "Career Advice" and
# "Join Our Mailing List" don't count. A bare "Join Us" is left out too: gyms, churches
# and clubs use it for membership (a "Join Us" link to /careers still counts by its path).
_CAREERS_TEXT_RE = re.compile(
    r"^(?:careers?|career\s{1,3}opportunities|jobs?|job\s{1,3}(?:openings|opportunities)|"
    r"current\s{1,3}(?:job\s{1,3})?openings|open\s{1,3}positions|employment(?:\s{1,3}opportunities)?|"
    r"join\s{1,3}(?:our|the)\s{1,3}team|work\s{1,3}(?:with|for)\s{1,3}us|"
    r"(?:we'?re|we’re|we\s{1,3}are|now)\s{1,3}hiring|hiring|team\s{1,3}openings)\s{0,2}[!.]?$", re.I)
# A section heading that says the business is hiring.
_HIRING_HEADING_RE = re.compile(r"^(?:we'?re|we’re|we\s{1,3}are|now)\s{1,3}hiring\s{0,2}[!.]?$", re.I)
# Path parts (whole parts, e.g. "/careers/" but not "/career-advice/"). Not "/join-us/":
# that is usually a membership page.
_CAREERS_PATH_PARTS = {
    "careers", "career", "jobs", "job-openings", "job-opportunities", "employment",
    "employment-opportunities", "join-our-team", "join-the-team", "work-with-us",
    "work-for-us", "hiring", "now-hiring", "were-hiring", "we-are-hiring", "open-positions",
    "career-opportunities", "current-openings",
}
# A link to one of these job sites means the business is hiring.
_ATS_HOSTS = ("greenhouse.io", "lever.co", "applytojob.com", "bamboohr.com", "workable.com",
              "recruiting.paylocity.com", "recruitee.com", "workforcenow.adp.com", "breezy.hr",
              "dentalpost.net", "poachedjobs.com", "harri.com")
# Paths that are content about jobs, not the business's own jobs page.
_NOT_CAREERS_PATH_RE = re.compile(
    r"blog|news|article|tips|advice|services|practice-areas|employment-law|portfolio|projects|"
    r"gallery|category|tag/|post", re.I)


def _careers_link(parser: _PageParser, page_url: str) -> tuple[bool, str | None]:
    """(has_careers, careers_url): a careers / jobs / "we're hiring" link or heading."""
    site = registered_domain(_host_of(page_url))
    for link in parser.links[:_MAX_LINKS]:
        href = link.href.strip()
        if not href or href.startswith("#"):
            continue
        url = _resolve(href, page_url)
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        # Only web addresses: never "JavaScript:...", "data:...", "mailto:..." in any letter case.
        # (An empty scheme only happens when we were given no page address to resolve against.)
        if parsed.scheme.lower() not in ("http", "https", ""):
            continue
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        if _NOT_CAREERS_PATH_RE.search(path):
            continue
        if host and any(host == h or host.endswith("." + h) for h in _ATS_HOSTS):
            return True, url
        if host == "indeed.com" or host.endswith(".indeed.com"):
            if path.startswith("/cmp/") and "/jobs" in path:
                return True, url
            continue
        if site and host and registered_domain(host) != site:
            continue  # another website (e.g. Glassdoor reviews)
        parts = {p for p in path.split("/") if p}
        text = link.text.strip()[:60]
        if parts & _CAREERS_PATH_PARTS or (len(text) <= 40 and _CAREERS_TEXT_RE.match(text)):
            return True, url
        # A bare "Opportunities" only counts when it links to /opportunities.
        if text.lower() == "opportunities" and "opportunities" in parts:
            return True, url
    for chunk in parser.chunks:
        if not chunk.in_link and len(chunk.text) <= 30 and _HIRING_HEADING_RE.match(chunk.text):
            return True, None
    return False, None


# -- JSON-LD (schema.org) ----------------------------------------------------

def _jsonld_nodes(parser: _PageParser):
    """Every dict in the page's JSON-LD blocks (walked without recursion, capped)."""
    count = 0
    for block in parser.jsonld:
        if len(block) > _JSONLD_LIMIT:
            continue
        try:
            data = json.loads(block.strip(), strict=False)
        except (ValueError, TypeError, RecursionError):
            try:  # trailing commas are a common mistake: {"a": 1,}
                data = json.loads(re.sub(r",\s{0,50}([}\]])", r"\1", block.strip()), strict=False)
            except (ValueError, TypeError, RecursionError):
                continue
        stack = [(data, 0)]
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > _JSONLD_MAX_NODES:
                return
            if isinstance(node, list):
                stack.extend((item, depth + 1) for item in reversed(node) if depth < 10)
            elif isinstance(node, dict):
                yield node
                if depth < 10:
                    stack.extend((v, depth + 1) for v in reversed(list(node.values()))
                                 if isinstance(v, (dict, list)))


_PLAIN_RANGE_RE = re.compile(_RANGE, re.I)


def _jsonld_employees(value) -> int | None:
    """numberOfEmployees in any of its shapes: 12, "12", "11-50", "50+",
    {"value": 25}, {"minValue": 11, "maxValue": 50}, or a list of those.
    0, 1 and "" are left out: site builders fill them in by default."""
    if isinstance(value, list):
        for item in value[:10]:
            found = _jsonld_employees(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in ("value", "minValue"):
            if key in value:
                found = _jsonld_employees(value[key])
                if found:
                    return found
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None  # NaN / Infinity / 1e400: Python's JSON reader allows them, int() would crash
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        match = _PLAIN_RANGE_RE.search(value[:100])
        number = _number_value(match.group("n")) if match else None
    else:
        return None
    return number if number and 2 <= number <= MAX_EMPLOYEES else None


def _jsonld_year(value) -> int | None:
    """foundingDate: 1998, "1998", "1998-01-01", "May 1998", "Est. 1998" -> 1998."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None  # NaN / Infinity / 1e400 (see _jsonld_employees)
    if isinstance(value, (int, float)):
        year = int(value)
    elif isinstance(value, str):
        match = re.search(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)", value[:100])
        year = int(match.group(1)) if match else None
    else:
        return None
    return year if year and 1800 <= year <= datetime.now(timezone.utc).year else None


def _short_json(key: str, value) -> str:
    try:
        return _snippet(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    except (TypeError, ValueError):
        return key


def _size_from_jsonld(parser: _PageParser) -> dict:
    """{"employees", "employees_snippet", "founded_year", "founded_snippet", "locations"}"""
    found: dict = {}
    for node in _jsonld_nodes(parser):
        if "employees" not in found and "numberOfEmployees" in node:
            employees = _jsonld_employees(node["numberOfEmployees"])
            if employees:
                found["employees"] = employees
                found["employees_snippet"] = _short_json("numberOfEmployees", node["numberOfEmployees"])
        if "founded_year" not in found:
            for key in ("foundingDate", "foundingYear", "dateFounded", "yearFounded"):
                year = _jsonld_year(node.get(key))
                if year:
                    found["founded_year"] = year
                    found["founded_snippet"] = _short_json(key, node[key])
                    break
        # Branches listed as "department" / "subOrganization": count distinct street addresses.
        for key in ("department", "subOrganization"):
            branches = node.get(key)
            if isinstance(branches, list):
                addresses = set()
                for branch in branches[:MAX_LOCATIONS]:
                    address = branch.get("address") if isinstance(branch, dict) else None
                    if isinstance(address, dict) and isinstance(address.get("streetAddress"), str):
                        addresses.add((" ".join(address["streetAddress"].lower().split()),
                                       str(address.get("postalCode") or "").strip()))
                if len(addresses) >= 2:
                    found["locations"] = max(found.get("locations", 0), len(addresses))
    return found


def _size_signals_from(parser: _PageParser, page_url: str) -> dict:
    """Signals that hint at how big the business is (see the section comment above)."""
    sentences = _size_sentences(parser)
    employees, employees_snippet = _headcount_from_text(sentences)
    founded, founded_snippet = _founded_from_text(sentences)
    locations = _locations_from_text(sentences)
    has_careers, careers_url = _careers_link(parser, page_url)
    jsonld = _size_from_jsonld(parser)

    employees_from = "text" if employees else None
    if not employees and jsonld.get("employees"):
        employees, employees_snippet, employees_from = jsonld["employees"], jsonld["employees_snippet"], "jsonld"
    if not founded and jsonld.get("founded_year"):
        founded, founded_snippet = jsonld["founded_year"], jsonld["founded_snippet"]
    if jsonld.get("locations"):
        locations = max(locations or 0, jsonld["locations"])
    return {
        "employees_stated": employees,
        "employees_from": employees_from,
        "employees_snippet": employees_snippet,
        "founded_year": founded,
        "founded_snippet": founded_snippet,
        "locations_stated": locations,
        "has_careers": has_careers,
        "careers_url": careers_url,
    }


def detect_size_signals(html: str, page_url: str = "") -> dict:
    """{"employees_stated", "employees_from", "employees_snippet", "founded_year",
    "founded_snippet", "locations_stated", "has_careers", "careers_url"}"""
    return _size_signals_from(_parse(html), page_url)


# ---------------------------------------------------------------------------
# Intent signals (what the website already has: analytics, ads, booking, chat...)
# ---------------------------------------------------------------------------
#
# They hint at which services a business may need: no online booking -> software, no
# analytics -> SEO, ad pixels -> they already pay for marketing. Precision first: a signal
# is True only on clear technical evidence (a script, iframe, form or link of a known
# provider, a meta tag, JSON-LD), never because a word appears in the visible text
# ("Online booking coming soon!", a privacy policy that mentions Google Analytics).
# Everything is a dictionary lookup or a plain substring search over capped lists, so it
# stays fast on hostile pages.

MAX_JOB_TITLES = 10
JOB_TITLE_LIMIT = 80  # characters per job title we keep
# A page with less visible text than this (or an empty <div id="root"></div>) is built by
# JavaScript in the browser. We never run JavaScript, so on such a page "we found no
# booking / chat / schema" means "we can't tell", not "they have none".
THIN_PAGE_CHARS = 200


@dataclass
class _Ref:
    """One address the page points to: a link, script, iframe or form."""
    kind: str  # "link", "script", "iframe" or "form"
    host: str
    domain: str  # registered domain of the host ("" for a relative address without a page URL)
    path: str  # lowercase
    query: str  # lowercase
    text: str = ""  # link text


def _refs_from(parser: _PageParser, page_url: str) -> list[_Ref]:
    sources = ([("script", src, "") for src in parser.script_srcs]
               + [("iframe", src, "") for src in parser.iframe_srcs]
               + [("form", action, "") for action in parser.form_actions]
               + [("link", link.href, link.text) for link in parser.links[:_MAX_LINKS]])
    refs: list[_Ref] = []
    for kind, raw, text in sources:
        raw = raw.strip()
        if not raw or raw.startswith("#") or raw[:11].lower().startswith(("mailto:", "tel:", "javascript:", "data:", "sms:")):
            continue
        try:
            parsed = urlparse(_resolve(raw, page_url))
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if parsed.scheme.lower() not in ("http", "https", ""):
            continue
        refs.append(_Ref(kind, host, registered_domain(host), parsed.path.lower(), parsed.query.lower(),
                         " ".join(text.split())[:100]))
    return refs


def _inline_code(parser: _PageParser) -> str:
    return "".join(parser.inline_script_parts)


def _is_resource(ref: _Ref) -> bool:
    """A script or iframe (code that runs on the page), not a link to somewhere."""
    return ref.kind in ("script", "iframe")


# -- Analytics / Search Console / ad pixels ------------------------------------

_GA_CONFIG_RE = re.compile(r"""gtag\(\s{0,5}['"]config['"]\s{0,5},\s{0,5}['"](?:G|UA|GT)-[A-Z0-9-]{4,20}['"]""", re.I)
_GA_CREATE_RE = re.compile(r"""\bga\(\s{0,5}['"]create['"]\s{0,5},\s{0,5}['"]UA-\d""", re.I)
# Substrings of inline code that load Google Analytics / Tag Manager
_GA_CODE_MARKS = ("googletagmanager.com/gtm.js", "googletagmanager.com/gtag/js?id=g-",
                  "googletagmanager.com/gtag/js?id=ua-", "googletagmanager.com/gtag/js?id=gt-",
                  "google-analytics.com/analytics.js", "google-analytics.com/ga.js",
                  "google-analytics.com/urchin.js")
_AW_RE = re.compile(r"(?<![A-Za-z0-9])AW-\d{6,12}(?!\d)")  # Google Ads ids are upper case
_FBQ_INIT_RE = re.compile(r"""fbq\(\s{0,5}['"]init['"]""")
_GTM_ID_RE = re.compile(r"(?<![A-Za-z0-9])GTM-[A-Z0-9]{4,10}(?![A-Za-z0-9])")


def _has_tag_manager(refs: list[_Ref], code: str, code_lower: str) -> bool:
    """A Google Tag Manager container. It can add chat widgets and ad pixels to the page
    after it loads, so their absence from the HTML proves nothing."""
    for ref in refs:
        if _is_resource(ref) and ref.domain == "googletagmanager.com" and ("gtm.js" in ref.path or "ns.html" in ref.path):
            return True
    return "googletagmanager.com/gtm.js" in code_lower or bool(_GTM_ID_RE.search(code))


def _has_analytics(refs: list[_Ref], code_lower: str) -> bool:
    for ref in refs:
        if not _is_resource(ref):
            continue
        if ref.domain == "google-analytics.com":
            return True
        if ref.domain == "googletagmanager.com":
            if "gtm.js" in ref.path or "ns.html" in ref.path:
                return True  # Google Tag Manager container
            if "gtag/js" in ref.path:
                ids = parse_qs(ref.query).get("id", [""])
                # "AW-..." alone is a Google Ads tag, not analytics
                if any(i.startswith(("g-", "ua-", "gt-")) for i in ids):
                    return True
    if any(mark in code_lower for mark in _GA_CODE_MARKS):
        return True
    return bool(_GA_CONFIG_RE.search(code_lower) or _GA_CREATE_RE.search(code_lower))


def _ad_pixels(refs: list[_Ref], code: str, code_lower: str) -> list[str]:
    found: set[str] = set()
    resources = [r for r in refs if _is_resource(r)]
    for ref in resources:
        if ref.domain == "googleadservices.com" or ref.host == "googleads.g.doubleclick.net":
            found.add("google_ads")
        if ref.domain == "googletagmanager.com" and "id=aw-" in ref.query:
            found.add("google_ads")
        if ref.host == "connect.facebook.net" and "fbevents.js" in ref.path:
            found.add("meta")
        if ref.host == "analytics.tiktok.com":
            found.add("tiktok")
        if ref.host == "snap.licdn.com":
            found.add("linkedin")
        if ref.host == "bat.bing.com":
            found.add("bing")
    if ("googleadservices.com" in code_lower or "googleads.g.doubleclick.net" in code_lower
            or _AW_RE.search(code)):
        found.add("google_ads")
    if ("connect.facebook.net" in code_lower and "fbevents.js" in code_lower) or _FBQ_INIT_RE.search(code):
        found.add("meta")
    if "analytics.tiktok.com" in code_lower:
        found.add("tiktok")
    if "snap.licdn.com" in code_lower or "_linkedin_partner_id" in code_lower:
        found.add("linkedin")
    if "bat.bing.com" in code_lower:
        found.add("bing")
    return sorted(found)


# -- schema.org LocalBusiness / Organization --------------------------------------

# LocalBusiness, its subtypes and Organization types (lowercase)
_LOCAL_SCHEMA_TYPES = {t.lower() for t in """
LocalBusiness Organization Corporation MedicalOrganization NGO EducationalOrganization
SportsOrganization OnlineBusiness OnlineStore Cooperative ProfessionalService
AnimalShelter AutomotiveBusiness AutoBodyShop AutoDealer AutoPartsStore AutoRental AutoRepair
AutoWash GasStation MotorcycleDealer MotorcycleRepair ChildCare Dentist DryCleaningOrLaundry
EmergencyService EmploymentAgency EntertainmentBusiness ArtGallery AmusementPark Casino ComedyClub
MovieTheater NightClub FinancialService AccountingService BankOrCreditUnion InsuranceAgency
FoodEstablishment Bakery BarOrPub Brewery CafeOrCoffeeShop Distillery FastFoodRestaurant
IceCreamShop Restaurant Winery HealthAndBeautyBusiness BeautySalon DaySpa HairSalon HealthClub
NailSalon TattooParlor HomeAndConstructionBusiness Electrician GeneralContractor HVACBusiness
HousePainter Locksmith MovingCompany Plumber RoofingContractor LegalService Attorney Notary
LodgingBusiness BedAndBreakfast Campground Hostel Hotel Motel Resort VacationRental MedicalBusiness
MedicalClinic Hospital Pharmacy Physician IndividualPhysician PhysiciansOffice Physiotherapy
Optician Optometric Dermatology DietNutrition Pediatric PlasticSurgery Podiatric PrimaryCare
Psychiatric VeterinaryCare RealEstateAgent RecyclingCenter SelfStorage ShoppingCenter
SportsActivityLocation BowlingAlley ExerciseGym GolfCourse SportsClub TennisComplex Store
BikeStore BookStore ClothingStore ComputerStore ConvenienceStore DepartmentStore ElectronicsStore
Florist FurnitureStore GardenStore GroceryStore HardwareStore HobbyShop HomeGoodsStore JewelryStore
LiquorStore MensClothingStore MobilePhoneStore MusicStore OfficeEquipmentStore OutletStore PawnShop
PetStore ShoeStore SportingGoodsStore TireShop ToyStore WholesaleStore TravelAgency
TouristInformationCenter
""".split()}


def _schema_type_names(value) -> list[str]:
    """"https://schema.org/Dentist" / "schema:Dentist" / ["Dentist", "Organization"] -> names."""
    values = value if isinstance(value, list) else [value]
    names = []
    for item in values[:10]:
        if isinstance(item, str):
            for part in item.split()[:10]:
                names.append(part.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower())
    return names


def _has_local_schema(parser: _PageParser) -> bool:
    for node in _jsonld_nodes(parser):
        if any(name in _LOCAL_SCHEMA_TYPES for name in _schema_type_names(node.get("@type"))):
            return True
    return any(name in _LOCAL_SCHEMA_TYPES for itemtype in parser.itemtypes
               for name in _schema_type_names(itemtype) if "schema.org" in itemtype.lower())


# -- Links to known providers ------------------------------------------------------

# First path part of a provider's own marketing pages ("calendly.com/pricing"): a link
# there is a "powered by" / review link, not the business's booking page.
_PROVIDER_MARKETING_PARTS = {
    "pricing", "features", "signup", "sign-up", "login", "log-in", "signin", "sign-in", "about",
    "about-us", "blog", "careers", "pro", "business", "for-business", "partners", "integrations",
    "solutions", "product", "products", "resources", "support", "help", "legal", "privacy",
    "terms", "biz", "contact", "press", "download", "app",
}


def _provider_page(ref: _Ref) -> bool:
    """True when a link points to a business's own page on a provider (calendly.com/acme),
    False for the provider's homepage or marketing pages. Scripts / iframes always count."""
    if _is_resource(ref):
        return True
    on_main_host = ref.host in (ref.domain, "www." + ref.domain)
    parts = [p for p in ref.path.split("/") if p]
    if on_main_host and not parts and not ref.query:
        return False  # the provider's homepage
    if on_main_host and parts and parts[0] in _PROVIDER_MARKETING_PARTS:
        return False
    return True


def _in(text: str, *words: str) -> bool:
    return any(w in text for w in words)


# Booking platforms: registered domain -> (name, extra check of (host, path, query) or None)
_BOOKING_PROVIDERS: dict[str, tuple[str, object]] = {
    "calendly.com": ("Calendly", None),
    "acuityscheduling.com": ("Acuity Scheduling", None),
    "as.me": ("Acuity Scheduling", None),
    "squarespacescheduling.com": ("Acuity Scheduling", None),
    "zocdoc.com": ("Zocdoc", None),
    "opentable.com": ("OpenTable", lambda h, p, q: not h.startswith("restaurant.")),
    "resy.com": ("Resy", None),
    "exploretock.com": ("Tock", None),
    "sevenrooms.com": ("SevenRooms", lambda h, p, q: _in(p, "reservation", "/explore/") or _is_widget_host(h)),
    "vagaro.com": ("Vagaro", None),
    "mindbodyonline.com": ("Mindbody", None),
    "healcode.com": ("Mindbody", None),
    "booksy.com": ("Booksy", None),
    "schedulicity.com": ("Schedulicity", None),
    "setmore.com": ("Setmore", None),
    "simplybook.me": ("SimplyBook.me", None),
    "simplybook.it": ("SimplyBook.me", None),
    "fresha.com": ("Fresha", None),
    "styleseat.com": ("StyleSeat", None),
    "glossgenius.com": ("GlossGenius", None),
    "joinblvd.com": ("Boulevard", None),
    "blvd.co": ("Boulevard", None),
    "zenoti.com": ("Zenoti", lambda h, p, q: h not in ("zenoti.com", "www.zenoti.com")),
    "phorest.com": ("Phorest", lambda h, p, q: "/book" in p),
    "gettimely.com": ("Timely", lambda h, p, q: h.startswith("bookings.")),
    "booker.com": ("Booker", lambda h, p, q: h not in ("booker.com", "www.booker.com")),
    "janeapp.com": ("Jane", lambda h, p, q: h not in ("janeapp.com", "www.janeapp.com")),
    "clientsecure.me": ("SimplePractice", None),
    "youcanbook.me": ("YouCanBook.me", None),
    "oncehub.com": ("OnceHub", None),
    "appointy.com": ("Appointy", None),
    "10to8.com": ("10to8", None),
    "picktime.com": ("Picktime", None),
    "fareharbor.com": ("FareHarbor", None),
    "checkfront.com": ("Checkfront", None),
    "squareup.com": ("Square Appointments",
                     lambda h, p, q: h.startswith("book.") or _in(p, "/appointments/book", "/appointments/buyer")),
    "square.site": ("Square Appointments", lambda h, p, q: "book" in p),
    "localmed.com": ("LocalMed", None),
    "nexhealth.com": ("NexHealth", None),
    "healthgrades.com": ("Healthgrades", lambda h, p, q: _in(p + "?" + q, "book", "appointment")),
    "practo.com": ("Practo", None),
    "oladoc.com": ("oladoc", None),
    "marham.pk": ("Marham", None),
    "housecallpro.com": ("Housecall Pro", lambda h, p, q: h.startswith(("book.", "online-booking."))),
    "getjobber.com": ("Jobber", lambda h, p, q: h.startswith("clienthub.") and _in(p, "booking", "request")),
    "servicetitan.com": ("ServiceTitan", lambda h, p, q: "scheduler" in h),
    "hubspot.com": ("HubSpot Meetings", lambda h, p, q: h.startswith("meetings")),
    # Vets, dental / eye care, HVAC / plumbing, auto repair, gyms, legal intake
    "vetstoria.com": ("Vetstoria", lambda h, p, q: _app_host(h, "vetstoria.com")),
    "petdesk.com": ("PetDesk", lambda h, p, q: _app_host(h, "petdesk.com")),
    "getweave.com": ("Weave", lambda h, p, q: h.startswith(("book.", "schedule.", "app.book."))),
    "scheduleengine.net": ("Schedule Engine", None),
    "tekmetric.com": ("Tekmetric", lambda h, p, q: "appointment" in p),
    "shopmonkey.io": ("Shopmonkey", lambda h, p, q: _in(p, "book", "appointment")),
    "zenplanner.com": ("Zen Planner", lambda h, p, q: _app_host(h, "zenplanner.com")),
    "pushpress.com": ("PushPress", lambda h, p, q: _app_host(h, "pushpress.com")),
    "wodify.com": ("Wodify", lambda h, p, q: _app_host(h, "wodify.com")),
    "glofox.com": ("Glofox", lambda h, p, q: _app_host(h, "glofox.com")),
    "doctible.com": ("Doctible", lambda h, p, q: _app_host(h, "doctible.com")),
    "flex.dental": ("Flex Dental", lambda h, p, q: _app_host(h, "flex.dental") or _in(p, "book", "schedule")),
    "yapiapp.com": ("Yapi", lambda h, p, q: _app_host(h, "yapiapp.com")),
    "modento.io": ("Modento", lambda h, p, q: _app_host(h, "modento.io")),
    "solutionreach.com": ("Solutionreach", lambda h, p, q: _app_host(h, "solutionreach.com")),
    "lh360.com": ("Lighthouse 360", lambda h, p, q: _app_host(h, "lh360.com")),
    "patientpop.com": ("PatientPop", lambda h, p, q: _app_host(h, "patientpop.com")),
    "tebra.com": ("Tebra", lambda h, p, q: _app_host(h, "tebra.com")),
    "lawmatics.com": ("Lawmatics", lambda h, p, q: _app_host(h, "lawmatics.com")),
    "clio.com": ("Clio Grow", lambda h, p, q: h.startswith("grow.") or "/grow/" in p),
    "goclio.com": ("Clio Grow", lambda h, p, q: h.startswith("grow.")),
    "google.com": ("Google Calendar", lambda h, p, q: h == "calendar.google.com" and "/appointments" in p),
    "calendar.app.google": ("Google Calendar", None),
}


def _is_widget_host(host: str) -> bool:
    return host.startswith(("widget", "widgets.", "embed."))


# Sub-domains a provider uses for its own website, not for its customers' booking pages
_PROVIDER_SITE_HOSTS = ("www.", "help.", "support.", "blog.", "docs.", "status.", "community.",
                        "info.", "go.", "learn.", "academy.", "developers.", "partners.", "careers.")


def _app_host(host: str, domain: str) -> bool:
    """booking.vetstoria.com / acme.zenplanner.com: a sub-domain where customers book,
    not vetstoria.com itself or its help / blog pages."""
    return host != domain and not host.startswith(_PROVIDER_SITE_HOSTS)


def _provider_match(ref: _Ref, providers: dict) -> str | None:
    """Name of the provider this link / script belongs to, or None."""
    entry = providers.get(ref.domain)
    if entry is None and ref.host in providers:
        entry = providers[ref.host]
    if entry is None:
        return None
    name, check = entry
    if not _provider_page(ref):
        return None
    if check is not None and not check(ref.host, ref.path, ref.query):
        return None
    return name


# "Book now", "Book online", "Book an appointment", "Schedule online", "Request an appointment"...
_BOOKING_TEXT_RE = re.compile(
    r"^(?:(?:book|schedule|request|make|reserve)\s{1,3}(?:an?\s{1,3}|your\s{1,3}|my\s{1,3}|a\s{1,3}free\s{1,3})?"
    r"(?:online|now|appointment|appt|visit|consultation|consult|session|table|today|service|exam|cleaning)"
    r"(?:\s{1,3}(?:online|now|today))?|online\s{1,3}(?:booking|scheduling|appointments?))\s{0,2}[!.»›>]?$", re.I)
# Words in a same-site path that show the link leads to a booking page / form
_BOOKING_PATH_WORDS = {"book", "booking", "bookings", "book-online", "book-now", "appointment",
                       "appointments", "appt", "schedule", "scheduling", "reserve", "reservation",
                       "reservations", "request-appointment", "request-an-appointment",
                       "book-an-appointment", "schedule-appointment", "online-booking"}
_CONTENT_PATH_WORDS = {"blog", "news", "post", "posts", "article", "articles", "tag", "category"}


def _path_words(path: str) -> set[str]:
    """'/book-online/new' -> {'book-online', 'book', 'online', 'new'}"""
    words: set[str] = set()
    for part in path.split("/")[:20]:
        if part:
            words.add(part)
            words.update(w for w in re.split(r"[-_.]", part[:100]) if w)
    return words


def _same_site(ref: _Ref, site: str) -> bool:
    return not ref.host or (bool(site) and ref.domain == site)


# WhatsApp links ("Book on WhatsApp", wa.me/92300...?text=I want to book an appointment):
# how many businesses in Pakistan and India take bookings.
_WHATSAPP_DOMAINS = {"wa.me", "whatsapp.com"}
_WHATSAPP_BOOKING_RE = re.compile(r"\b(?:book|booking|appointments?|appt|reserve|reservations?)\b", re.I)


def _whatsapp_booking(ref: _Ref) -> bool:
    if ref.kind != "link" or ref.domain not in _WHATSAPP_DOMAINS:
        return False
    message = " ".join(parse_qs(ref.query).get("text", []))[:300]
    return bool(_BOOKING_TEXT_RE.match(ref.text) or _WHATSAPP_BOOKING_RE.search(ref.text)
                or _WHATSAPP_BOOKING_RE.search(message))


def _booking(refs: list[_Ref], site: str) -> str | None:
    """Name of the booking provider, "WhatsApp", "own form", or None."""
    for ref in refs:
        if ref.kind == "form":
            continue
        name = _provider_match(ref, _BOOKING_PROVIDERS)
        if name:
            return name
    if any(_whatsapp_booking(ref) for ref in refs):
        return "WhatsApp"
    for ref in refs:
        if ref.kind != "link" or not _same_site(ref, site) or not _BOOKING_TEXT_RE.match(ref.text):
            continue
        words = _path_words(ref.path)
        if words & _BOOKING_PATH_WORDS and not words & _CONTENT_PATH_WORDS:
            return "own form"
    return None


# -- Online payment ------------------------------------------------------------

_PAY_TEXT_RE = re.compile(
    r"^(?:pay\s{1,3}(?:online|now|here|today|(?:your|my|a)\s{1,3}(?:bill|invoice|balance|statement)|bill(?:\s{1,3}online)?|invoice)|"
    r"make\s{1,3}(?:a\s{1,3})?payment(?:\s{1,3}online)?|online\s{1,3}(?:bill\s{1,3})?pay(?:ments?)?|"
    r"bill\s{0,3}pay|payments?|patient\s{1,3}payments?|pay\s{1,3}(?:my\s{1,3})?bill\s{1,3}online)\s{0,2}[!.»›>]?$", re.I)
_PAY_PATH_WORDS = {"pay", "payment", "payments", "billpay", "bill-pay", "pay-online", "pay-bill",
                   "make-a-payment", "pay-your-bill", "online-payment", "online-payments", "paybill"}


def _stripe(h, p, q):
    return h.split(".")[0] in ("js", "checkout", "buy", "billing", "donate", "invoice", "pay")


def _paypal(h, p, q):
    return (h.startswith(("www.paypalobjects.", "paypalobjects.")) or "/sdk/js" in p
            or _in(p, "/paypalme/", "/donate", "/cgi-bin/webscr", "/invoice/", "/ncp/payment", "/checkoutnow"))


_PAYMENT_PROVIDERS: dict[str, tuple[str, object]] = {
    "stripe.com": ("Stripe", _stripe),
    "stripe.network": ("Stripe", None),
    "paypal.com": ("PayPal", _paypal),
    "paypalobjects.com": ("PayPal", lambda h, p, q: "/api/checkout" in p or "/en_us/i/btn" in p or "/js/external" in p),
    "paypal.me": ("PayPal", None),
    "square.link": ("Square", None),
    "squareup.com": ("Square", lambda h, p, q: h.startswith(("js.", "web.")) or _in(p, "/pay-invoice", "/pay/", "/checkout")),
    "squarecdn.com": ("Square", lambda h, p, q: h.startswith("web.") or "payments" in p),
    "square.site": ("Square", lambda h, p, q: h.startswith("checkout.")),
    "braintreegateway.com": ("Braintree", None),
    "authorize.net": ("Authorize.net", lambda h, p, q: h.startswith(("js.", "jstest.", "accept.", "secure", "simplecheckout."))),
    "clover.com": ("Clover", lambda h, p, q: h.startswith("checkout.") or p.startswith("/pay")),
    "lawpay.com": ("LawPay", lambda h, p, q: h.startswith("secure.")),
    # India
    "razorpay.com": ("Razorpay", lambda h, p, q: h.split(".")[0] in ("checkout", "pages", "api", "cdn")),
    "razorpay.me": ("Razorpay", None),
    "rzp.io": ("Razorpay", None),
    "instamojo.com": ("Instamojo", lambda h, p, q: h.startswith(("js.", "api.", "test.")) or p.startswith("/@")),
    "imjo.in": ("Instamojo", None),
    "paytm.me": ("Paytm", None),
    "paytm.in": ("Paytm", lambda h, p, q: h.startswith(("securegw.", "securegw-stage."))),
    "phonepe.com": ("PhonePe", lambda h, p, q: h.startswith(("mercury.", "mercury-t2.", "api."))),
    "phon.pe": ("PhonePe", None),
    "cashfree.com": ("Cashfree", lambda h, p, q: h.startswith(("payments.", "sdk.", "api."))),
    "payu.in": ("PayU", lambda h, p, q: h.startswith(("secure.", "checkout.", "u.", "pmny."))
                or p.startswith(("/pay/", "/invoice"))),
    # Pakistan
    "jazzcash.com.pk": ("JazzCash", lambda h, p, q: h.startswith(("payments.", "sandbox."))),
    "easypaisa.com.pk": ("Easypaisa", lambda h, p, q: h.startswith(("easypay.", "easypaystg."))),
}
# Bill-pay hosts that only count from a "Pay online" / "Pay your bill" link
_BILL_PAY_DOMAINS = {"lawpay.com", "paysimple.com", "intuit.com", "invoicecloud.com", "payjunction.com",
                     "paymentus.com", "instamed.com", "rectanglehealth.com", "clientpay.com", "headnote.com",
                     "xpress-pay.com", "authorize.net", "clover.com", "squareup.com", "square.link",
                     "paypal.com", "paypal.me", "stripe.com", "patientpay.com", "phreesia.net",
                     "payzen.com", "elavonpayments.com", "convergepay.com", "payliance.com",
                     "razorpay.com", "razorpay.me", "rzp.io", "instamojo.com", "imjo.in", "paytm.me",
                     "paytm.com", "phonepe.com", "cashfree.com", "payu.in", "jazzcash.com.pk",
                     "easypaisa.com.pk"}


def _has_payment(refs: list[_Ref], site: str) -> bool:
    for ref in refs:
        if _provider_match(ref, _PAYMENT_PROVIDERS):
            return True
    for ref in refs:
        if ref.kind != "link" or not _PAY_TEXT_RE.match(ref.text):
            continue
        if ref.domain in _BILL_PAY_DOMAINS and _provider_page(ref):
            return True
        if _same_site(ref, site) and _path_words(ref.path) & _PAY_PATH_WORDS:
            return True
    return False


# -- Shop / online ordering --------------------------------------------------------

_SHOP_PATH_WORDS = {"cart", "basket", "checkout", "product", "products", "shop", "store"}


def _has_shop(parser: _PageParser, refs: list[_Ref], code_lower: str, site: str) -> bool:
    if parser.has_add_to_cart:
        return True
    resources = [r for r in refs if _is_resource(r)]
    for ref in resources:
        if ref.domain in ("shopify.com", "myshopify.com", "shopifycdn.com", "bigcommerce.com", "ecwid.com"):
            if ref.domain != "shopify.com" or ref.host.startswith("cdn."):
                return True
    if "shopify.theme" in code_lower or "shopifybuy" in code_lower or "ecwid_product_browser" in code_lower:
        return True
    # WooCommerce: its files alone are not enough (the plugin may be installed and unused),
    # it also needs links to the cart / products.
    woo = any("/plugins/woocommerce/" in r.path for r in resources) or "woocommerce_params" in code_lower
    if woo:
        for ref in refs:
            if ref.kind == "link" and _same_site(ref, site):
                parts = {p for p in ref.path.split("/") if p}
                if parts & _SHOP_PATH_WORDS or "add-to-cart=" in ref.query:
                    return True
    return False


def _toast(h, p, q):
    return h.startswith("order.") or "/order" in p or p.endswith("/v3") or p.startswith("/local/")


_ORDERING_PROVIDERS: dict[str, tuple[str, object]] = {
    "toasttab.com": ("Toast", _toast),
    "chownow.com": ("ChowNow", lambda h, p, q: h.split(".")[0] in ("ordering", "direct", "order")
                    or p.startswith("/order")),
    "doordash.com": ("DoorDash", lambda h, p, q: p.startswith("/store/")),
    "order.online": ("DoorDash", None),
    "ubereats.com": ("Uber Eats", lambda h, p, q: "/store/" in p),
    "grubhub.com": ("Grubhub", lambda h, p, q: p.startswith("/restaurant/")),
    "seamless.com": ("Seamless", lambda h, p, q: p.startswith(("/menu/", "/restaurant/"))),
    "postmates.com": ("Postmates", lambda h, p, q: p.startswith(("/store/", "/merchant/"))),
    "slicelife.com": ("Slice", lambda h, p, q: p.startswith("/restaurants/")),
    "menufy.com": ("Menufy", None),
    "beyondmenu.com": ("BeyondMenu", None),
    "spoton.com": ("SpotOn", lambda h, p, q: h.startswith("order.")),
    "clover.com": ("Clover", lambda h, p, q: p.startswith("/online-ordering")),
    "foodbooking.com": ("GloriaFood", None),
    "fbgcdn.com": ("GloriaFood", None),
    "foodpanda.pk": ("foodpanda", lambda h, p, q: "/restaurant/" in p),
    "foodpanda.com": ("foodpanda", lambda h, p, q: "/restaurant/" in p),
    "swiggy.com": ("Swiggy", lambda h, p, q: p.startswith(("/restaurants/", "/city/"))),
    "zomato.com": ("Zomato", lambda h, p, q: "/order" in p),
    "olo.com": ("Olo", lambda h, p, q: h not in ("olo.com", "www.olo.com")),
}
_ORDER_TEXT_RE = re.compile(
    r"^(?:order\s{1,3}(?:online|now|here|today|pickup|pick-up|delivery|takeout|take-out|carryout|food|ahead)"
    r"(?:\s{1,3}(?:now|here|today))?|online\s{1,3}ordering|start\s{1,3}(?:an?\s{1,3}|your\s{1,3})?order)\s{0,2}[!.»›>]?$", re.I)


def _has_ordering(refs: list[_Ref], site: str) -> bool:
    for ref in refs:
        if ref.kind != "form" and _provider_match(ref, _ORDERING_PROVIDERS):
            return True
    for ref in refs:
        if ref.kind != "link" or not _ORDER_TEXT_RE.match(ref.text) or not _provider_page(ref):
            continue
        if ref.domain in ("square.site", "squareup.com"):  # Square Online store
            return True
        # order.acmepizza.com: the business's own ordering site
        if _same_site(ref, site) and ref.host.split(".")[0] in ("order", "orders", "ordering"):
            return True
    return False


# -- PDF forms ------------------------------------------------------------------

_PDF_FORM_RE = re.compile(
    r"\b(?:forms?|intake|registration|register|application|paperwork|new\s{1,3}patients?|newpatient|"
    r"questionnaire|enrollment|enrolment|consent|medical\s{1,3}history|health\s{1,3}history)\b", re.I)
# PDFs may be stored on the site builder's file server rather than on the site itself.
_PDF_FILE_HOSTS = {"wixstatic.com", "filesusr.com", "squarespace.com", "sqspcdn.com", "wsimg.com",
                   "amazonaws.com", "cloudfront.net", "weebly.com", "editmysite.com", "websitepro.hosting",
                   "hubspotusercontent.com", "hubspotusercontent-na1.net", "hubfs.net", "webflow.com",
                   "website-files.com", "godaddysites.com", "b-cdn.net", "googleusercontent.com"}


def _has_pdf_forms(refs: list[_Ref], site: str) -> bool:
    for ref in refs:
        if ref.kind != "link" or not ref.path.endswith(".pdf"):
            continue
        if not (_same_site(ref, site) or ref.domain in _PDF_FILE_HOSTS):
            continue
        file_name = unquote(ref.path.rsplit("/", 1)[-1])[:200]
        file_words = re.sub(r"[-_.+%]", " ", file_name)
        if _PDF_FORM_RE.search(ref.text) or _PDF_FORM_RE.search(file_words):
            return True
    return False


# -- Live chat ------------------------------------------------------------------

# Substrings of a chat widget's script address / loader code
_CHAT_MARKS = (
    "widget.intercom.io", "js.intercomcdn.com", "js.driftt.com", "embed.tawk.to", "cdn.livechatinc.com",
    "static.zdassets.com/ekr/snippet.js", "v2.zopim.com", "js.usemessages.com", "code.tidio.co",
    "client.crisp.chat", "static.olark.com", "connect.podium.com", "birdeye.com/embed/v6/",
    "wchat.freshchat.com", "smartsuppchat.com", "code.jivosite.com", "call.chatra.io",
    "xfbml.customerchat.js", "config.gorgias.chat", "salesiq.zoho", "userlike-cdn-widgets",
    "apexchat.net", "ngageics.com", "chatserver.comm100.com", "vue.comm100.com",
)


def _has_live_chat(parser: _PageParser, code_lower: str) -> bool:
    srcs = " ".join(s.lower() for s in parser.script_srcs + parser.iframe_srcs)
    return any(mark in srcs or mark in code_lower for mark in _CHAT_MARKS)


# -- Job titles on the careers page ---------------------------------------------

# Words that make a short line a job title ("Dental Assistant", "Office Manager").
_JOB_NOUNS = set("""
receptionist assistant hygienist technician tech manager coordinator specialist representative rep
associate developer engineer designer administrator admin clerk dispatcher scheduler nurse driver
installer plumber electrician cashier server cook chef host hostess bartender barista stylist barber
therapist accountant bookkeeper agent advisor consultant analyst director supervisor intern attorney
paralegal secretary mechanic apprentice writer strategist executive officer operator programmer
estimator foreman laborer helper dishwasher busser aide caregiver dentist physician optometrist
veterinarian pharmacist groomer trainer instructor teacher tutor cleaner housekeeper janitor porter
valet merchandiser buyer planner generalist recruiter worker lpn rn cna esthetician
""".split())
_JOB_PHRASES = ("front desk", "data entry", "front office")
# Words that show a line is a sentence, not a title
_NOT_TITLE_WORDS = {"the", "a", "an", "will", "you", "your", "we", "our", "us", "is", "are", "be", "to",
                    "with", "who", "that", "this", "if", "please", "must", "should", "can", "help",
                    "assist", "answer", "apply", "contact", "click", "email", "call", "send", "join",
                    "meet", "about", "view", "see", "read", "learn", "more", "no", "not"}
_HIRING_ROLE_RES = [
    ("front_desk", re.compile(r"\b(?:receptionists?|front\s{1,3}(?:desk|office)|patient\s{1,3}(?:care\s{1,3})?coordinators?)\b", re.I)),
    ("scheduling", re.compile(r"\b(?:schedulers?|scheduling|dispatchers?|dispatch)\b", re.I)),
    ("data_entry", re.compile(r"\bdata\s{1,3}entry\b", re.I)),
    ("customer_service", re.compile(r"\b(?:customer\s{1,3}(?:service|support|care|success)|call\s{1,3}cent(?:er|re))\b", re.I)),
    ("office_admin", re.compile(r"\b(?:office\s{1,3}(?:manager|administrator|assistant|coordinator|admin)|"
                                r"administrative\s{1,3}assistant|admin\s{1,3}assistant|secretary)\b", re.I)),
    ("marketing", re.compile(r"\b(?:marketing|seo|content\s{1,3}(?:writer|creator|marketer|strategist|specialist|manager))\b", re.I)),
    ("social_media", re.compile(r"\bsocial\s{1,3}media\b", re.I)),
    ("web_it", re.compile(r"\b(?:web\s{1,3}(?:developer|designer)|software|developer|programmer|"
                          r"systems?\s{1,3}administrator|network\s{1,3}(?:administrator|engineer)|help\s{0,3}desk)\b", re.I)),
    ("sales", re.compile(r"\b(?:sales|account\s{1,3}executive|business\s{1,3}development)\b", re.I)),
]
_IT_RE = re.compile(r"\bIT\b")  # case-sensitive: "IT Support Specialist", not "it"


def _looks_like_job_title(text: str) -> bool:
    if len(text) > 100 or not text[:1].isupper() or text.endswith(("?", ":")) or ". " in text:
        return False
    if text.endswith(".") and not text.endswith(("Jr.", "Sr.", "Inc.")):
        return False
    words = re.findall(r"[A-Za-z]+", text.lower())
    if not words or len(words) > 8 or set(words) & _NOT_TITLE_WORDS:
        return False
    lower = text.lower()
    if any(phrase in lower for phrase in _JOB_PHRASES):
        return True
    return any(w in _JOB_NOUNS or (w.endswith("s") and w[:-1] in _JOB_NOUNS) for w in words)


def _hiring_roles(title: str) -> set[str]:
    roles = {role for role, role_re in _HIRING_ROLE_RES if role_re.search(title)}
    if _IT_RE.search(title):
        roles.add("web_it")
    return roles


def _job_titles_from(parser: _PageParser) -> tuple[list[str], list[str]]:
    """(hiring_titles, hiring_roles) from a careers page: short title-like lines with a job word."""
    titles: list[str] = []
    seen: set[str] = set()
    roles: set[str] = set()
    for chunk in parser.chunks[:20_000]:
        if chunk.in_menu:
            continue
        text = chunk.text.strip(" -–—|•·*:")
        if not text or not _looks_like_job_title(text):
            continue
        roles |= _hiring_roles(text)
        key = text.lower()
        if key not in seen and len(titles) < MAX_JOB_TITLES:
            seen.add(key)
            titles.append(text[:JOB_TITLE_LIMIT].rstrip())
    return titles, sorted(roles)


# -- All intent signals of one page ------------------------------------------------

def _has_viewport(parser: _PageParser, page_url: str) -> bool | None:
    """True = built for phones. None = can't tell: the site has a separate mobile version
    (an m. site announced with <link rel="alternate" media="...max-width...">, or we were
    sent to it), which phones get instead of the desktop page we read."""
    content = "".join(parser.meta.get("viewport", "").lower().split())
    if "width=device-width" in content:
        return True
    if parser.has_mobile_alternate or _host_of(page_url).startswith(("m.", "mobile.")):
        return None
    return False


# Signals that say "the site lacks this" when False / []. They become None ("can't tell")
# when the page HTML can't show them (see THIN_PAGE_CHARS).
_ABSENCE_KEYS = ("has_analytics", "ad_pixels", "has_local_schema", "has_online_booking",
                 "has_online_payment", "has_shop", "has_online_ordering", "has_live_chat")
# Only ever found in scripts, so unknown when a speed plugin delays the scripts...
_DELAYED_SCRIPT_KEYS = ("has_analytics", "ad_pixels", "has_live_chat")
# ...and often added by a Google Tag Manager container after the page loads.
_TAG_MANAGER_KEYS = ("ad_pixels", "has_live_chat")


def _built_by_javascript(parser: _PageParser) -> bool:
    return parser.empty_app_mount or parser.visible_chars < THIN_PAGE_CHARS


def _intent_signals_from(parser: _PageParser, page_url: str, careers: bool = False) -> dict:
    """Buying-intent hints of one page (see the section comment above). Job titles are only
    read when `careers` is True (the page is the business's careers page).
    An absence signal is None ("can't tell") instead of False / [] when the page can't show
    it: a page built by JavaScript, scripts delayed by a speed plugin, or Tag Manager."""
    site = registered_domain(_host_of(page_url))
    refs = _refs_from(parser, page_url)
    code = _inline_code(parser)
    code_lower = code.lower()
    booking = _booking(refs, site)
    has_shop = _has_shop(parser, refs, code_lower, site)
    ordering = _has_ordering(refs, site)
    titles, roles = _job_titles_from(parser) if careers else ([], [])
    signals = {
        "has_analytics": _has_analytics(refs, code_lower),
        "has_search_console": bool(parser.meta.get("google-site-verification", "").strip()),
        "ad_pixels": _ad_pixels(refs, code, code_lower),
        "has_local_schema": _has_local_schema(parser),
        "has_online_booking": booking is not None,
        "booking_provider": booking,
        # A shop (Shopify, WooCommerce...) and an ordering platform (Toast, ChowNow,
        # DoorDash...) take the card payment online at their checkout.
        "has_online_payment": has_shop or ordering or _has_payment(refs, site),
        "has_shop": has_shop,
        "has_online_ordering": ordering,
        "has_pdf_forms": _has_pdf_forms(refs, site),
        "has_live_chat": _has_live_chat(parser, code_lower),
        "has_viewport": _has_viewport(parser, page_url),
        "hiring_roles": roles,
        "hiring_titles": titles,
    }
    # Tag Manager can load ads, chat and booking after the page opens (intent.py and the
    # dashboard read this to say "can't tell" instead of "missing").
    signals["has_tag_manager"] = _has_tag_manager(refs, code, code_lower)
    unknown: set[str] = set()
    if _built_by_javascript(parser):
        unknown.update(_ABSENCE_KEYS)
    if parser.scripts_delayed:
        unknown.update(_DELAYED_SCRIPT_KEYS)
    if signals["has_tag_manager"]:
        unknown.update(_TAG_MANAGER_KEYS)
    for key in unknown:
        if not signals[key]:  # something we did find stays found
            signals[key] = None
    return signals


def detect_intent_signals(html: str, page_url: str = "", careers: bool = False) -> dict:
    """{"has_analytics", "has_search_console", "ad_pixels", "has_local_schema",
    "has_online_booking", "booking_provider", "has_online_payment", "has_shop",
    "has_online_ordering", "has_pdf_forms", "has_live_chat", "has_viewport",
    "hiring_roles", "hiring_titles", "has_tag_manager"}"""
    return _intent_signals_from(_parse(html), page_url, careers)


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
    # A list of branches tells us how big the business is (the careers page is not a
    # candidate here: the crawler fetches it separately, at most once, for job titles).
    (40, ["locations", "our-locations", "offices", "our-offices", "branches"]),
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

def analyze_page(html: str, page_url: str, careers: bool = False) -> PageInfo:
    """Parse the page once and run every extractor on it. `careers` = this is the
    business's careers page (job titles are read from it)."""
    parser = _parse(html)
    return PageInfo(
        emails=_emails_from(parser, html),
        phones=_phones_from(parser),
        socials=_socials_from(parser, page_url),
        people=_people_from(parser, page_url),
        signals={**_signals_from(parser, html), **_size_signals_from(parser, page_url),
                 **_intent_signals_from(parser, page_url, careers)},
        links=_candidate_links(parser, page_url),
    )
