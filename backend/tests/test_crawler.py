"""Tests for app/enrich/crawler.py. No real network: every request goes to an
httpx.MockTransport, and DNS lookups go to a fake resolver."""
import asyncio
import gzip
import time
import zlib

import httpcore
import httpx
import pytest

from app.enrich import PersonFound, WebsiteData, crawl_website
from app.enrich import crawler

PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Every hostname resolves to a public IP, except a few 'internal' names."""
    async def resolve(host: str) -> list[str]:
        internal = {"intranet.acme.com": ["10.0.0.5"], "sneaky.example": [PUBLIC_IP, "127.0.0.1"]}
        return internal.get(host, [PUBLIC_IP])

    monkeypatch.setattr(crawler, "_resolve_host", resolve)


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    """Don't wait 2 seconds before retrying a blocked homepage in tests."""
    monkeypatch.setattr(crawler, "BLOCKED_RETRY_DELAY", 0)


def html_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def make_site(pages: dict[str, httpx.Response | str], requested: list[str] | None = None):
    """Build a MockTransport from {"https://host/path": response-or-html}. Unknown URLs -> 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if requested is not None:
            requested.append(url)
        page = pages.get(url)
        if page is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        if isinstance(page, Exception):
            raise page
        return html_response(page) if isinstance(page, str) else page

    return httpx.MockTransport(handler)


def crawl(transport: httpx.MockTransport, url: str, **kwargs) -> WebsiteData:
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await crawl_website(url, client=client, **kwargs)

    return asyncio.run(run())


HOME = """
<html><head><title>Acme Plumbing | Austin TX</title>
<meta name="description" content="24/7 emergency plumbing.">
<link rel="stylesheet" href="/wp-content/themes/acme/style.css"></head>
<body>
<nav><a href="/">Home</a><a href="/about-us/">About</a><a href="/contact/">Contact</a>
<a href="/services/">Services</a><a href="https://facebook.com/acmeplumbing">Facebook</a></nav>
<p>Call <a href="tel:+15125550142">(512) 555-0142</a></p>
<footer>Site by <a href="mailto:hello@webdesignco.com">WebDesignCo</a> &copy; 2019 Acme Plumbing</footer>
</body></html>
"""

CONTACT = """
<html><body><h1>Contact Us</h1>
<p>Email <a href="mailto:info@acmeplumbing.com">info@acmeplumbing.com</a> or mike [at] acmeplumbing [dot] com</p>
<form><input type="email" name="email"><textarea name="msg"></textarea></form>
<a href="/our-team/">Meet the team</a>
</body></html>
"""

ABOUT = """
<html><body><h1>About Acme</h1>
<div class="person"><h3>Mike Johnson</h3><p>Owner</p>
<a href="https://www.linkedin.com/in/mike-johnson-acme">LinkedIn</a></div>
<p>Owner-operated plumbing company. Office line 512-555-0199.</p>
</body></html>
"""

TEAM = """
<html><body><div class="member"><h3>Rosa Diaz</h3><p>Office Manager</p></div></body></html>
"""


def test_crawl_finds_contact_pages_and_merges_results():
    requested: list[str] = []
    transport = make_site({
        "https://acmeplumbing.com/": HOME,
        "https://acmeplumbing.com/contact/": CONTACT,
        "https://acmeplumbing.com/about-us/": ABOUT,
        "https://acmeplumbing.com/our-team/": TEAM,
    }, requested)

    data = crawl(transport, "acmeplumbing.com")

    assert data.ok and data.error is None
    assert data.final_url == "https://acmeplumbing.com/"
    # same-domain role address first, then personal, then the web designer's address
    assert data.emails == ["info@acmeplumbing.com", "mike@acmeplumbing.com", "hello@webdesignco.com"]
    assert data.phones == ["+15125550142", "512-555-0199"]
    assert data.socials == {"facebook": "https://www.facebook.com/acmeplumbing",
                            "linkedin": "https://www.linkedin.com/in/mike-johnson-acme"}
    people = {p.full_name: p for p in data.people}
    assert set(people) == {"Mike Johnson", "Rosa Diaz"}  # Rosa found on a 2nd-level page
    assert people["Mike Johnson"].title == "Owner"
    assert people["Mike Johnson"].linkedin_url == "https://www.linkedin.com/in/mike-johnson-acme"
    assert people["Mike Johnson"].source_url == "https://acmeplumbing.com/about-us/"
    assert isinstance(data.people[0], PersonFound)

    s = data.signals
    assert s["https"] is True
    assert s["platform"] == "WordPress"
    assert s["copyright_year"] == 2019
    assert s["has_contact_form"] is True  # found on the contact page
    assert s["title"] == "Acme Plumbing | Austin TX"
    assert s["description"] == "24/7 emergency plumbing."
    assert s["pages_crawled"] == 4
    assert s["final_url"] == "https://acmeplumbing.com/"
    assert s["status_code"] == 200
    # contact page is fetched before about, and the services page is never fetched
    assert requested.index("https://acmeplumbing.com/contact/") < requested.index("https://acmeplumbing.com/about-us/")
    assert "https://acmeplumbing.com/services/" not in requested


def test_max_pages_is_respected():
    requested: list[str] = []
    transport = make_site({
        "https://acmeplumbing.com/": HOME,
        "https://acmeplumbing.com/contact/": CONTACT,
        "https://acmeplumbing.com/about-us/": ABOUT,
        "https://acmeplumbing.com/our-team/": TEAM,
    }, requested)
    data = crawl(transport, "https://acmeplumbing.com", max_pages=2)
    assert data.signals["pages_crawled"] == 2
    assert [u for u in requested if not u.endswith("robots.txt")] == [
        "https://acmeplumbing.com/", "https://acmeplumbing.com/contact/"]


def test_robots_txt_disallow_is_respected():
    requested: list[str] = []
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /contact\n", headers={"content-type": "text/plain"})
    transport = make_site({
        "https://acmeplumbing.com/": HOME,
        "https://acmeplumbing.com/robots.txt": robots,
        "https://acmeplumbing.com/contact/": CONTACT,
        "https://acmeplumbing.com/about-us/": ABOUT,
    }, requested)
    data = crawl(transport, "acmeplumbing.com")
    assert data.ok
    assert "https://acmeplumbing.com/contact/" not in requested
    assert "https://acmeplumbing.com/about-us/" in requested
    assert "info@acmeplumbing.com" not in data.emails
    assert requested.count("https://acmeplumbing.com/robots.txt") == 1  # fetched once


def test_robots_txt_blocking_whole_site():
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /\n", headers={"content-type": "text/plain"})
    transport = make_site({"https://acmeplumbing.com/": HOME, "https://acmeplumbing.com/robots.txt": robots})
    data = crawl(transport, "acmeplumbing.com")
    assert not data.ok
    assert data.error == "Blocked by robots.txt"
    assert data.emails == []


def test_redirect_to_www_and_https():
    redirect = httpx.Response(301, headers={"location": "https://www.acmeplumbing.com/"})
    transport = make_site({
        "http://acmeplumbing.com/": redirect,
        "https://www.acmeplumbing.com/": HOME,
        "https://www.acmeplumbing.com/contact/": CONTACT,
    })
    data = crawl(transport, "http://acmeplumbing.com")
    assert data.ok
    assert data.final_url == "https://www.acmeplumbing.com/"
    assert data.signals["https"] is True
    assert data.emails[0] == "info@acmeplumbing.com"


def test_http_only_site_falls_back_from_https():
    transport = make_site({
        "https://oldsite.pk/": httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
        "http://oldsite.pk/": "<html><body>Email: owner@oldsite.pk</body></html>",
    })
    data = crawl(transport, "oldsite.pk")
    assert data.ok
    assert data.final_url == "http://oldsite.pk/"
    assert data.signals["https"] is False
    assert data.emails == ["owner@oldsite.pk"]


def test_non_html_homepage_and_subpages_are_skipped():
    pdf = httpx.Response(200, content=b"%PDF-1.4 info@acme.com", headers={"content-type": "application/pdf"})
    data = crawl(make_site({"https://acme.com/": pdf}), "acme.com")
    assert not data.ok
    assert data.error == "Not an HTML page"

    home = '<a href="/contact.php">Contact</a><p>sales@acme.com</p>'
    transport = make_site({"https://acme.com/": home, "https://acme.com/contact.php": pdf})
    data = crawl(transport, "acme.com")
    assert data.ok
    assert data.emails == ["sales@acme.com"]
    assert data.signals["pages_crawled"] == 1


@pytest.mark.parametrize("failure, message", [
    (httpx.ConnectError("Connection refused"), "Website did not respond"),
    (httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number"), "SSL error"),
    (httpx.ReadTimeout("timed out"), "Website timed out"),
    (httpx.Response(403, text="Forbidden", headers={"content-type": "text/html"}), "Blocked (HTTP 403)"),
    (httpx.Response(503, text="Down", headers={"content-type": "text/html"}), "Website error (HTTP 503)"),
])
def test_errors_return_ok_false(failure, message):
    pages = {"https://broken.com/": failure}
    if isinstance(failure, httpx.ConnectError):
        pages["http://broken.com/"] = httpx.ConnectError("Connection refused")
    data = crawl(make_site(pages), "broken.com")
    assert data.ok is False
    assert data.error == message
    assert data.signals["pages_crawled"] == 0


def test_subpage_failure_keeps_homepage_results():
    transport = make_site({
        "https://acmeplumbing.com/": HOME,
        "https://acmeplumbing.com/contact/": httpx.ReadTimeout("slow"),
        "https://acmeplumbing.com/about-us/": httpx.Response(500, text="oops", headers={"content-type": "text/html"}),
    })
    data = crawl(transport, "acmeplumbing.com")
    assert data.ok
    assert data.emails == ["hello@webdesignco.com"]
    assert data.signals["pages_crawled"] == 1


def test_too_many_redirects():
    loop = httpx.Response(302, headers={"location": "https://loop.com/"})
    data = crawl(make_site({"https://loop.com/": loop}), "loop.com")
    assert not data.ok
    assert data.error == "Too many redirects"


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://10.1.2.3:8080/admin",
    "http://[::1]/",
    "http://169.254.169.254/latest/meta-data/",
    "https://intranet.acme.com/",
    "https://sneaky.example/",
])
def test_private_addresses_are_refused(url):
    requested: list[str] = []
    data = crawl(make_site({url: "<p>secret@internal.com</p>"}, requested), url)
    assert data.ok is False
    assert data.error.startswith("Blocked")
    assert requested == []  # no request was even sent


def test_redirect_to_private_address_is_refused():
    requested: list[str] = []
    redirect = httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    transport = make_site({"https://evil.com/": redirect,
                           "http://169.254.169.254/latest/meta-data/": "<p>root@aws.com</p>"}, requested)
    data = crawl(transport, "evil.com")
    assert not data.ok
    assert data.error.startswith("Blocked")
    assert requested == ["https://evil.com/"]


def test_invalid_urls():
    transport = make_site({})
    assert crawl(transport, "").error == "Invalid website address"
    assert crawl(transport, "ftp://files.acme.com").error == "Invalid website address"
    assert crawl(transport, "javascript:alert(1)").error == "Invalid website address"


def test_links_to_other_domains_are_not_followed():
    requested: list[str] = []
    home = '<a href="https://partner-site.com/contact">Contact our partner</a><a href="/contact">Contact</a>'
    transport = make_site({"https://acme.com/": home, "https://acme.com/contact": "<p>info@acme.com</p>"}, requested)
    data = crawl(transport, "acme.com")
    assert data.emails == ["info@acme.com"]
    assert not any("partner-site.com" in u for u in requested)


def test_dns_failure(monkeypatch):
    import socket

    async def no_such_host(host):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(crawler, "_resolve_host", no_such_host)
    data = crawl(make_site({}), "does-not-exist-12345.com")
    assert not data.ok
    assert data.error == "Website address not found"


def test_large_page_is_capped(monkeypatch):
    monkeypatch.setattr(crawler, "MAX_BYTES", 10_000)  # smaller cap keeps the test fast
    body = "<p>info@bigpage.com</p>" + ("<p>filler</p>" * 20_000) + "<p>late@bigpage.com</p>"
    data = crawl(make_site({"https://bigpage.com/": body}), "bigpage.com")
    assert data.ok
    assert data.emails == ["info@bigpage.com"]  # the part after the cap was never read


def test_creates_its_own_client_when_none_given(monkeypatch):
    transport = make_site({"https://acme.com/": "<title>Acme</title><p>info@acme.com</p>"})
    monkeypatch.setattr(crawler, "_make_client", lambda: httpx.AsyncClient(transport=transport))
    data = asyncio.run(crawl_website("acme.com"))
    assert data.ok
    assert data.signals["title"] == "Acme"


# ---------------------------------------------------------------------------
# Failed crawls must not claim "No SSL"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pages, url", [
    ({"https://down.com/": httpx.ConnectError("refused"), "http://down.com/": httpx.ConnectError("refused")},
     "down.com"),
    ({"https://slow.com/": httpx.ReadTimeout("timed out")}, "slow.com"),
    ({}, "intranet.acme.com"),  # blocked before any request
])
def test_failed_crawl_reports_https_as_unknown(pages, url):
    data = crawl(make_site(pages), url)
    assert not data.ok
    assert data.signals["https"] is None  # unknown, not "No SSL"
    assert data.signals["platform"] is None and data.signals["title"] is None


def test_blocked_site_reports_the_scheme_it_answered_on():
    blocked = httpx.Response(403, text="<title>Just a moment...</title>", headers={"content-type": "text/html"})
    data = crawl(make_site({"https://shop.com/": blocked}), "shop.com")
    assert data.error == "Blocked (HTTP 403)"
    assert data.signals["https"] is True and data.signals["status_code"] == 403
    assert data.signals["title"] is None  # nothing from the challenge page is reported

    data = crawl(make_site({"http://oldshop.com/": blocked}), "http://oldshop.com")
    assert data.signals["https"] is False  # really reached over plain http


# ---------------------------------------------------------------------------
# HTTP 403 / 429: browser headers and one polite retry
# ---------------------------------------------------------------------------

def forbidden() -> httpx.Response:
    return httpx.Response(403, text="Forbidden", headers={"content-type": "text/html"})


def test_browser_headers_are_sent():
    seen: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers
        if request.url.path == "/":
            return html_response('<a href="/contact">Contact</a>')
        return html_response("<p>info@acme.com</p>")

    data = crawl(httpx.MockTransport(handler), "acme.com")
    assert data.ok
    home = seen["https://acme.com/"]
    assert "Chrome" in home["user-agent"]
    assert home["accept"].startswith("text/html")
    assert home["accept-language"] == "en-US,en;q=0.9"
    assert home["accept-encoding"] == "gzip, deflate"  # only what we can decode
    assert home["sec-fetch-mode"] == "navigate" and home["sec-fetch-site"] == "none"
    assert home["upgrade-insecure-requests"] == "1"
    assert "cookie" not in home
    sub = seen["https://acme.com/contact"]
    assert sub["referer"] == "https://acme.com/" and sub["sec-fetch-site"] == "same-origin"


def test_403_homepage_retries_other_host_once():
    requested: list[str] = []
    transport = make_site({
        "https://acme.com/": forbidden(),
        "https://www.acme.com/": "<title>Acme</title><p>info@acme.com</p>",
    }, requested)
    data = crawl(transport, "acme.com")
    assert data.ok
    assert data.final_url == "https://www.acme.com/"
    assert data.emails == ["info@acme.com"]
    assert requested.count("https://acme.com/") == 1 and requested.count("https://www.acme.com/") == 1


def test_429_homepage_on_www_retries_without_www():
    too_many = httpx.Response(429, text="slow down", headers={"content-type": "text/html"})
    transport = make_site({"https://www.acme.com/": too_many, "https://acme.com/": "<p>info@acme.com</p>"})
    data = crawl(transport, "www.acme.com")
    assert data.ok and data.final_url == "https://acme.com/"


def test_403_homepage_falls_back_to_contact_page():
    requested: list[str] = []
    transport = make_site({
        "https://acme.com/": forbidden(),
        "https://www.acme.com/": forbidden(),
        "https://acme.com/contact-us": "<title>Contact</title><p>Call 512-555-0199 or info@acme.com</p>",
    }, requested)
    data = crawl(transport, "acme.com")
    assert data.ok
    assert data.final_url == "https://acme.com/contact-us"
    assert data.emails == ["info@acme.com"]
    assert data.phones == ["512-555-0199"]
    # /contact was tried first (404), then /contact-us
    assert requested.index("https://acme.com/contact") < requested.index("https://acme.com/contact-us")


def test_403_everywhere_is_still_blocked_and_tries_little():
    requested: list[str] = []
    data = crawl(make_site({"https://acme.com/": forbidden(), "https://www.acme.com/": forbidden(),
                            "https://acme.com/contact": forbidden(), "https://acme.com/contact-us": forbidden(),
                            "https://acme.com/robots.txt": forbidden()}, requested), "acme.com")
    assert not data.ok
    assert data.error == "Blocked (HTTP 403)"
    assert len(requested) == 5  # home, www home, robots.txt, /contact, /contact-us - nothing more


def test_bot_check_page_is_not_retried():
    # Cloudflare's "Just a moment..." page protects the whole site; we don't try to get past it.
    requested: list[str] = []
    challenge = httpx.Response(403, text="<title>Just a moment...</title>",
                               headers={"content-type": "text/html", "cf-mitigated": "challenge"})
    data = crawl(make_site({"https://acme.com/": challenge, "https://www.acme.com/": "<p>info@acme.com</p>"},
                           requested), "acme.com")
    assert data.error == "Blocked (HTTP 403)"
    assert requested == ["https://acme.com/"]


def test_contact_page_fallback_respects_robots_txt():
    requested: list[str] = []
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /contact\n", headers={"content-type": "text/plain"})
    data = crawl(make_site({"https://acme.com/": forbidden(), "https://www.acme.com/": forbidden(),
                            "https://acme.com/robots.txt": robots,
                            "https://acme.com/contact": "<p>info@acme.com</p>"}, requested), "acme.com")
    assert data.error == "Blocked (HTTP 403)"
    assert not any("/contact" in u for u in requested)  # /contact and /contact-us are both disallowed


# ---------------------------------------------------------------------------
# robots.txt that can't be read
# ---------------------------------------------------------------------------

def test_robots_txt_server_error_means_homepage_only():
    requested: list[str] = []
    transport = make_site({
        "https://acmeplumbing.com/": HOME,
        "https://acmeplumbing.com/robots.txt": httpx.Response(503, text="down", headers={"content-type": "text/html"}),
        "https://acmeplumbing.com/contact/": CONTACT,
    }, requested)
    data = crawl(transport, "acmeplumbing.com")
    assert data.ok
    assert data.signals["pages_crawled"] == 1
    assert "https://acmeplumbing.com/contact/" not in requested


# ---------------------------------------------------------------------------
# Time limits
# ---------------------------------------------------------------------------

def test_per_page_time_limit(monkeypatch):
    monkeypatch.setattr(crawler, "PAGE_TIME_LIMIT", 0.3)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)  # a site that never finishes answering
        return html_response("<p>late@slow.com</p>")

    started = time.perf_counter()
    data = crawl(httpx.MockTransport(handler), "slow.com")
    assert data.error == "Website timed out"
    assert time.perf_counter() - started < 3


def test_whole_site_time_budget(monkeypatch):
    monkeypatch.setattr(crawler, "SITE_TIME_BUDGET", 0.5)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return html_response('<a href="/contact">Contact</a><a href="/about">About</a><p>info@acme.com</p>')
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        await asyncio.sleep(5)  # every sub-page is very slow
        return html_response("<p>late@acme.com</p>")

    started = time.perf_counter()
    data = crawl(httpx.MockTransport(handler), "acme.com")
    assert data.ok and data.emails == ["info@acme.com"]  # homepage results are kept
    assert data.signals["pages_crawled"] == 1
    assert time.perf_counter() - started < 3


# ---------------------------------------------------------------------------
# Compression, size cap and character encodings
# ---------------------------------------------------------------------------

class ChunkedStream(httpx.AsyncByteStream):
    """A response body that arrives in pieces, like a real network response."""

    def __init__(self, data: bytes, chunk_size: int = 16_384):
        self.data, self.chunk_size = data, chunk_size

    async def __aiter__(self):
        for i in range(0, len(self.data), self.chunk_size):
            yield self.data[i:i + self.chunk_size]


def streamed(data: bytes, **headers) -> httpx.Response:
    headers = {"content-type": "text/html", **{k.replace("_", "-"): v for k, v in headers.items()}}
    return httpx.Response(200, headers=headers, stream=ChunkedStream(data))


def test_gzip_and_deflate_pages_are_decoded():
    page = b"<title>Zipped</title><p>info@zipped.com</p>"
    raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw_deflate = raw_deflate.compress(page) + raw_deflate.flush()
    for body, encoding in [(gzip.compress(page), "gzip"), (zlib.compress(page), "deflate"),
                           (raw_deflate, "deflate")]:
        data = crawl(make_site({"https://zipped.com/": streamed(body, content_encoding=encoding)}), "zipped.com")
        assert data.ok and data.emails == ["info@zipped.com"], encoding
        assert data.signals["title"] == "Zipped"


def test_zip_bomb_is_cut_at_the_size_cap():
    bomb = gzip.compress(b"<p>info@bomb.com</p>" + b" " * 8_000_000)  # 8 MB that zips to ~8 KB

    async def read() -> bytes:
        return await crawler._read_body(streamed(bomb, content_encoding="gzip"), crawler.MAX_BYTES)

    body = asyncio.run(read())
    assert len(body) == crawler.MAX_BYTES  # 2 MB, never the full 8 MB
    data = crawl(make_site({"https://bomb.com/": streamed(bomb, content_encoding="gzip")}), "bomb.com")
    assert data.ok and data.emails == ["info@bomb.com"]


def test_streamed_page_is_capped(monkeypatch):
    monkeypatch.setattr(crawler, "MAX_BYTES", 10_000)
    body = b"<p>info@bigpage.com</p>" + b"<p>filler</p>" * 20_000 + b"<p>late@bigpage.com</p>"
    data = crawl(make_site({"https://bigpage.com/": streamed(body)}), "bigpage.com")
    assert data.ok and data.emails == ["info@bigpage.com"]


def test_unsupported_compression_is_reported():
    data = crawl(make_site({"https://br.com/": streamed(b"\x0b\x02\x80", content_encoding="br")}), "br.com")
    assert not data.ok
    assert data.error == "Could not read website (unsupported compression)"


@pytest.mark.parametrize("body, content_type, expected", [
    ("<p>José Pérez – Dueño</p>".encode("cp1252"), "text/html", "José Pérez"),
    ('<meta charset="iso-8859-1"><p>José</p>'.encode("latin-1"), "text/html", "José"),
    ('<meta http-equiv="Content-Type" content="text/html; charset=windows-1251"><p>Иван</p>'
     .encode("cp1251"), "text/html", "Иван"),
    ("<p>José</p>".encode("utf-8"), "text/html; charset=base64", "José"),  # not a text codec
    ("<p>José</p>".encode("utf-8"), "text/html; charset=no-such-charset", "José"),
    (b"\xef\xbb\xbf<p>Jos\xc3\xa9</p>", "text/html; charset=iso-8859-1", "José"),  # the BOM wins
    ("<p>José</p>".encode("utf-16"), "text/html", "José"),
])
def test_page_encodings(body, content_type, expected):
    response = httpx.Response(200, content=body, headers={"content-type": content_type})
    assert expected in crawler._decode(body, response)


def test_utf8_cut_in_the_middle_of_a_character_stays_utf8():
    body = "<p>José García</p>".encode("utf-8") + "é".encode("utf-8")[:1]
    text = crawler._decode(body, httpx.Response(200, content=body, headers={"content-type": "text/html"}))
    assert "José García" in text


# ---------------------------------------------------------------------------
# SSRF protection (more cases)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "224.0.0.1", "255.255.255.255", "240.0.0.1", "::1", "::", "fe80::1", "fc00::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1",  # IPv4-mapped
    "::127.0.0.1",  # IPv4-compatible (deprecated, but still parsed)
    "64:ff9b::7f00:1", "64:ff9b::a00:1",  # NAT64 to 127.0.0.1 / 10.0.0.1
    "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo wrapping 127.0.0.1
    "fe80::1%eth0", "not-an-ip",
])
def test_non_public_ips_are_refused(ip):
    assert crawler._is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_ips_are_allowed(ip):
    assert crawler._is_public_ip(ip) is True


@pytest.mark.parametrize("url", [
    "http://acme.com:22/",  # not a web port
    "http://[::ffff:127.0.0.1]/",
    "http://0x7f000001/",  # resolves (fake DNS below) to loopback
])
def test_more_unsafe_urls_are_refused(url, monkeypatch):
    async def resolve(host: str) -> list[str]:
        return ["127.0.0.1"] if host == "0x7f000001" else [PUBLIC_IP]

    monkeypatch.setattr(crawler, "_resolve_host", resolve)
    requested: list[str] = []
    data = crawl(make_site({}, requested), url)
    assert not data.ok and data.error.startswith("Blocked")
    assert requested == []


def test_redirect_to_non_web_scheme_is_refused():
    redirect = httpx.Response(302, headers={"location": "file:///etc/passwd"})
    data = crawl(make_site({"https://evil.com/": redirect}), "evil.com")
    assert not data.ok and data.error.startswith("Blocked")


class FakeBackend(httpcore.AsyncNetworkBackend):
    """Records where connections would go; never touches the network."""

    def __init__(self, fail: set[str] = frozenset()):
        self.calls: list[str] = []
        self.fail = fail

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append(host)
        if host in self.fail:
            raise httpcore.ConnectError("refused")
        return "stream-to-" + host

    async def sleep(self, seconds):
        pass


def test_safe_backend_connects_only_to_the_checked_public_address(monkeypatch):
    async def resolve(host: str) -> list[str]:
        return {"intranet.acme.com": ["10.0.0.5"], "two.acme.com": ["2606:4700::1", PUBLIC_IP]}.get(host, [PUBLIC_IP])

    monkeypatch.setattr(crawler, "_resolve_host", resolve)

    async def run():
        inner = FakeBackend(fail={"2606:4700::1"})
        backend = crawler._SafeNetworkBackend(inner)
        assert await backend.connect_tcp("acme.com", 443) == "stream-to-" + PUBLIC_IP  # the IP, not the name
        assert await backend.connect_tcp("two.acme.com", 443) == "stream-to-" + PUBLIC_IP  # 2nd address
        with pytest.raises(crawler._BlockedHost):
            await backend.connect_tcp("intranet.acme.com", 80)
        with pytest.raises(crawler._BlockedHost):
            await backend.connect_tcp("127.0.0.1", 80)
        return inner.calls

    assert asyncio.run(run()) == [PUBLIC_IP, "2606:4700::1", PUBLIC_IP]


def test_make_client_refuses_private_addresses_without_connecting():
    async def run():
        async with crawler.make_client() as client:
            # Straight through the client (skipping crawl_website's own checks): the
            # connection itself is refused before anything is sent.
            with pytest.raises(crawler._BlockedHost):
                await client.get("http://intranet.acme.com/")
            with pytest.raises(crawler._BlockedHost):
                await client.get("http://127.0.0.1:8000/api/jobs")

    asyncio.run(run())


def test_site_local_ipv6_is_refused():
    assert crawler._is_public_ip("fec0::1") is False
    assert crawler.is_public_ip("8.8.8.8") is True
