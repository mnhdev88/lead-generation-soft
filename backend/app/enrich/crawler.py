"""Visit a business website and collect emails, phones, social links, people and
"website signals" from its homepage plus a few likely contact/about/team pages (and the
careers page, when the page budget allows, to read the job titles).

    data = await crawl_website("example.com")
    if data.ok:
        print(data.emails, data.people)
    else:
        print(data.error)   # e.g. "Website did not respond"

Safety rules:
- Only http(s) URLs on normal web ports, and never hosts that resolve to private /
  internal IP addresses (checked again on every redirect), so a malicious "website"
  value can't make the server call itself or the local network. The client made by
  make_client() also connects only to the exact public address it checked, so a DNS
  server can't answer "public" to the check and "private" a moment later.
- robots.txt is respected for every page after the homepage (and the homepage too,
  if robots.txt forbids the whole site).
- Each page is capped at ~2 MB (counted after decompression) and PAGE_TIME_LIMIT
  seconds; a whole website gets SITE_TIME_BUDGET seconds.
- We send the headers a normal browser sends, and when a site refuses us (HTTP 403 /
  429) we retry once, politely. We never try to get around bot protection: no
  headless browser, no solving challenges, no fake cookies.
"""
from __future__ import annotations

import asyncio
import codecs
import ipaddress
import logging
import re
import socket
import zlib
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpcore
import httpx

from ..config import CRAWL_MAX_PAGES, CRAWL_TIMEOUT, CRAWL_USER_AGENT
from .extract import PersonFound, url_key, analyze_page, merge_people, rank_emails, registered_domain

log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024  # stop reading a page after 2 MB (after decompression)
MAX_REDIRECTS = 8
PAGE_CONCURRENCY = 3  # sub-pages of one website fetched at the same time
PAGE_TIME_LIMIT = CRAWL_TIMEOUT + 5  # hard stop for one page, even if it trickles in slowly
SITE_TIME_BUDGET = 45.0  # seconds for one whole website (all pages, retries and robots.txt)
BLOCKED_RETRY_DELAY = 2.0  # seconds to wait before retrying a homepage that said 403 / 429
CONTACT_PATHS = ("/contact", "/contact-us")  # tried directly when only the homepage is blocked
MAX_EMAILS = 30  # per website
MAX_PEOPLE = 50  # per website
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}  # normal web ports only (no port scanning)
ROBOTS_AGENT = "LeadGenSoft"  # name we match against "User-agent:" lines in robots.txt
# When a site answers on http://, one quick look at its https:// address (seconds), so a
# site that has a working certificate but doesn't redirect is not called "No SSL".
HTTPS_CHECK_TIME = 8.0
HTTPS_CHECK_BYTES = 64 * 1024

