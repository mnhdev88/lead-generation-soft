"""Buying intent: how likely a business needs (and buys) each service we sell, right now.

One score 0-100 per service, each with its reasons, e.g.
    website  65  ["No SSL (http only) +15", "Slow on mobile (PageSpeed 38) +15", ...]
like the lead score (app/scoring.py) and the size estimate (app/sizing.py). Stored in
Company.intent, with the four scores also as plain columns (intent_website, ...) so the
list can be filtered and sorted by them. Change the weights below to tune it.

Precision first: a point is only given when we are sure of the fact behind it.
    Website facts count only when they came from a good crawl (pages_crawled) of the
    business's own site (not a Facebook / Yelp page), and a "missing" fact only when the
    crawler could tell (False; None = can't tell, e.g. a page built by JavaScript). With a
    tag manager (GTM) on the site, chat / ads / booking widgets may be loaded later, so their
    absence gives no points; "no payment" / "no live chat" also need ABSENCE_MIN_PAGES pages.
    PageSpeed only when the check worked on the website the company has now. Google review
    facts only when we have them. Status Lost / Not interested, or closed -> every score 0.

Services and their points
    website    no website +30, only a Facebook / free page +25, no SSL +15, copyright 3+ years
               old +10, slow on mobile (PageSpeed < 50 +15, 50-69 +8), not mobile-friendly
               +10, DIY website builder +10, possibly a new business +20
    seo        missing page title / description +15, no LocalBusiness schema +10, no analytics
               and no Search Console +10, PageSpeed SEO < 80 +10, few Google reviews or photos
               +15, low in Google's results of a small search (> 10 +15, > 5 +8), reviews far
               below the competitors +10 (not when "few reviews" already counted). Without a
               website at most SEO_NO_WEBSITE_CAP.
    software   (by kind of business) no online booking +20 / no online ordering +20 / no
               online shop +10, no online payment +10, PDF forms +15, hiring office staff
               +20, medium or large business +10, reviews complain about booking / phone /
               waiting +15, no live chat +5
    marketing  runs ads +20, hiring marketing +15, social profiles but no own website +10,
               rating below 4.0 +15, fewer reviews than competitors +10, reviews complain
               about the website +5, measures visitors but runs no ads +5
"""
import re
from datetime import datetime, timezone

from .models import Company
from .utils import SHARED_DOMAINS, normalize_url, own_domain

OFFERS = ("website", "seo", "software", "marketing")
OFFER_LABELS = {"website": "Website", "seo": "SEO", "software": "Software", "marketing": "Marketing"}
HOT_SCORE = 60  # this score or more = a hot lead for that service

# --- weights -------------------------------------------------------------------------

# website
WEBSITE_NONE = 30
WEBSITE_SHARED_PAGE = 25  # only a Facebook page / free Wix site, no own domain
WEBSITE_NO_SSL = 15
WEBSITE_OLD_COPYRIGHT = 10
OLD_COPYRIGHT_YEARS = 3  # copyright this many years old or more = outdated
WEBSITE_VERY_SLOW = 15  # PageSpeed performance below SLOW_SCORE
WEBSITE_SLOW = 8  # PageSpeed performance below OK_SPEED_SCORE
SLOW_SCORE = 50
OK_SPEED_SCORE = 70
WEBSITE_NOT_MOBILE = 10
WEBSITE_DIY_BUILDER = 10
DIY_PLATFORMS = {"wix", "godaddy", "weebly", "squarespace", "site123"}
DIY_DOMAIN_SUFFIXES = (".wixsite.com", ".godaddysites.com", ".weebly.com", ".squarespace.com", ".site123.me")
WEBSITE_NEW_BUSINESS = 20
ESTABLISHED_YEARS = 2  # founded / copyright this many years ago = not a new business
# "Possibly a new business" only when we have seen ALL its reviews (Google sends at most 5)
# and the oldest one is younger than NEW_BUSINESS_DAYS, counted from today (not from the
# day of the search, so the reason goes away by itself).
NEW_BUSINESS_MAX_REVIEWS = 5
NEW_BUSINESS_DAYS = 365

