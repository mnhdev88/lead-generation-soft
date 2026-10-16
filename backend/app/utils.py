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


# Websites on shared platforms: their domain is not the company's own email domain,
# so we never guess emails or spend Hunter searches on them.
SHARED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
    "google.com", "goo.gl", "g.page", "linktr.ee", "yelp.com", "wa.me", "whatsapp.com",
}
SHARED_DOMAIN_SUFFIXES = (
    ".business.site", ".wixsite.com", ".wordpress.com", ".blogspot.com", ".godaddysites.com",
    ".squarespace.com", ".webflow.io", ".netlify.app", ".vercel.app", ".weebly.com", ".site123.me",
)


def own_domain(domain: str | None) -> str | None:
    """The company's own domain, or None for e.g. a Facebook page or a free Wix site."""
    domain = (domain or "").lower()
    if not domain or domain in SHARED_DOMAINS or domain.endswith(SHARED_DOMAIN_SUFFIXES):
        return None
    return domain


def merge_emails(*lists: list[str]) -> list[str]:
    """Join email lists keeping the first occurrence of each (lowercase) address,
    so the order (best first) of the earlier lists wins."""
    merged: list[str] = []
    for emails in lists:
        for e in emails or []:
            e = (e or "").strip().lower()
            if e and e not in merged:
                merged.append(e)
    return merged