# The headers a normal browser sends when you open a page. We only advertise the
# compressions we can decode ourselves (brotli/zstd are not installed).
HEADERS = {
    "User-Agent": CRAWL_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


@dataclass
class WebsiteData:
    ok: bool
    error: str | None = None
    final_url: str | None = None
    emails: list[str] = field(default_factory=list)  # best first
    phones: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    people: list[PersonFound] = field(default_factory=list)
    # https, platform, copyright_year, has_contact_form, title, description,
    # pages_crawled, final_url, status_code, and the size hints: employees_stated,
    # employees_from, employees_snippet, founded_year, founded_snippet, locations_stated,
    # has_careers, careers_url, and the buying-intent hints: has_analytics,
    # has_search_console, ad_pixels, has_local_schema, has_online_booking, booking_provider,
    # has_online_payment, has_shop, has_online_ordering, has_pdf_forms, has_live_chat,
    # has_viewport, hiring_roles, hiring_titles
    signals: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

class _BlockedHost(Exception):
    pass


async def _resolve_host(host: str) -> list[str]:
    """Return the IP addresses a hostname points to. Tests replace this function
    (monkeypatch) so they don't need real DNS."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _is_public_ip(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text.split("%")[0])  # strip IPv6 zone like "%eth0"
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv6 addresses that carry an IPv4 address inside (::ffff:10.0.0.1, ::127.0.0.1,
        # 6to4, Teredo, NAT64) must also be checked by the IPv4 address they lead to.
        inner: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped:
            inner.append(ip.ipv4_mapped)
        if ip.sixtofour:
            inner.append(ip.sixtofour)
        if ip.teredo:
            inner.extend(ip.teredo)
        if int(ip) >> 32 == 0 or ip in _NAT64:  # ::a.b.c.d and 64:ff9b::a.b.c.d
            inner.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(not _is_public_ip(str(v4)) for v4 in inner):
            return False
        if ip.is_site_local:  # fec0::/10: deprecated, but still used on some internal networks
            return False
    return ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local
                                 or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


is_public_ip = _is_public_ip  # also used by the email checker (app/emails/smtp.py)


async def _public_ips(host: str) -> list[str]:
    """The addresses of `host`, or _BlockedHost if any of them is not public.
    Raises socket.gaierror if the name doesn't exist."""
    try:
        ips = [str(ipaddress.ip_address(host.strip("[]")))]  # the host is already an IP address
    except ValueError:
        ips = await _resolve_host(host)
    if not ips:
        raise socket.gaierror(f"No address for {host}")
    if not all(_is_public_ip(ip) for ip in ips):
        raise _BlockedHost("Website address points to a private network")
    return list(dict.fromkeys(ips))  # without duplicates, order kept


async def _check_url_is_safe(url: str, dns_cache: dict[str, bool]) -> None:
    """Raise _BlockedHost (or socket.gaierror if the name doesn't exist)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _BlockedHost("Only http(s) website addresses are supported")
    if parsed.port not in ALLOWED_PORTS:
        raise _BlockedHost("Only normal web ports are supported")
    host = parsed.hostname.lower().rstrip(".")
    if host not in dns_cache:
        try:
            await _public_ips(host)
            dns_cache[host] = True
        except _BlockedHost:
            dns_cache[host] = False
    if not dns_cache[host]:
        raise _BlockedHost("Website address points to a private network")


class _SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """Opens TCP connections only to public IP addresses, and connects to exactly the
    address it just checked. (The TLS certificate is still checked against the host
    name, because httpcore passes the name separately for that.)"""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None):
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        error: Exception | None = None
        for ip in await _public_ips(host):
            try:
                return await self._inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc  # try the website's next address
        raise error or httpcore.ConnectError(f"Could not connect to {host}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise _BlockedHost("Unix sockets are not allowed")

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


class _SafeTransport(httpx.AsyncHTTPTransport):
    """httpx's normal transport, but every connection goes through _SafeNetworkBackend."""

    def __init__(self, limits: httpx.Limits):
        ssl_context = httpx.create_ssl_context()  # normal certificate checks
        super().__init__(verify=ssl_context, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=_SafeNetworkBackend(),
        )


# ---------------------------------------------------------------------------
# Fetching one URL
# ---------------------------------------------------------------------------

@dataclass
class _Fetched:
    url: str  # final URL after redirects
    status: int | None = None
    text: str | None = None
    error: str | None = None  # human readable
    kind: str | None = None  # connect|ssl|timeout|dns|blocked|http|not_html|redirects|other
    challenge: bool = False  # the site answered with a bot-check page (e.g. Cloudflare "Just a moment...")


@dataclass
class _Session:
    """What every request of one website crawl shares."""
    client: httpx.AsyncClient
    deadline: float  # event-loop time when the whole website must be finished
    dns_cache: dict[str, bool] = field(default_factory=dict)

    def time_left(self) -> float:
        return self.deadline - asyncio.get_running_loop().time()


class _Unreadable(Exception):
    """The body uses a compression we don't support."""


async def _read_body(resp: httpx.Response, max_bytes: int) -> bytes:
    """Read at most `max_bytes` of the body, counted AFTER decompression. We unzip
    gzip/deflate ourselves with an output limit, so a tiny "zip bomb" can't grow into
    gigabytes of memory."""
    if resp.is_stream_consumed:  # already in memory (only happens with test transports)
        return resp.content[:max_bytes]
    encoding = resp.headers.get("content-encoding", "").strip().lower()
    if encoding in ("", "identity"):
        inflater = None
    elif encoding in ("gzip", "x-gzip"):
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        inflater = zlib.decompressobj()
    else:
        raise _Unreadable(encoding)

    body = bytearray()
    raw_total = 0
    first_chunk = True
    try:
        async for raw in resp.aiter_raw():
            raw_total += len(raw)
            if inflater is None:
                body += raw[: max_bytes - len(body)]
            else:
                if first_chunk and encoding == "deflate":
                    try:  # "deflate" should have a zlib header, but some servers send it raw
                        zlib.decompressobj().decompress(raw[:64])
                    except zlib.error:
                        inflater = zlib.decompressobj(-zlib.MAX_WBITS)
                data = raw
                while data and len(body) < max_bytes:
                    piece = inflater.decompress(data, max_bytes - len(body))
                    body += piece
                    data = inflater.unconsumed_tail
                    if not piece:
                        break  # needs more input
            first_chunk = False
            # Stop at the limit; also stop if lots of compressed data gives (almost) nothing.
            if len(body) >= max_bytes or raw_total >= max_bytes:
                break
    except zlib.error:
        pass  # corrupt compressed data: keep what we could read
    return bytes(body)


def _sniff_charset(body: bytes, declared: str | None) -> str | None:
    """The character encoding of a page: from the HTTP header, else from a
    <meta charset> tag near the top. None if neither says (or names a codec we lack)."""
    encoding = declared
    if not encoding:
        match = re.search(rb"<meta[^>]{0,200}?charset\s*=\s*[\"']?\s*([\w\-:.]{1,40})", body[:4096], re.I)
        encoding = match.group(1).decode("ascii", "ignore") if match else None
        # A page whose <meta> we could read as ASCII can't really be UTF-16 (HTML rule).
        if encoding and encoding.lower().replace("_", "-").startswith(("utf-16", "utf-32")):
            encoding = "utf-8"
    if not encoding:
        return None
    try:
        name = codecs.lookup(encoding.strip()).name
    except LookupError:
        return None
    # Browsers read "Latin-1" / "ASCII" pages as Windows-1252 (adds curly quotes, € etc.).
    return "cp1252" if name in ("latin-1", "iso8859-1", "ascii") else name


def _decode(body: bytes, response: httpx.Response) -> str:
    """Bytes -> text. Order: byte-order mark, declared charset, valid UTF-8, Windows-1252."""
    if body.startswith(codecs.BOM_UTF8):
        return body[3:].decode("utf-8", errors="replace")
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return body.decode("utf-16", errors="replace")
    try:
        declared = response.charset_encoding
    except Exception:
        declared = None
    encoding = _sniff_charset(body, declared)
    if encoding:
        try:
            return body.decode(encoding, errors="replace")
        except (LookupError, ValueError):  # not a text encoding (e.g. "base64")
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(body) - 3:  # only the last character was cut off by the size cap
            return body.decode("utf-8", errors="replace")
    return body.decode("cp1252", errors="replace")


def _http_error(status: int) -> str:
    if status in (401, 403):
        return f"Blocked (HTTP {status})"
    if status == 404:
        return "Page not found (HTTP 404)"
    if status == 429:
        return "Too many requests (HTTP 429)"
    if status >= 500:
        return f"Website error (HTTP {status})"
    return f"HTTP {status}"


async def _fetch(session: _Session, url: str, html_only: bool = True, max_bytes: int | None = None,
                 referer: str | None = None) -> _Fetched:
    """GET a URL, following redirects by hand so every hop passes the SSRF check.
    Never raises: problems come back as _Fetched.error."""
    max_bytes = max_bytes or MAX_BYTES
    time_left = session.time_left()
    if time_left <= 0:
        return _Fetched(url, error="Website timed out", kind="timeout")
    headers = dict(HEADERS)
    if referer:  # a link clicked on the site's own page
        headers.update({"Referer": referer, "Sec-Fetch-Site": "same-origin"})
    try:
        async with asyncio.timeout(min(PAGE_TIME_LIMIT, time_left)):
            for _ in range(MAX_REDIRECTS + 1):
                await _check_url_is_safe(url, session.dns_cache)
                async with session.client.stream("GET", url, headers=headers, timeout=CRAWL_TIMEOUT,
                                                 follow_redirects=False) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(str(resp.url), resp.headers["location"].strip())
                        continue
                    if resp.status_code >= 400:
                        challenge = resp.headers.get("cf-mitigated", "").lower() == "challenge"
                        return _Fetched(str(resp.url), resp.status_code, error=_http_error(resp.status_code),
                                        kind="http", challenge=challenge)
                    content_type = resp.headers.get("content-type", "").lower()
                    if html_only and content_type and "html" not in content_type:
                        return _Fetched(str(resp.url), resp.status_code, error="Not an HTML page", kind="not_html")
                    body = await _read_body(resp, max_bytes)
                    return _Fetched(str(resp.url), resp.status_code, text=_decode(body, resp))
            return _Fetched(url, error="Too many redirects", kind="redirects")
    except _BlockedHost as exc:
        return _Fetched(url, error=f"Blocked: {exc}", kind="blocked")
    except _Unreadable:
        return _Fetched(url, error="Could not read website (unsupported compression)", kind="other")
    except socket.gaierror:
        return _Fetched(url, error="Website address not found", kind="dns")
    except (httpx.TimeoutException, TimeoutError):
        return _Fetched(url, error="Website timed out", kind="timeout")
    except httpx.ConnectError as exc:
        if "ssl" in str(exc).lower() or "certificate" in str(exc).lower():
            return _Fetched(url, error="SSL error", kind="ssl")
        return _Fetched(url, error="Website did not respond", kind="connect")
    except httpx.TooManyRedirects:
        return _Fetched(url, error="Too many redirects", kind="redirects")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError) as exc:
        log.debug("fetch %s failed: %r", url, exc)
        return _Fetched(url, error="Website did not respond", kind="other")


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