# seo
SEO_MISSING_META = 15
SEO_NO_LOCAL_SCHEMA = 10
SEO_NO_TRACKING = 10
SEO_LOW_PAGESPEED = 10
LOW_SEO_SCORE = 80  # PageSpeed SEO score below this
SEO_FEW_REVIEWS_OR_PHOTOS = 15
FEW_REVIEWS = 20
FEW_PHOTOS = 5
SEO_RANK_FAR = 15  # Google search position worse than RANK_FAR (only small searches save one)
SEO_RANK_MID = 8  # position worse than RANK_MID
RANK_FAR = 10
RANK_MID = 5
SEO_FAR_BELOW_PEERS = 10
FAR_BELOW_PEERS_RATIO = 0.2  # fewer reviews than a fifth of the typical count in the search
SEO_NO_WEBSITE_CAP = 30

# software
SOFTWARE_NO_BOOKING = 20  # appointment businesses
SOFTWARE_NO_ORDERING = 20  # restaurants / food
SOFTWARE_NO_SHOP = 10  # retail
SOFTWARE_NO_PAYMENT = 10  # only for the business kinds above
SOFTWARE_PDF_FORMS = 15
SOFTWARE_HIRING_OFFICE = 20
OFFICE_ROLES = {"front_desk": "front desk", "scheduling": "scheduling", "data_entry": "data entry",
                "customer_service": "customer service", "office_admin": "office admin"}
SOFTWARE_BIG_BUSINESS = 10
SOFTWARE_PAIN_POINTS = 15
SOFTWARE_PAIN_CATEGORIES = {"booking": "booking", "phone": "phone", "communication": "no follow-up",
                            "wait_time": "waiting"}
SOFTWARE_NO_CHAT = 5
# "No online payment" / "No live chat" only when the crawl read at least this many pages
# (a payment page or a chat widget may sit on a page we did not read).
ABSENCE_MIN_PAGES = 3

# marketing
MARKETING_RUNS_ADS = 20
AD_LABELS = {"google_ads": "Google Ads", "meta": "Meta", "tiktok": "TikTok", "linkedin": "LinkedIn", "bing": "Bing"}
MARKETING_HIRING = 15
MARKETING_ROLES = {"marketing": "marketing", "social_media": "social media"}
MARKETING_SOCIAL_NO_SITE = 10
MARKETING_LOW_RATING = 15
LOW_RATING = 4.0
MIN_REVIEWS_FOR_RATING = 3  # one or two unhappy reviews don't make a "low rating"
MARKETING_BELOW_PEERS = 10
BELOW_PEERS_RATIO = 0.5  # fewer reviews than half the typical count in the search
MARKETING_WEBSITE_COMPLAINTS = 5
MARKETING_MEASURES_NO_ADS = 5

# Statuses that mean "don't bother" (the same as in scoring.py, which imports this module).
DEAD_STATUSES = {"lost": "Lost", "not_interested": "Not interested"}

# --- kind of business (from the category text, e.g. "Dentist", "dental clinic") ------------
# Order of the checks in business_kind:
#   1. words that mean "not a business that books / orders / sells online" (a county health
#      department, a medical school, a dental supply wholesaler) -> unknown, no points;
#   2. Google's primaryType when it is in PRIMARY_TYPE_KINDS (the most exact fact we have);
#   3. the category words below: shop words first ("Grill store" is a shop, not food), then
#      food, then appointment businesses.
# Word starts: r"\bsalon" matches "Salon" and "salons"; a "\b" at the end means the whole word.

