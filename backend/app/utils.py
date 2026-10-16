from urllib.parse import urlparse


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip().split(";")[0].strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def extract_domain(url: str | None) -> str | None:
    """'https://www.Example.com/about' -> 'example.com'"""
    url = normalize_url(url)
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None