class _Robots:
    """Loads robots.txt once per host and answers "may we fetch this URL?" with:
    "allow"       - yes (also when there is no robots.txt, as the standard says);
    "disallow"    - robots.txt forbids it;
    "unavailable" - robots.txt gave a server error / timed out, so we don't know:
                    we then read only the homepage we already have, nothing more."""

    def __init__(self, session: _Session):
        self.session = session
        self.rules: dict[str, RobotFileParser | str | None] = {}
        self.lock = asyncio.Lock()  # so parallel page fetches don't each download robots.txt

    async def check(self, url: str) -> str:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        async with self.lock:
            if origin not in self.rules:
                fetched = await _fetch(self.session, origin + "/robots.txt", html_only=False,
                                       max_bytes=512 * 1024)
                rules: RobotFileParser | str | None = None
                if fetched.text is not None and fetched.status == 200:
                    rules = RobotFileParser()
                    rules.parse(fetched.text.splitlines())
                elif (fetched.status or 0) >= 500 or fetched.status == 429 or fetched.kind in ("timeout", "connect"):
                    rules = "unavailable"
                self.rules[origin] = rules
        rules = self.rules[origin]
        if rules is None:
            return "allow"
        if rules == "unavailable":
            return "unavailable"
        return "allow" if rules.can_fetch(ROBOTS_AGENT, url) else "disallow"


