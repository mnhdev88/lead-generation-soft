"""Database tables.

companies   - businesses found by any source (local search now, B2B later)
contacts    - people working at a company (found by the website crawler / B2B sources)
search_jobs - background work: kind="search" (find businesses), "enrich" (crawl websites),
              "emails" (find + verify emails)
job_results - which companies each search returned
company_sources - extra source records of a company after duplicates were merged
email_checks, domains - email verification cache and per-domain facts (Step 4)
lead_lists, list_items, activities - saved lists, notes and status history (Step 6)
api_usage   - paid API calls per month, so we never exceed the free allowance
users, user_sessions - who may log in, and who is logged in right now
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SearchJob(Base):
    __tablename__ = "search_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="search", server_default=text("'search'"))  # search|enrich|emails
    query: Mapped[str] = mapped_column(String(200))
    location: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(50))
    limit: Mapped[int] = mapped_column(Integer, default=200)
    # search jobs: also crawl the websites of the results when the search finishes
    enrich: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # enrich jobs: the search job whose results are crawled (or None when params.company_ids is used)
    parent_job_id: Mapped[int | None] = mapped_column(ForeignKey("search_jobs.id"))
    # extra options, e.g. {"company_ids": [1, 2], "force": true, "find_emails": true,
    #                      "titles": ["owner", "manager"], "crawl": true}
    params: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed
    progress_message: Mapped[str | None] = mapped_column(String(300))
    total: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))  # items to process
    processed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))  # items done
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("source", "source_ref", name="uq_company_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    category: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(100))
    # Standard form of `phone` ("+923001234567"), None when it couldn't be read (app/phones.py).
    # Used to spot the same business found by two sources.
    phone_e164: Mapped[str | None] = mapped_column(String(20), index=True)
    # mobile|landline|landline_or_mobile|toll_free|voip|other|invalid, None = don't know
    phone_type: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(200))  # best email (from source or crawler)
    emails: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))  # all emails found
    website: Mapped[str | None] = mapped_column(String(500))
    domain: Mapped[str | None] = mapped_column(String(200), index=True)
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(100))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    rating: Mapped[float | None] = mapped_column(Float)
    reviews_count: Mapped[int | None] = mapped_column(Integer)
    opening_hours: Mapped[str | None] = mapped_column(String(300))
    google_maps_url: Mapped[str | None] = mapped_column(String(500))
    business_status: Mapped[str | None] = mapped_column(String(50))  # OPERATIONAL, CLOSED_TEMPORARILY...
    socials: Mapped[dict] = mapped_column(JSON, default=dict)
    # Website crawl results
    enrich_status: Mapped[str | None] = mapped_column(String(20))  # None|done|failed|no_website
    enrich_error: Mapped[str | None] = mapped_column(String(300))
    enriched_at: Mapped[datetime | None]
    # e.g. {"https": true, "platform": "WordPress", "copyright_year": 2019, "has_contact_form": true,
    #       "title": "...", "description": "...", "pages_crawled": 4, "final_url": "..."}
    website_signals: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    # Step 4: verification result per email in `emails`, e.g. {"info@x.com": "valid"}
    email_statuses: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    # Where each email in `emails` came from: "source" (the business listing), "website"
    # (the crawler) or "hunter". A new good crawl replaces the "website" ones.
    email_sources: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    emails_checked_at: Mapped[datetime | None]
    # Step 6: sales pipeline + lead score
    lead_status: Mapped[str] = mapped_column(String(20), default="new", server_default=text("'new'"))
    score: Mapped[int | None] = mapped_column(Integer, index=True)  # 0-100
    score_reasons: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # Company size (app/sizing.py). Every estimate comes with its reasons, like the score.
    size_band: Mapped[str | None] = mapped_column(String(10), index=True)  # solo|small|medium|large, None = unknown
    employees: Mapped[int | None] = mapped_column(Integer)  # best headcount figure we have
    # where `employees` comes from: website (the site says so) | ppp (PPP loan 2020-21) |
    # team_page (people listed on the website: a minimum)
    employees_source: Mapped[str | None] = mapped_column(String(20))
    size_confidence: Mapped[str | None] = mapped_column(String(10))  # high|medium|low
    size_reasons: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    founded_year: Mapped[int | None] = mapped_column(Integer)
    locations_count: Mapped[int | None] = mapped_column(Integer)  # branches (website + same-website companies)
    # Matching US PPP loan (app/ppp.py), e.g. {"jobs": 14, "date_approved": "2021-02-03",
    # "name": "SMILE DENTAL PLLC", "match": "name + zip", ...}; {} = checked, none found
    ppp: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    ppp_checked_at: Mapped[datetime | None]
    # Google Maps profile (app/sources/google_places.py) and what its reviews say (app/reviews.py)
    google_photos_count: Mapped[int | None] = mapped_column(Integer)  # Google returns at most 10
    # up to 5 reviews: [{"rating": 2, "text": "...", "publish_time": "2026-01-05T...", "relative": "a month ago"}]
    google_reviews: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # e.g. {"pain_points": {"booking": ["...hard to book..."]}, "likely_new": false, "newest_review": "..."}
    review_signals: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    search_rank: Mapped[int | None] = mapped_column(Integer)  # best position in a Google Maps search (1 = top)
    peer_reviews_median: Mapped[int | None] = mapped_column(Integer)  # typical review count of the same search
    # Google PageSpeed (app/pagespeed.py), mobile: {"performance": 38, "seo": 82, "checked_at": "..."}
    pagespeed: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    pagespeed_checked_at: Mapped[datetime | None]
    # Buying intent per service we sell (app/intent.py):
    # {"website": {"score": 65, "reasons": ["No SSL +15", ...]}, "seo": {...}, "software": {...}, "marketing": {...}}
    intent: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    intent_best: Mapped[str | None] = mapped_column(String(20))  # the service with the highest score
    intent_best_score: Mapped[int | None] = mapped_column(Integer, index=True)
    # The same scores as plain columns, so the list can be filtered and sorted by them.
    intent_website: Mapped[int | None] = mapped_column(Integer, index=True)
    intent_seo: Mapped[int | None] = mapped_column(Integer, index=True)
    intent_software: Mapped[int | None] = mapped_column(Integer, index=True)
    intent_marketing: Mapped[int | None] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(50))
    source_ref: Mapped[str] = mapped_column(String(200))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    contacts: Mapped[list["Contact"]] = relationship(back_populates="company", cascade="all, delete-orphan")

    @property
    def business_kind(self) -> str | None:
        """'appointments' | 'food' | 'retail' | None, as used by the intent scores (app/intent.py)."""
        from .intent import business_kind  # here, not at the top: intent.py imports this module

        return business_kind(self)
    activities: Mapped[list["Activity"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(String(200))
    # owner | c_level | director | manager | professional | staff  (see app/people/seniority.py)
    seniority: Mapped[str | None] = mapped_column(String(50))
    department: Mapped[str | None] = mapped_column(String(50))  # e.g. management, sales, medical
    is_decision_maker: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    email: Mapped[str | None] = mapped_column(String(200), index=True)
    email_status: Mapped[str | None] = mapped_column(String(20))  # valid|invalid|catch_all|unknown|risky
    email_source: Mapped[str | None] = mapped_column(String(50))  # website|pattern|hunter
    email_confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100
    email_checked_at: Mapped[datetime | None]
    phone: Mapped[str | None] = mapped_column(String(100))
    phone_type: Mapped[str | None] = mapped_column(String(20))  # same values as Company.phone_type
    linkedin_url: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str | None] = mapped_column(String(50))
    source_url: Mapped[str | None] = mapped_column(String(500))  # page where this person was found
    lead_status: Mapped[str] = mapped_column(String(20), default="new", server_default=text("'new'"))
    score: Mapped[int | None] = mapped_column(Integer)  # 0-100
    score_reasons: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    company: Mapped[Company | None] = relationship(back_populates="contacts")


class CompanySource(Base):
    """Other source records of a company: when OpenStreetMap and Google both list the same
    business, the two are merged into one company (app/dedupe.py) and the second one's
    (source, source_ref) is kept here, so the next search finds the merged company."""

    __tablename__ = "company_sources"
    __table_args__ = (UniqueConstraint("source", "source_ref", name="uq_company_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(50))
    source_ref: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class JobResult(Base):
    __tablename__ = "job_results"

    job_id: Mapped[int] = mapped_column(ForeignKey("search_jobs.id"), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), primary_key=True)


class EmailCheck(Base):
    """Cache of email verification results (one row per address), reused everywhere."""

    __tablename__ = "email_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20))  # valid|invalid|catch_all|unknown|risky
    reason: Mapped[str | None] = mapped_column(String(300))  # human-readable explanation
    is_role: Mapped[bool] = mapped_column(Boolean, default=False)  # info@, sales@...
    is_free: Mapped[bool] = mapped_column(Boolean, default=False)  # gmail, yahoo...
    is_disposable: Mapped[bool] = mapped_column(Boolean, default=False)
    mx_found: Mapped[bool | None] = mapped_column(Boolean)
    smtp_code: Mapped[int | None] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(default=utcnow)


class DomainInfo(Base):
    """What we know about an email domain: mail servers, catch-all, email pattern."""

    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(200), unique=True)
    mx_hosts: Mapped[list] = mapped_column(JSON, default=list)
    has_mx: Mapped[bool | None] = mapped_column(Boolean)
    accepts_all: Mapped[bool | None] = mapped_column(Boolean)  # catch-all: server accepts any address
    # e.g. "{first}.{last}", "{f}{last}", "{first}" (see app/emails/patterns.py)
    email_pattern: Mapped[str | None] = mapped_column(String(50))
    pattern_confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100
    pattern_source: Mapped[str | None] = mapped_column(String(20))  # website|hunter|smtp|guess
    checked_at: Mapped[datetime | None]
    # When Hunter.io was last asked about this domain (each search costs a credit, so a domain
    # is asked at most once per EMAIL_CHECK_MAX_AGE_DAYS, even if Hunter found no one).
    hunter_checked_at: Mapped[datetime | None]


class LeadList(Base):
    """A named list of saved leads (companies and/or people)."""

    __tablename__ = "lead_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    items: Mapped[list["ListItem"]] = relationship(back_populates="lead_list", cascade="all, delete-orphan")


class ListItem(Base):
    """One company OR one contact in a list."""

    __tablename__ = "list_items"
    __table_args__ = (UniqueConstraint("list_id", "company_id", "contact_id", name="uq_list_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("lead_lists.id"), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), index=True)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)

    lead_list: Mapped[LeadList] = relationship(back_populates="items")


class Activity(Base):
    """Timeline of a lead: notes and status changes."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    kind: Mapped[str] = mapped_column(String(20))  # note|status
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    company: Mapped[Company] = relationship(back_populates="activities")


class ApiUsage(Base):
    __tablename__ = "api_usage"
    __table_args__ = (UniqueConstraint("provider", "period", name="uq_usage_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))  # e.g. "google_places"
    period: Mapped[str] = mapped_column(String(7))  # "2026-10"
    count: Mapped[int] = mapped_column(Integer, default=0)


class User(Base):
    """Someone who may log in to the dashboard. Admins can also manage the other users."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # always lower case
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))  # argon2id, never the password itself
    role: Mapped[str] = mapped_column(String(20), default="member")  # admin|member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)  # wrong passwords in a row
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    password_changed_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserSession(Base):
    """One logged-in browser. The cookie holds a random token; we store only its SHA-256,
    so a stolen copy of the database can't be used to log in."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
