"""Company size: roughly how many people work at a business, and why we think so.

Like the lead score, every estimate comes with its reasons, e.g.
    'Website says: "our team of 25 dentists"'   "PPP loan 2021: 14 jobs reported ..."
stored in size_reasons and shown in the UI. Change the thresholds below to tune it.

Size bands (people working there)
    solo 1   small 2-10   medium 11-50   large 51+
    None = unknown (no evidence). A business is only "solo" when a headcount of 1 is
    stated or reported, never just because we know nothing.

Headcount, best first
    1. the website says it in its text ("a team of 25", "over 50 employees" -> 50)
    2. a US PPP loan (2020-21): the headcount the business reported itself (app/ppp.py)
    3. the website's hidden page data (JSON-LD numberOfEmployees; often a template value)
    When the website and the PPP loan differ more than DISAGREE_RATIO times, the website
    text wins (it is current, the loan is from 2020-21) but JSON-LD loses to the loan.
    Either way the confidence drops to medium and both figures are listed.
    4. people listed on the website: a minimum (at least that many work there). It can
       only raise the figure, never lower it.
    Website facts count when they came from a good crawl at some point (pages_crawled):
    a later re-crawl that times out does not make them wrong.

Locations
    2+ locations -> at least small, 3+ -> at least medium, 10+ -> at least large.
    Locations = what the website says, or the saved companies with the same own website
    at other places (branches) plus this one, whichever is more.
    They only raise the band when there is no headcount (1-3 above). With a headcount the
    band stays what the headcount says; the locations are listed as a reason and, when they
    suggest a bigger business, the confidence drops to medium.
    More than CHAIN_BRANCHES saved branches on one website: probably a chain or franchise
    brand's website (each store is its own small business), so they are listed as a reason
    but say nothing about this company's size.

Weak hints (only when no headcount is known at all)
    300+ Google reviews -> at least small, 1500+ -> at least medium. Hiring -> not solo.

Confidence
    high    website text or PPP loan, nothing contradicts it
    medium  JSON-LD only, a PPP loan matched less surely (ppp "confidence" medium), or the
            figures disagree (website vs PPP, or people / locations suggest more)
    low     only minimums and hints (people listed, locations, reviews, hiring)
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session, selectinload

from .intent import apply_intent
from .models import Company
from .utils import own_domain

# --- thresholds ----------------------------------------------------------------------

BANDS = ("solo", "small", "medium", "large")  # smallest first
BAND_MIN_PEOPLE = {"solo": 1, "small": 2, "medium": 11, "large": 51}
BAND_LABELS = {"solo": "1 person", "small": "2-10 people", "medium": "11-50 people", "large": "51+ people"}

DISAGREE_RATIO = 3  # website and PPP figures this many times apart = they disagree
TEAM_MIN_PEOPLE = 2  # one name on the website is usually just the owner: tells nothing
LOCATIONS_BANDS = [(10, "large"), (3, "medium"), (2, "small")]  # locations -> at least this band
REVIEWS_BANDS = [(1500, "medium"), (300, "small")]  # Google reviews -> at least this band
MAX_EMPLOYEES = 100_000
MAX_LOCATIONS = 500
CHAIN_BRANCHES = 5  # more saved branches than this on one website = probably a chain or franchise brand
MAX_BRANCHES = 100  # stop counting branches here (more is shown as "100+")
OLDEST_FOUNDED = 1800

NO_INFO_REASON = "No size information yet"
SIZE_BATCH = 200  # companies per database commit in size_saved_companies
QUERY_CHUNK = 500  # values per "IN (...)" list in one query


@dataclass
class SizeEstimate:
    band: str | None = None  # solo|small|medium|large, None = unknown
    employees: int | None = None  # the headcount figure we used
    employees_source: str | None = None  # website|ppp|team_page
    confidence: str | None = None  # high|medium|low
    reasons: list[str] = field(default_factory=list)
    founded_year: int | None = None
    locations_count: int | None = None


def band_for(people: int | None) -> str | None:
    """1 -> 'solo', 8 -> 'small', 25 -> 'medium', 200 -> 'large', None/0 -> None"""
    if not people or people < 1:
        return None
    return next(band for band in reversed(BANDS) if people >= BAND_MIN_PEOPLE[band])


def _bigger(a: str | None, b: str | None) -> str | None:
    """The bigger of two bands (None = unknown counts as smallest)."""
    if a is None or b is None:
        return a or b
    return a if BANDS.index(a) >= BANDS.index(b) else b


def _band_text(band: str) -> str:
    return f"{band} ({BAND_LABELS[band]})"  # "small (2-10 people)"


def _whole_number(value, low: int, high: int) -> int | None:
    """value as an int when it is a whole number between low and high, else None."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _website_reason(people: int, origin: str | None, snippet: str | None) -> str:
    if origin == "jsonld":  # hidden data on the page, often left over from a website template
        return f"Website page data: {people:,} employees (may be a template value)"
    if snippet:
        return f'Website says: "{snippet.strip()}"'
    return f"Website says: {people:,} employees"