# ---------------------------------------------------------------------------
# The crawl
# ---------------------------------------------------------------------------

def _normalize_start_url(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    url = url.split()[0]  # "example.com  (main site)" -> "example.com"
    if "://" not in url:
        if re.match(r"^[a-zA-Z][\w+.\-]*:(?!\d)", url):
            return None  # another scheme such as "javascript:" or "mailto:"
        url = "https://" + url.lstrip("/")
    try:
        parsed = urlparse(url)
        parsed.port  # raises ValueError for a malformed port
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/",
                       parsed.params, parsed.query, ""))


def _empty_signals() -> dict:
    # https is None = "unknown": a site we could not reach must not be shown as "No SSL".
    return {"https": None, "platform": None, "copyright_year": None, "has_contact_form": False,
            "title": None, "description": None, "pages_crawled": 0, "final_url": None,
            "status_code": None,
            # Size hints the site states about itself (see extract._size_signals_from)
            "employees_stated": None, "employees_from": None, "employees_snippet": None,
            "founded_year": None, "founded_snippet": None, "locations_stated": None,
            "has_careers": False, "careers_url": None,
            # Buying-intent hints (see extract._intent_signals_from). has_viewport is None
            # ("unknown") until the homepage was read.
            "has_analytics": False, "has_search_console": False, "ad_pixels": [],
            "has_local_schema": False, "has_online_booking": False, "booking_provider": None,
            "has_online_payment": False, "has_shop": False, "has_online_ordering": False,
            "has_pdf_forms": False, "has_live_chat": False, "has_viewport": None,
            "has_tag_manager": False,
            "hiring_roles": [], "hiring_titles": []}


# Intent hints: True on any page = True for the website; lists are joined.
_INTENT_BOOL_KEYS = ("has_analytics", "has_search_console", "has_local_schema", "has_online_booking",
                     "has_online_payment", "has_shop", "has_online_ordering", "has_pdf_forms",
                     "has_live_chat")
_INTENT_LIST_CAPS = {"ad_pixels": 10, "hiring_roles": 20, "hiring_titles": 10}