NOT_A_KIND_WORDS = [
    r"government", r"department", r"city hall", r"county", r"municipal", r"embassy", r"consulate",
    r"school", r"university", r"college", r"academy", r"institute",
    r"insurance", r"supplier", r"wholesale", r"distributor",
    r"manufactur", r"factory", r"laborator", r"lab\b", r"labs\b",
]
NOT_A_KIND_TYPES = {
    "local_government_office", "government_office", "city_hall", "courthouse", "embassy",
    "school", "primary_school", "secondary_school", "university", "insurance_agency",
    "wholesaler", "medical_lab", "police", "fire_station", "post_office",
}
# Google primaryType -> kind. Used before the category words (they can mislead).
PRIMARY_TYPE_KINDS = {
    "dentist": "appointments", "dental_clinic": "appointments", "doctor": "appointments",
    "physiotherapist": "appointments", "chiropractor": "appointments", "medical_clinic": "appointments",
    "hair_salon": "appointments", "hair_care": "appointments", "beauty_salon": "appointments",
    "barber_shop": "appointments", "nail_salon": "appointments", "spa": "appointments",
    "massage": "appointments", "veterinary_care": "appointments", "car_repair": "appointments",
    "lawyer": "appointments", "accounting": "appointments", "plumber": "appointments",
    "electrician": "appointments", "roofing_contractor": "appointments", "locksmith": "appointments",
    "gym": "appointments", "fitness_center": "appointments", "yoga_studio": "appointments",
    "restaurant": "food", "meal_takeaway": "food", "meal_delivery": "food", "cafe": "food",
    "coffee_shop": "food", "bakery": "food", "pizza_restaurant": "food", "fast_food_restaurant": "food",
    "clothing_store": "retail", "florist": "retail", "jewelry_store": "retail", "furniture_store": "retail",
    "gift_shop": "retail", "book_store": "retail", "shoe_store": "retail",
}
FOOD_WORDS = [
    r"restaurant", r"caf[eé]", r"coffee", r"pizz", r"bakery", r"diner\b", r"bistro", r"grill",
    r"takeaway", r"take ?out", r"fast food", r"food court", r"food truck", r"sushi", r"burger",
    r"kebab", r"biryani", r"catering", r"deli\b", r"steak ?house", r"taco", r"noodle", r"bbq",
    r"barbecue", r"meal ", r"ice cream", r"dhaba", r"sweets", r"juice bar",
]
RETAIL_WORDS = [  # checked before food and appointment words ("beauty supply store" is a shop)
    r"store", r"supermarket", r"boutique", r"retail", r"outlet", r"florist", r"jewel",
    r"clothing", r"apparel", r"furniture", r"hardware", r"grocery", r"gift", r"pharmacy",
    r"mart\b",
]
STRONG_FOOD_WORDS = [r"restaurant", r"caf[eé]", r"coffee shop"]  # "Restaurant & gift store" is food
APPOINTMENT_WORDS = [
    # health ("health" and "medical" alone are too broad: health departments, medical schools)
    r"dentist", r"dental", r"orthodont", r"clinic", r"doctor", r"physician", r"medical clinic",
    r"medical center", r"medical practice", r"medical spa", r"health clinic", r"health center",
    r"chiropract", r"physio", r"physical therap", r"therap", r"optometr",
    r"optician", r"eye care", r"dermatolog", r"pediatric", r"paediatric", r"podiatr",
    r"psycholog", r"counsel", r"acupunct", r"urgent care", r"hearing",
    # beauty
    r"salon", r"barber", r"beauty", r"spa\b", r"spas\b", r"nail", r"massage", r"lash",
    r"brow", r"wax", r"tattoo", r"cosmetic", r"aesthetic", r"hair",
    # legal / professional
    r"lawyer", r"attorney", r"law firm", r"legal", r"notary", r"accountant", r"tax prep",
    # auto (no "car wash": a drive-in, and no "tire": tire shops and wholesalers sell)
    r"auto repair", r"car repair", r"mechanic", r"auto service", r"car service",
    r"oil change", r"auto body", r"body shop", r"car detail",
    # home services (no "contractor" / "cleaning": general contractors quote, cleaning
    # products are sold)
    r"plumb", r"electrician", r"hvac", r"heating", r"air condition", r"roofing", r"roofer",
    r"cleaning service", r"house cleaning", r"maid", r"pest", r"locksmith", r"handyman",
    r"landscap", r"lawn", r"painter", r"moving", r"movers",
    # fitness / pets / lessons
    r"gym\b", r"fitness", r"yoga", r"pilates", r"personal trainer", r"martial art",
    r"veterinar", r"vet\b", r"animal hospital", r"pet groom", r"grooming", r"tutor",
    r"driving school", r"photograph",
]
WEAK_RETAIL_WORDS = [r"shop\b", r"shops\b"]  # after appointment words: "barber shop" books


def _words_re(words: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + ")", re.IGNORECASE)