def _ppp_reason(ppp: dict, jobs: int | None) -> str:
    """'PPP loan 2021: 14 jobs reported (figure from 2020-21, matched by same name and ZIP)'"""
    year = str(ppp.get("date_approved") or "")[:4]
    label = f"PPP loan {year}".strip()
    how = f"matched by {ppp['match']}" if ppp.get("match") else ""
    if jobs is None:
        return f"{label} found, no headcount given" + (f" ({how})" if how else "")
    return f"{label}: {jobs:,} jobs reported (figure from 2020-21" + (f", {how})" if how else ")")


def _website_people(company: Company) -> int:
    """People with a name found on the company's website (the crawler's team pages)."""
    return sum(1 for c in company.contacts or [] if c.source == "website" and c.full_name)


def estimate_size(company: Company, branches: int | None = None) -> SizeEstimate:
    """How big the company is, with reasons (see the module docstring). Does not change it.
    branches = other saved companies with the same own website at other places."""
    # Website facts only count when they came from a good crawl. They are kept when a later
    # re-crawl fails (the worker never wipes them), and they are still true then.
    signals = company.website_signals or {}
    if not signals.get("pages_crawled"):
        signals = {}
    ppp = company.ppp or {}
    has_loan = "jobs" in ppp  # {} = no loan found, {"not_applicable": True} = not looked up
    est = SizeEstimate()
    reasons = est.reasons

    # 1-3. Headcount figures: the website and the PPP loan.
    site_people = _whole_number(signals.get("employees_stated"), 1, MAX_EMPLOYEES)
    site_origin = signals.get("employees_from")
    ppp_people = _whole_number(ppp.get("jobs"), 1, MAX_EMPLOYEES)  # 0 or blank = not given
    site_reason = _website_reason(site_people, site_origin, signals.get("employees_snippet")) if site_people else None
    ppp_reason = _ppp_reason(ppp, ppp_people) if has_loan else None
    ppp_confidence = "medium" if ppp.get("confidence") == "medium" else "high"  # how sure the match is

    if site_people and ppp_people:
        disagree = max(site_people, ppp_people) / min(site_people, ppp_people) > DISAGREE_RATIO
        if site_origin == "jsonld":  # the loan beats a template value
            est.employees, est.employees_source = ppp_people, "ppp"
            reasons += [ppp_reason, site_reason]
        else:  # the website text is current, the loan is from 2020-21
            est.employees, est.employees_source = site_people, "website"
            reasons += [site_reason, ppp_reason]
        est.confidence = "medium" if disagree else "high"
        if disagree:
            used = "website" if est.employees_source == "website" else "PPP"
            reasons.append(f"Website and PPP loan disagree ({site_people:,} vs {ppp_people:,}): using the {used} figure")
    elif site_people:
        est.employees, est.employees_source = site_people, "website"
        est.confidence = "medium" if site_origin == "jsonld" else "high"
        reasons.append(site_reason)
        if ppp_reason:
            reasons.append(ppp_reason)
    elif ppp_people:
        est.employees, est.employees_source, est.confidence = ppp_people, "ppp", ppp_confidence
        reasons.append(ppp_reason)
    elif ppp_reason:
        reasons.append(ppp_reason)  # a loan without a headcount: shown, but no figure
    est.band = band_for(est.employees)
    has_headcount = est.employees is not None  # from the website or the PPP loan

    # 4. People listed on the website: at least that many work there.
    team = _website_people(company)
    if team >= TEAM_MIN_PEOPLE:
        if est.employees is None:
            est.employees, est.employees_source, est.confidence = team, "team_page", "low"
            est.band = band_for(team)
            reasons.append(f"{team} people listed on the website (a minimum)")
        elif team > est.employees:
            reasons.append(f"{team} people listed on the website (more than the {est.employees:,} above)")
            est.employees, est.employees_source, est.confidence = team, "team_page", "medium"
            est.band = band_for(team)
        else:
            reasons.append(f"{team} people listed on the website")

    # Locations: a business with many branches can't be tiny.
    stated_locations = _whole_number(signals.get("locations_stated"), 2, MAX_LOCATIONS)
    saved_locations = branches + 1 if branches else None
    if branches and branches > CHAIN_BRANCHES:
        # Many saved listings link to this website: usually a chain or franchise brand's
        # website, while each store is its own small business. Not counted as locations.
        more = "+" if branches >= MAX_BRANCHES else ""
        reasons.append(f"{saved_locations}{more} saved listings share this website - may be a chain or franchise")
        saved_locations = None
    est.locations_count = max(filter(None, (stated_locations, saved_locations)), default=None)
    if est.locations_count:
        where = "website" if est.locations_count == stated_locations else "same website, other places"
        reason = f"{est.locations_count} locations ({where})"
        minimum = next((band for n, band in LOCATIONS_BANDS if est.locations_count >= n), None)
        if minimum and _bigger(est.band, minimum) != est.band:
            if has_headcount:
                # The headcount wins (the band and the number must match); the locations only
                # make us less sure of it.
                reason += f": more than the {est.employees:,} employees suggests"
                if est.confidence == "high":
                    est.confidence = "medium"
            else:
                reason += f": at least {_band_text(minimum)}"
                est.band = minimum
        reasons.append(reason)

    # Weak hints, only when we have no headcount at all.
    if est.employees is None:
        reviews = company.reviews_count or 0
        minimum = next((band for n, band in REVIEWS_BANDS if reviews >= n), None)
        if minimum and _bigger(est.band, minimum) != est.band:
            reasons.append(f"{reviews:,} Google reviews: at least {_band_text(minimum)}")
            est.band = minimum
    if signals.get("has_careers"):
        if est.employees is None and est.band is None:
            reasons.append(f"Hiring (careers page): at least {_band_text('small')}")
            est.band = "small"
        else:
            reasons.append("Hiring (careers page)")
    if est.band and est.confidence is None:
        est.confidence = "low"  # only minimums and hints

    est.founded_year = _whole_number(signals.get("founded_year"), OLDEST_FOUNDED, datetime.now(timezone.utc).year)
    if est.founded_year:
        reasons.append(f"Founded {est.founded_year}")

    if est.band is None:
        est.confidence = None
        reasons.append(NO_INFO_REASON)
    return est