def _merge_intent_signals(signals: dict, page_signals: list[dict]) -> None:
    """Combine the intent hints of all pages (homepage first). Booleans: True when any page
    says True, else False when at least one page could tell (said False), else None
    ("can't tell", e.g. every page was built by JavaScript). Lists: joined, sorted, capped;
    ad_pixels is None when no page could tell. Booking provider: the first one found.
    has_viewport: from the homepage only (it says whether the site is built for phones)."""
    for key in _INTENT_BOOL_KEYS:
        values = [p.get(key) for p in page_signals]
        if any(v is True for v in values):
            signals[key] = True
        elif any(v is False for v in values):
            signals[key] = False
        else:
            signals[key] = None
    for key, cap in _INTENT_LIST_CAPS.items():
        values = {v for p in page_signals for v in (p.get(key) or []) if isinstance(v, str)}
        signals[key] = sorted(values)[:cap]
    if not signals["ad_pixels"] and not any(isinstance(p.get("ad_pixels"), list) for p in page_signals):
        signals["ad_pixels"] = None
    signals["booking_provider"] = next((p["booking_provider"] for p in page_signals
                                        if p.get("booking_provider")), None)
    signals["has_tag_manager"] = any(p.get("has_tag_manager") is True for p in page_signals)
    home = page_signals[0] if page_signals else {}
    signals["has_viewport"] = home.get("has_viewport") if isinstance(home.get("has_viewport"), bool) else None


def _merge_size_signals(signals: dict, page_signals: list[dict]) -> None:
    """Combine the size hints of all pages (homepage first, then sub-pages in crawl order).
    Headcount: the first one stated in the text; JSON-LD only when no page text states
    one (JSON-LD is often copied from a template). Founded year: the first one found.
    Locations: the largest number. Careers: any page with a careers link (first URL)."""
    for key in ("employees_stated", "employees_from", "employees_snippet", "founded_year",
                "founded_snippet", "locations_stated", "careers_url"):
        signals[key] = None
    signals["has_careers"] = False
    for source in ("text", "jsonld"):
        page = next((p for p in page_signals
                     if p.get("employees_stated") and p.get("employees_from") == source), None)
        if page:
            for key in ("employees_stated", "employees_from", "employees_snippet"):
                signals[key] = page.get(key)
            break
    page = next((p for p in page_signals if p.get("founded_year")), None)
    if page:
        signals["founded_year"], signals["founded_snippet"] = page["founded_year"], page.get("founded_snippet")
    signals["locations_stated"] = max((p["locations_stated"] for p in page_signals if p.get("locations_stated")),
                                      default=None)
    for page in page_signals:
        if page.get("has_careers"):
            signals["has_careers"] = True
            signals["careers_url"] = signals["careers_url"] or page.get("careers_url")


def _careers_key(careers_url: str | None, home_domain: str) -> str | None:
    """url_key of a careers page on this website (job sites such as greenhouse.io are
    never fetched), or None."""
    if not careers_url:
        return None
    careers_url = urldefrag(careers_url)[0]
    try:
        careers_host = urlparse(careers_url).hostname
    except ValueError:
        return None
    if not careers_url.startswith(("http://", "https://")) or registered_domain(careers_host) != home_domain:
        return None
    return url_key(careers_url)


def _failure(error: str, fetched: _Fetched | None = None) -> WebsiteData:
    signals = _empty_signals()
    if fetched and fetched.status is not None:
        # The server did answer (e.g. with 403), so we know the status and the scheme it used.
        signals["status_code"] = fetched.status
        signals["https"] = fetched.url.startswith("https://")
    return WebsiteData(ok=False, error=error, signals=signals)


def _other_host(url: str) -> str:
    """'https://www.shop.com/' <-> 'https://shop.com/'"""
    parsed = urlparse(url)
    host = parsed.netloc
    host = host[4:] if host.lower().startswith("www.") else "www." + host
    return urlunparse((parsed.scheme, host, parsed.path or "/", parsed.params, parsed.query, ""))