_NOT_A_KIND_RE, _STRONG_FOOD_RE = _words_re(NOT_A_KIND_WORDS), _words_re(STRONG_FOOD_WORDS)
_FOOD_RE, _RETAIL_RE = _words_re(FOOD_WORDS), _words_re(RETAIL_WORDS)
_APPOINTMENT_RE, _WEAK_RETAIL_RE = _words_re(APPOINTMENT_WORDS), _words_re(WEAK_RETAIL_WORDS)


def business_kind(company: Company) -> str | None:
    """'appointments' | 'food' | 'retail' | None (unknown) from Google's primaryType
    ("dental_clinic") and the category text. Unknown gives no booking / ordering / shop /
    payment points, so when in doubt we say unknown."""
    raw = company.raw if isinstance(company.raw, dict) else {}
    primary = raw.get("primaryType") if isinstance(raw.get("primaryType"), str) else ""
    primary = primary.strip().lower()
    category = company.category or ""
    text = f"{category} {primary.replace('_', ' ')}".strip()
    if not text:
        return None
    if primary in NOT_A_KIND_TYPES or _NOT_A_KIND_RE.search(text):
        return None
    if primary in PRIMARY_TYPE_KINDS:
        return PRIMARY_TYPE_KINDS[primary]
    if _STRONG_FOOD_RE.search(text):
        return "food"
    if _RETAIL_RE.search(text):
        return "retail"
    if _FOOD_RE.search(text):
        return "food"
    if _APPOINTMENT_RE.search(text):
        return "appointments"
    if _WEAK_RETAIL_RE.search(text):
        return "retail"
    return None


# --- helpers ---------------------------------------------------------------------------


def _reason(label: str, points: int) -> str:
    """'No SSL', 15 -> 'No SSL +15'"""
    return f"{label} {points:+d}"


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def is_platform_page(domain: str | None) -> bool:
    """True for a Facebook / Yelp / Linktree ... page (also m.facebook.com): its HTML and
    speed are the platform's, not the business's. Free builder sites (joes.wixsite.com)
    are the business's own pages: False."""
    host = (domain or "").lower()
    return bool(host) and any(host == d or host.endswith("." + d) for d in SHARED_DOMAINS)


def _has_own_site(domain: str | None) -> bool:
    """A website on the business's own domain (not a Facebook page or a free Wix site)."""
    return own_domain(domain) is not None and not is_platform_page(domain)


def _site_signals(company: Company) -> dict:
    """Website facts, only when a good crawl found them (and the company has a website
    of its own: a Facebook page's HTML says nothing about the business)."""
    signals = company.website_signals if isinstance(company.website_signals, dict) else {}
    if not company.website or not signals.get("pages_crawled") or is_platform_page(company.domain):
        return {}
    return signals


def _same_url(a: str, b: str) -> bool:
    """'https://www.acme.com/' and 'acme.com' are the same website."""
    def key(url: str) -> str:
        url = (normalize_url(url) or "").lower().split("#")[0].rstrip("/")
        url = url.replace("://www.", "://", 1)
        return url.split("://", 1)[-1]
    return key(a) == key(b)


def _pagespeed(company: Company) -> dict:
    """PageSpeed result, only when the check worked, on the website the company has now
    (the result keeps the "url" it tested; older results without one are trusted)."""
    result = company.pagespeed if isinstance(company.pagespeed, dict) else {}
    if not company.website or not result or result.get("error") or is_platform_page(company.domain):
        return {}
    tested = result.get("url")
    if isinstance(tested, str) and tested.strip() and not _same_url(tested, company.website):
        return {}
    return result


def _missing(signals: dict, key: str) -> bool:
    """The crawler looked for it and it is not there (False, not just an absent key or
    None = "can't tell", e.g. a page built by JavaScript)."""
    return key in signals and signals[key] is False


def _tag_manager(signals: dict) -> bool:
    """A tag manager (GTM) can add chat widgets, ad pixels and booking widgets after the
    page loads, so their absence from the HTML proves nothing."""
    return signals.get("has_tag_manager") is True


