"""What wins: which lead signals actually come with good results, from the statuses you set.

A business you worked on has an outcome:
    good      replied, interested or won (the business or one of its people)
    bad       lost, not interested, or contacted more than NO_REPLY_DAYS ago without a reply
    (open)    new, or contacted recently: not counted yet
For each signal (verified email, no website, slow site, size, chain...) we compare the share of
good outcomes among the leads WITH the signal to the share among those WITHOUT it. A signal
whose leads do clearly better is worth more points; one whose leads do worse is worth fewer.
Only signals seen on at least MIN_LEADS worked leads are shown: fewer is just luck.

The scores are not changed automatically: the report says what to change, and you decide.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .intent import OFFER_LABELS, broken_site
from .models import Activity, Company
from .scoring import _best_company_grade

GOOD = {"replied", "interested", "won"}
BAD = {"lost", "not_interested"}
STATUS_RANK = {"won": 6, "interested": 5, "replied": 4, "not_interested": 3, "lost": 2, "contacted": 1, "new": 0}
NO_REPLY_DAYS = 14  # contacted this long ago and still no reply = a bad outcome
MIN_LEADS = 5  # signals seen on fewer worked leads are not shown
STRONG_LIFT = 0.10  # 10 percentage points better / worse = worth changing its points
STRONG_MIN_LEADS = 10  # ...when seen on at least this many worked leads
SCORE_BUCKETS = [(80, 101, "80-100"), (60, 80, "60-79"), (40, 60, "40-59"), (0, 40, "0-39")]


@dataclass
class SignalStat:
    signal: str
    leads: int  # worked leads with the signal
    good: int
    rate: float  # good / leads
    rate_without: float | None  # among worked leads without it
    lift: float | None  # rate - rate_without
    advice: str | None


@dataclass
class Insights:
    worked: int = 0  # leads with an outcome
    good: int = 0
    open: int = 0  # contacted recently, waiting for a reply
    baseline: float | None = None
    signals: list[SignalStat] = field(default_factory=list)
    by_score: list[dict] = field(default_factory=list)  # [{"band": "80-100", "leads": 12, "rate": 0.4}]


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def outcome(company: Company, contacted_at: datetime | None, now: datetime) -> str | None:
    """good | bad | None (not worked, or still waiting for a reply)."""
    statuses = [company.lead_status or "new"] + [p.lead_status or "new" for p in company.contacts or []]
    best = max(statuses, key=lambda s: STATUS_RANK.get(s, 0))
    if best in GOOD:
        return "good"
    if best in BAD:
        return "bad"
    if best == "contacted":
        when = _aware(contacted_at)
        if when is not None and now - when >= timedelta(days=NO_REPLY_DAYS):
            return "bad"
    return None


def signals_of(company: Company) -> set[str]:
    """The signals a lead has, in plain words (each one a row of the report)."""
    s: set[str] = set()
    grade = _best_company_grade(company)
    s.add({"valid": "Verified email", "catch_all": "Catch-all email", "unverified": "Unverified email"}
          .get(grade, "No usable email"))
    if any(p.is_decision_maker and p.email and p.email_status != "invalid" for p in company.contacts or []):
        s.add("Decision-maker with email")
    if company.phone_type == "mobile":
        s.add("Mobile number")
    signals = company.website_signals or {}
    if not company.website:
        s.add("No website")
    elif broken_site(company):
        s.add("Broken website (parked, placeholder...)")
    elif company.enrich_status == "done":
        if signals.get("https") is False:
            s.add("No SSL")
        if signals.get("has_viewport") is False:
            s.add("Not mobile-friendly")
    perf = (company.pagespeed or {}).get("performance")
    if isinstance(perf, (int, float)) and perf < 50:
        s.add("Very slow website")
    if company.rating is not None:
        s.add("Rating 4.5+" if company.rating >= 4.5 else "Rating below 4.5")
    if company.reviews_count is not None and company.reviews_count < 20:
        s.add("Fewer than 20 reviews")
    if company.size_band:
        s.add(f"Size: {company.size_band}")
    if company.chain:
        s.add("Chain / franchise")
    if company.fit:
        s.add("Ideal customer" if company.fit == "good" else "Poor fit")
    for offer, label in OFFER_LABELS.items():
        score = getattr(company, f"intent_{offer}", None)
        if score is not None and score >= 60:
            s.add(f"Hot for {label}")
    if company.category:
        s.add(f"Type: {company.category.strip().lower()}")
    if company.source:
        s.add(f"Found on {'Google Maps' if company.source == 'google' else 'OpenStreetMap' if company.source == 'osm' else company.source}")
    return s


def _advice(lift: float | None, leads: int) -> str | None:
    if lift is None or leads < STRONG_MIN_LEADS:
        return None
    if lift >= STRONG_LIFT:
        return "Works for you: worth more points"
    if lift <= -STRONG_LIFT:
        return "Doesn't help: worth fewer points"
    return None


def build(session: Session, now: datetime | None = None) -> Insights:
    now = now or datetime.now(timezone.utc)
    # When each business was last marked "Contacted" (its timeline), for "no reply yet".
    contacted: dict[int, datetime] = {}
    for company_id, created in session.execute(
        select(Activity.company_id, Activity.created_at).where(Activity.kind == "status",
                                                              Activity.text.like("%→ Contacted%"))
    ):
        if company_id not in contacted or _aware(created) > _aware(contacted[company_id]):
            contacted[company_id] = created

    out = Insights()
    rows: list[tuple[bool, set[str], int | None]] = []
    stmt = select(Company).options(selectinload(Company.contacts))
    for company in session.scalars(stmt.execution_options(yield_per=500)):
        result = outcome(company, contacted.get(company.id), now)
        if result is None:
            statuses = {company.lead_status} | {p.lead_status for p in company.contacts or []}
            if "contacted" in statuses:
                out.open += 1
            continue
        rows.append((result == "good", signals_of(company), company.score))
    out.worked = len(rows)
    out.good = sum(1 for good, _, _ in rows if good)
    if not rows:
        return out
    out.baseline = out.good / out.worked

    seen = Counter(sig for _, sigs, _ in rows for sig in sigs)
    wins = Counter(sig for good, sigs, _ in rows if good for sig in sigs)
    for sig, n in seen.items():
        if n < MIN_LEADS:
            continue
        rest = out.worked - n
        rate = wins[sig] / n
        rate_without = (out.good - wins[sig]) / rest if rest else None
        lift = rate - rate_without if rate_without is not None else None
        out.signals.append(SignalStat(sig, n, wins[sig], rate, rate_without, lift, _advice(lift, n)))
    out.signals.sort(key=lambda x: (x.lift is None, -(x.lift or 0), -x.leads))

    for low, high, label in SCORE_BUCKETS:
        in_band = [good for good, _, score in rows if score is not None and low <= score < high]
        if in_band:
            out.by_score.append({"band": label, "leads": len(in_band), "rate": sum(in_band) / len(in_band)})
    return out