async def _retry_blocked_homepage(session: _Session, robots: _Robots, blocked: _Fetched) -> _Fetched:
    """The homepage said 403 (forbidden) or 429 (too many requests). Many small sites
    answer fine on their other host name (www. or not) or on their contact page, so
    we try those once, after a short pause. Returns the first page that works, or the
    original error. We don't try anything that would get around real bot protection."""
    await asyncio.sleep(BLOCKED_RETRY_DELAY)
    site = registered_domain(urlparse(blocked.url).hostname)
    retry = await _fetch(session, _other_host(blocked.url))
    if not retry.error and registered_domain(urlparse(retry.url).hostname) == site:
        return retry
    parsed = urlparse(blocked.url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for path in CONTACT_PATHS:
        if await robots.check(origin + path) != "allow":
            continue
        page = await _fetch(session, origin + path)
        if not page.error and registered_domain(urlparse(page.url).hostname) == site:
            return page
    return blocked


async def _https_works(session: _Session, http_url: str) -> bool | None:
    """The homepage answered on http://. Does the same address answer on https:// with a
    valid certificate? True / False, or None when we can't tell (timeout, out of time)."""
    loop = asyncio.get_running_loop()
    deadline = min(session.deadline, loop.time() + HTTPS_CHECK_TIME)
    if deadline <= loop.time():
        return None
    probe = _Session(session.client, deadline, session.dns_cache)
    fetched = await _fetch(probe, "https://" + http_url[len("http://"):], html_only=False,
                           max_bytes=HTTPS_CHECK_BYTES)
    if fetched.status is not None:
        # Any answer (even 404) came over a valid certificate. If it sent us back to
        # http://, visitors never get the secure version.
        return fetched.url.startswith("https://")
    if fetched.kind in ("ssl", "connect"):
        return False  # bad / missing certificate, or nothing listening on https
    return None


async def _crawl(url: str, client: httpx.AsyncClient, max_pages: int) -> WebsiteData:
    start = _normalize_start_url(url)
    if not start:
        return _failure("Invalid website address")
    session = _Session(client, asyncio.get_running_loop().time() + SITE_TIME_BUDGET)
    robots = _Robots(session)

    # 1. Homepage (fall back to http:// if https:// can't connect or has a bad certificate)
    home = await _fetch(session, start)
    if home.error and home.kind in ("connect", "ssl") and start.startswith("https://"):
        retry = await _fetch(session, "http://" + start[len("https://"):])
        if not retry.error:
            home = retry
    if home.status in (403, 429) and not home.challenge:
        # (A bot-check page means the whole site is protected: we don't retry those.)
        home = await _retry_blocked_homepage(session, robots, home)
    if home.error or home.text is None:
        return _failure(home.error or "Website did not respond", home)

    if await robots.check(home.url) == "disallow":
        return _failure("Blocked by robots.txt", home)

    home_domain = registered_domain(urlparse(home.url).hostname)
    # Parsing is CPU work; a thread keeps other crawls running meanwhile.
    pages = [(home.url, await asyncio.to_thread(analyze_page, home.text, home.url))]
    visited = {url_key(start), url_key(home.url)}
    # We were given an http:// address (often what Google lists) and the site answered there
    # without sending us to https://. Many sites work on both: check https:// once, so a
    # site with a working certificate is not called "No SSL".
    https: bool | None = home.url.startswith("https://")
    if not https and start.startswith("http://"):
        https = await _https_works(session, home.url)
    # The careers page the homepage links to, read with its job titles when it is also one
    # of the pages below (e.g. /join-our-team, which looks like a "team" page).
    careers_key = _careers_key(pages[0][1].signals.get("careers_url"), home_domain)
    # Page text by url_key, so a page fetched before we knew it was the careers page can be
    # read again for job titles without downloading it twice.
    texts: dict[str, tuple[str, str]] = {}

    # 2. Likely contact / about / team pages: up to two rounds (homepage links, then
    #    links found on those pages, e.g. About -> Our Team).
    queue = list(pages[0][1].links)
    budget = max(0, max_pages - 1)
    semaphore = asyncio.Semaphore(PAGE_CONCURRENCY)

    async def fetch_page(page_url: str, careers: bool = False):
        async with semaphore:
            if await robots.check(page_url) != "allow":
                return None
            fetched = await _fetch(session, page_url, referer=home.url)
        if fetched.error or fetched.text is None:
            return None
        if registered_domain(urlparse(fetched.url).hostname) != home_domain:
            return None  # redirected to another website
        texts[url_key(page_url)] = (fetched.url, fetched.text)
        return fetched.url, await asyncio.to_thread(analyze_page, fetched.text, fetched.url, careers)

    for _round in range(2):
        batch: list[str] = []
        for candidate in queue:
            key = url_key(candidate)
            if len(batch) >= budget:
                break
            if key not in visited and registered_domain(urlparse(candidate).hostname) == home_domain:
                visited.add(key)
                batch.append(candidate)
        if not batch or session.time_left() <= 0:
            break
        budget -= len(batch)
        results = await asyncio.gather(*(fetch_page(u, careers=url_key(u) == careers_key) for u in batch))
        queue = []
        for result in results:
            if result:
                pages.append(result)
                queue.extend(result[1].links)

    # 3. The careers page (on this website only; job sites such as greenhouse.io are not
    #    fetched), as one extra page when the page budget allows: its job titles show
    #    which roles the business is hiring for.
    careers_url = next((info.signals["careers_url"] for _page_url, info in pages
                        if info.signals.get("careers_url")), None)
    found_key = _careers_key(careers_url, home_domain)
    if found_key and found_key != careers_key and found_key in texts:
        # Already fetched as an ordinary page (found on a sub-page, after the homepage):
        # read it again for its job titles. No extra request.
        page_url, text = texts[found_key]
        info = await asyncio.to_thread(analyze_page, text, page_url, True)
        pages = [(u, info) if url_key(u) == url_key(page_url) else (u, i) for u, i in pages]
    elif found_key and found_key not in visited and budget > 0 and session.time_left() > 0:
        visited.add(found_key)
        result = await fetch_page(urldefrag(careers_url)[0], careers=True)
        if result:
            pages.append(result)

    # 4. Merge everything, homepage first
    emails: list[str] = []
    seen_emails: set[str] = set()
    phones: list[str] = []
    phone_keys: set[str] = set()
    socials: dict[str, str] = {}
    people: list[PersonFound] = []
    for _page_url, info in pages:
        for email in info.emails:
            if email not in seen_emails:
                seen_emails.add(email)
                emails.append(email)
        for phone in info.phones:
            key = re.sub(r"\D", "", phone)[-10:]
            if key not in phone_keys:
                phone_keys.add(key)
                phones.append(phone)
        for network, link in info.socials.items():
            socials.setdefault(network, link)
        people += info.people

    home_signals = pages[0][1].signals
    signals = _empty_signals()
    signals.update(home_signals)
    for _page_url, info in pages[1:]:  # fill gaps from sub-pages
        for key in ("platform", "copyright_year", "description"):
            if not signals.get(key) and info.signals.get(key):
                signals[key] = info.signals[key]
        if info.signals.get("has_contact_form"):
            signals["has_contact_form"] = True
    _merge_size_signals(signals, [info.signals for _page_url, info in pages])
    _merge_intent_signals(signals, [info.signals for _page_url, info in pages])
    signals.update({
        "https": https,
        "pages_crawled": len(pages),
        "final_url": home.url,
        "status_code": home.status,
    })

    return WebsiteData(
        ok=True,
        final_url=home.url,
        emails=rank_emails(emails, home.url)[:MAX_EMAILS],
        phones=phones[:10],
        socials=socials,
        people=merge_people(people)[:MAX_PEOPLE],
        signals=signals,
    )


def make_client() -> httpx.AsyncClient:
    """The HTTP client to use for crawling (share one between many crawl_website calls).
    Its connections can only reach public IP addresses (see _SafeNetworkBackend)."""
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    return httpx.AsyncClient(
        transport=_SafeTransport(limits),
        follow_redirects=False,  # _fetch follows redirects itself, checking each hop
        timeout=CRAWL_TIMEOUT,
        headers=HEADERS,
    )


_make_client = make_client  # tests replace this one


async def crawl_website(url: str, client: httpx.AsyncClient | None = None,
                        max_pages: int = CRAWL_MAX_PAGES) -> WebsiteData:
    """Crawl one website. Never raises for network/HTTP problems: returns ok=False with a
    short error message instead. Pass a shared `client` (from make_client()) when
    crawling many sites."""
    try:
        # Each request already stops at the site's deadline; this is a last safety net
        # (it also covers time spent parsing pages).
        async with asyncio.timeout(SITE_TIME_BUDGET + 30):
            if client is None:
                async with _make_client() as own_client:
                    return await _crawl(url, own_client, max_pages)
            return await _crawl(url, client, max_pages)
    except TimeoutError:
        return _failure("Website timed out")
    except Exception:  # last-resort safety net so one strange site can't stop a whole job
        log.exception("crawl of %s failed", url)
        return _failure("Could not read website")