def _missing_after_full_crawl(signals: dict, key: str) -> bool:
    """Missing, and the crawl read enough pages, with no tag manager, for us to be sure."""
    pages = _number(signals.get("pages_crawled")) or 0
    return _missing(signals, key) and pages >= ABSENCE_MIN_PAGES and not _tag_manager(signals)


def _parse_time(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _possibly_new(company: Company, review: dict, now: datetime | None = None) -> bool:
    """Worked out at scoring time: few reviews in total (we saw them all) and the oldest
    is younger than NEW_BUSINESS_DAYS today. The flag saved at search time must agree
    (it is False when a review had no date, so we can't tell)."""
    if review.get("likely_new") is not True:
        return False
    count = company.reviews_count
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= NEW_BUSINESS_MAX_REVIEWS:
        return False
    seen = company.google_reviews if isinstance(company.google_reviews, list) else []
    if seen and count > len(seen):
        return False  # Google has reviews we did not get: the oldest may be years old
    oldest = _parse_time(review.get("oldest_review"))
    if oldest is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - oldest).days < NEW_BUSINESS_DAYS


def _known_established(company: Company, signals: dict) -> bool:
    """Facts that prove the business is not new, whatever its few Google reviews suggest:
    a founding year or copyright at least ESTABLISHED_YEARS ago, or a 2020-21 PPP loan."""
    this_year = datetime.now(timezone.utc).year
    for key in ("founded_year", "copyright_year"):
        year = _number(signals.get(key))
        if year and year <= this_year - ESTABLISHED_YEARS:
            return True
    ppp = company.ppp if isinstance(company.ppp, dict) else {}
    return bool(ppp.get("loan_number") or ppp.get("jobs"))


def _pain_points(review: dict) -> dict:
    """Review pain points strong enough to score. Newer analyses list them in
    "pain_points_confirmed" (one passing remark is only shown as a quote)."""
    pains = review.get("pain_points") if isinstance(review.get("pain_points"), dict) else {}
    confirmed = review.get("pain_points_confirmed")
    if isinstance(confirmed, list):
        return {k: v for k, v in pains.items() if k in confirmed}
    return pains


def _reviews(company: Company) -> int | None:
    """Google review count. Google leaves the count out for a business without reviews,
    so with the Google profile loaded (photos counted) a missing count means 0."""
    if company.reviews_count is not None:
        return company.reviews_count
    return 0 if company.google_photos_count is not None else None


def _labels(keys, labels: dict) -> str:
    return ", ".join(labels.get(k, k) for k in keys)


# --- one service at a time ---------------------------------------------------------------


def _website_points(company: Company, signals: dict, speed: dict, review: dict) -> list[tuple[str, int]]:
    points: list[tuple[str, int]] = []
    domain = (company.domain or "").lower()
    if not company.website and company.source == "google":
        # Only Google's listing proves it: OpenStreetMap often just hasn't recorded the website
        # (e.g. MOD Pizza), so an OpenStreetMap-only lead without one gets no points here.
        points.append(("No website", WEBSITE_NONE))
    elif not _has_own_site(domain):
        points.append(("Only a Facebook / free page, no own website", WEBSITE_SHARED_PAGE))
    if signals.get("https") is False:
        points.append(("No SSL (http only)", WEBSITE_NO_SSL))
    year = _number(signals.get("copyright_year"))
    if year and year <= datetime.now(timezone.utc).year - OLD_COPYRIGHT_YEARS:
        points.append((f"Outdated website (copyright {int(year)})", WEBSITE_OLD_COPYRIGHT))
    performance = _number(speed.get("performance"))
    if performance is not None and performance < SLOW_SCORE:
        points.append((f"Very slow on mobile (PageSpeed {int(performance)})", WEBSITE_VERY_SLOW))
    elif performance is not None and performance < OK_SPEED_SCORE:
        points.append((f"Slow on mobile (PageSpeed {int(performance)})", WEBSITE_SLOW))
    if signals.get("has_viewport") is False:
        points.append(("Not mobile-friendly", WEBSITE_NOT_MOBILE))
    platform = str(signals.get("platform") or "").lower().replace(" ", "")
    if platform in DIY_PLATFORMS or (company.website and domain.endswith(DIY_DOMAIN_SUFFIXES)):
        name = signals.get("platform") or "free builder"
        points.append((f"DIY website builder ({name})", WEBSITE_DIY_BUILDER))
    if _possibly_new(company, review) and not _known_established(company, signals):
        points.append(("Possibly a new business", WEBSITE_NEW_BUSINESS))
    return points