def _same_city(a: str | None, b: str | None) -> bool | None:
    """True/False when both cities are known, None when one is missing."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return a == b if a and b else None


def _count_branches_among(company: Company, others) -> int:
    """How many of `others` (rows with .id .lat .lon .city, may include the company itself)
    are NOT the same place as `company`. Stops counting at MAX_BRANCHES."""
    from .dedupe import DOMAIN_MATCH_MAX_M, distance_m  # here: dedupe imports scoring, which imports us

    count = 0
    for other in others:
        if count >= MAX_BRANCHES:
            break
        if company.id is not None and other.id == company.id:
            continue
        d = distance_m(company, other)  # the row has .lat and .lon like a Company
        if d is not None:
            count += d > DOMAIN_MATCH_MAX_M
        elif _same_city(company.city, other.city) is False:
            count += 1
    return count


def count_branches(session: Session, company: Company) -> int:
    """Other saved companies with the same own website that are NOT the same place:
    farther apart than dedupe.DOMAIN_MATCH_MAX_M, or (without map positions) in another city.
    'mcdonalds.com' in 40 places = 39 branches; a Facebook page or Wix site = 0.
    At most MAX_BRANCHES."""
    domain = own_domain(company.domain)
    if not domain:
        return 0
    stmt = select(Company.id, Company.lat, Company.lon, Company.city).where(Company.domain == domain)
    with session.no_autoflush:  # only read: never save half-finished changes from here
        others = session.execute(stmt).all()
    return _count_branches_among(company, others)


def branch_counts(session: Session, companies: list[Company]) -> dict[int, int]:
    """count_branches for many companies at once: one query for the whole batch instead of
    one per company. Returns {company id: branches}."""
    counts = {c.id: 0 for c in companies}
    by_domain: dict[str, list[Company]] = {}
    for c in companies:
        domain = own_domain(c.domain)
        if domain:
            by_domain.setdefault(domain, []).append(c)
    rows_by_domain: dict[str, list] = {}
    domains = list(by_domain)
    with session.no_autoflush:  # only read: never save half-finished changes from here
        for i in range(0, len(domains), QUERY_CHUNK):
            stmt = select(Company.id, Company.domain, Company.lat, Company.lon, Company.city).where(
                Company.domain.in_(domains[i:i + QUERY_CHUNK]))
            for row in session.execute(stmt):
                rows_by_domain.setdefault(row.domain, []).append(row)
    for domain, members in by_domain.items():
        rows = rows_by_domain.get(domain, [])
        for c in members:
            counts[c.id] = _count_branches_among(c, rows)
    return counts


def apply_size(company: Company, branches: int | None = None) -> None:
    """Save the size estimate on the company (the caller commits). Called by
    scoring.rescore, so every place that rescores a company also refreshes its size.
    branches: already counted (branch_counts); None = count them now."""
    if branches is None:
        session = object_session(company)
        branches = count_branches(session, company) if session is not None else None
    est = estimate_size(company, branches)
    company.size_band, company.employees = est.band, est.employees
    company.employees_source, company.size_confidence = est.employees_source, est.confidence
    company.size_reasons = est.reasons
    company.founded_year, company.locations_count = est.founded_year, est.locations_count


def size_saved_companies(session: Session, limit: int | None = None) -> int:
    """Companies saved before sizing existed (size_reasons still empty) get a size now,
    committing every SIZE_BATCH companies. Afterwards every company has at least one reason
    (NO_INFO_REASON when nothing is known), so the next call skips them.
    limit: size at most this many in this call (the worker does a few at a time between
    jobs and calls again later); None = all of them. Returns how many companies were sized."""
    never_sized = func.coalesce(func.json_array_length(Company.size_reasons), 0) == 0
    stmt = select(Company).options(selectinload(Company.contacts)).where(never_sized).order_by(Company.id)
    n = last_id = 0
    while limit is None or n < limit:
        size = SIZE_BATCH if limit is None else min(SIZE_BATCH, limit - n)
        batch = list(session.scalars(stmt.where(Company.id > last_id).limit(size)))
        if not batch:
            break
        counts = branch_counts(session, batch)
        for company in batch:
            apply_size(company, counts[company.id])
            n += 1
        last_id = batch[-1].id
        session.commit()
    return n


def refresh_branch_sizes(session: Session, domains) -> int:
    """Size again every saved company whose own website is one of `domains`, so all
    branches of one business show the same number of locations (a search saves them one by
    one: the first one saved saw no others yet). The caller commits. Returns how many."""
    wanted = sorted({d for d in (own_domain(x) for x in domains) if d})
    n = 0
    for i in range(0, len(wanted), QUERY_CHUNK):
        companies = list(session.scalars(
            select(Company).options(selectinload(Company.contacts))  # estimate_size reads the people
            .where(Company.domain.in_(wanted[i:i + QUERY_CHUNK]))
        ))
        counts = branch_counts(session, companies)
        for company in companies:
            apply_size(company, counts[company.id])
            apply_intent(company)  # the size feeds the intent scores (app/intent.py)
            n += 1
    return n