def _seo_points(company: Company, signals: dict, speed: dict) -> list[tuple[str, int]]:
    points: list[tuple[str, int]] = []
    if "title" in signals or "description" in signals:
        missing = [what for what, key in (("page title", "title"), ("meta description", "description"))
                   if not signals.get(key)]
        if missing:
            points.append((f"Missing {' and '.join(missing)}", SEO_MISSING_META))
    if _missing(signals, "has_local_schema"):
        points.append(("No LocalBusiness schema", SEO_NO_LOCAL_SCHEMA))
    if _missing(signals, "has_analytics") and _missing(signals, "has_search_console"):
        points.append(("No Google Analytics or Search Console", SEO_NO_TRACKING))
    seo_score = _number(speed.get("seo"))
    if seo_score is not None and seo_score < LOW_SEO_SCORE:
        points.append((f"Low PageSpeed SEO score ({int(seo_score)})", SEO_LOW_PAGESPEED))
    reviews, photos = _reviews(company), company.google_photos_count
    few = []
    few_reviews = reviews is not None and reviews < FEW_REVIEWS
    if few_reviews:
        few.append(f"{reviews} reviews")
    if photos is not None and photos < FEW_PHOTOS:
        few.append(f"{photos} photos")
    if few:
        points.append((f"Weak Google profile ({', '.join(few)})", SEO_FEW_REVIEWS_OR_PHOTOS))
    rank = company.search_rank
    if rank is not None and rank > RANK_FAR:
        points.append((f"Not in Google's top {RANK_FAR} for the search (position {rank})", SEO_RANK_FAR))
    elif rank is not None and rank > RANK_MID:
        points.append((f"Google search position {rank}", SEO_RANK_MID))
    peers = company.peer_reviews_median
    # One fact (few reviews) counts once in SEO: "far below competitors" only when the
    # weak profile above was not about the reviews.
    if peers and not few_reviews and reviews is not None and reviews < peers * FAR_BELOW_PEERS_RATIO:
        points.append((f"Far fewer reviews than competitors ({reviews} vs typical {peers})", SEO_FAR_BELOW_PEERS))
    return points


def _software_points(company: Company, signals: dict, review: dict) -> list[tuple[str, int]]:
    points: list[tuple[str, int]] = []
    kind = business_kind(company)
    # A tag manager may load a booking / ordering widget we can't see: no points then.
    sure = not _tag_manager(signals)
    if kind == "appointments" and sure and _missing(signals, "has_online_booking"):
        points.append(("No online booking", SOFTWARE_NO_BOOKING))
    elif kind == "food" and sure and _missing(signals, "has_online_ordering"):
        points.append(("No online ordering", SOFTWARE_NO_ORDERING))
    elif kind == "retail" and sure and _missing(signals, "has_shop"):
        points.append(("No online shop", SOFTWARE_NO_SHOP))
    # Booking and ordering platforms (Vagaro, Square, Toast...) take deposits and payments
    # themselves, so a business using one is not missing online payment.
    takes_payment = signals.get("has_online_booking") is True or signals.get("has_online_ordering") is True
    if kind and not takes_payment and _missing_after_full_crawl(signals, "has_online_payment"):
        points.append(("No online payment", SOFTWARE_NO_PAYMENT))
    if signals.get("has_pdf_forms") is True:
        points.append(("Paper / PDF forms on the website", SOFTWARE_PDF_FORMS))
    office = [r for r in OFFICE_ROLES if r in (signals.get("hiring_roles") or [])]
    if office:
        points.append((f"Hiring {_labels(office, OFFICE_ROLES)}", SOFTWARE_HIRING_OFFICE))
    if company.size_band in ("medium", "large"):
        points.append((f"Size {company.size_band}", SOFTWARE_BIG_BUSINESS))
    pains = _pain_points(review)
    found = [c for c in SOFTWARE_PAIN_CATEGORIES if pains.get(c)]
    if found:
        points.append((f"Reviews complain about {_labels(found, SOFTWARE_PAIN_CATEGORIES)}", SOFTWARE_PAIN_POINTS))
    if _missing_after_full_crawl(signals, "has_live_chat"):
        points.append(("No live chat", SOFTWARE_NO_CHAT))
    return points


def _marketing_points(company: Company, signals: dict, review: dict) -> list[tuple[str, int]]:
    points: list[tuple[str, int]] = []
    pixels = signals.get("ad_pixels") if isinstance(signals.get("ad_pixels"), list) else None
    if pixels:
        points.append((f"Runs ads ({_labels(pixels, AD_LABELS)})", MARKETING_RUNS_ADS))
    roles = [r for r in MARKETING_ROLES if r in (signals.get("hiring_roles") or [])]
    if roles:
        points.append((f"Hiring {_labels(roles, MARKETING_ROLES)}", MARKETING_HIRING))
    if any(company.socials.values() if isinstance(company.socials, dict) else []) and not _has_own_site(company.domain):
        points.append(("Social profiles but no own website", MARKETING_SOCIAL_NO_SITE))
    reviews = _reviews(company)
    rating = _number(company.rating)
    if rating is not None and rating < LOW_RATING and (reviews or 0) >= MIN_REVIEWS_FOR_RATING:
        points.append((f"Rating {rating:g}", MARKETING_LOW_RATING))
    peers = company.peer_reviews_median
    if peers and reviews is not None and reviews < peers * BELOW_PEERS_RATIO:
        points.append((f"Fewer reviews than competitors ({reviews} vs typical {peers})", MARKETING_BELOW_PEERS))
    pains = _pain_points(review)
    if pains.get("website"):
        points.append(("Reviews complain about the website", MARKETING_WEBSITE_COMPLAINTS))
    if signals.get("has_analytics") is True and pixels is not None and not pixels and not _tag_manager(signals):
        points.append(("Measures visitors but runs no ads", MARKETING_MEASURES_NO_ADS))
    return points


def _total(points: list[tuple[str, int]], cap: int = 100) -> int:
    return max(0, min(cap, 100, sum(n for _, n in points)))


def estimate_intent(company: Company) -> dict[str, dict]:
    """{"website": {"score": 65, "reasons": [...]}, "seo": ..., "software": ..., "marketing": ...}
    for the company (see the module docstring). Does not change it and runs no queries."""
    signals = _site_signals(company)
    speed = _pagespeed(company)
    review = company.review_signals if isinstance(company.review_signals, dict) else {}

    all_points = {
        "website": _website_points(company, signals, speed, review),
        "seo": _seo_points(company, signals, speed),
        "software": _software_points(company, signals, review),
        "marketing": _marketing_points(company, signals, review),
    }
    dead = None
    if company.lead_status in DEAD_STATUSES:
        dead = f"Status {DEAD_STATUSES[company.lead_status]} → 0"
    elif company.business_status and company.business_status.upper() != "OPERATIONAL":
        dead = "Closed → 0"

    result = {}
    for offer in OFFERS:
        points = all_points[offer]
        reasons = [_reason(label, n) for label, n in points]
        cap = 100
        if offer == "seo" and not company.website:
            cap = SEO_NO_WEBSITE_CAP
            if sum(n for _, n in points) > cap:
                reasons.append(f"No website: SEO at most {cap}")
        score = _total(points, cap)
        if dead:
            score, reasons = 0, [dead] + reasons
        result[offer] = {"score": score, "reasons": reasons}
    return result


def apply_intent(company: Company) -> None:
    """Save the intent scores on the company (the caller commits). Called at the end of
    scoring.rescore, so every place that rescores a company also refreshes them."""
    intent = estimate_intent(company)
    company.intent = intent  # a new dict, so the JSON column is saved
    for offer in OFFERS:
        setattr(company, f"intent_{offer}", intent[offer]["score"])
    best = max(OFFERS, key=lambda o: intent[o]["score"])  # first in OFFERS wins a tie
    best_score = intent[best]["score"]
    company.intent_best = best if best_score > 0 else None
    company.intent_best_score = best_score  # 0, not NULL: NULL means "not scored yet"
