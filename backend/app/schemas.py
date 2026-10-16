"""Shapes of data going in and out of the API. This file is the contract with the frontend
(frontend/src/lib/api.ts mirrors it)."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


def _as_utc(d: datetime) -> datetime:
    # SQLite forgets the timezone; all our times are UTC, so say so (JSON gets "+00:00").
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]

LeadStatus = Literal["new", "contacted", "replied", "interested", "won", "lost", "not_interested"]
LEAD_STATUSES: tuple[str, ...] = LeadStatus.__args__  # type: ignore[attr-defined]
EmailStatus = Literal["valid", "invalid", "catch_all", "unknown", "risky"]
Seniority = Literal["owner", "c_level", "director", "manager", "professional", "staff"]
SizeBand = Literal["solo", "small", "medium", "large"]  # app/sizing.py


# ----- jobs ----------------------------------------------------------------------


class JobCreate(BaseModel):
    query: str = Field(min_length=2, max_length=200, examples=["dentist"])
    location: str = Field(min_length=2, max_length=200, examples=["Karachi, Pakistan"])
    source: str = "osm"
    limit: int = Field(default=200, ge=1, le=2000)
    enrich: bool = False  # crawl result websites for emails when the search finishes
    find_emails: bool = False  # after crawling, guess + verify emails (implies enrich)
    # Prospect search: job titles/keywords the user is looking for, e.g. ["owner", "manager"].
    # Stored in params.titles; the People page filters by them.
    titles: list[str] = Field(default=[], max_length=20)


class EnrichCompaniesRequest(BaseModel):
    company_ids: list[int] = Field(min_length=1, max_length=2000)
    force: bool = False  # re-crawl even if already crawled


class EnrichJobRequest(BaseModel):
    force: bool = False


class FindEmailsRequest(BaseModel):
    """Find + verify emails for companies (and their people)."""

    company_ids: list[int] = Field(min_length=1, max_length=2000)
    force: bool = False  # re-check emails checked recently
    crawl: bool = False  # crawl websites first if not crawled yet ("find decision-makers")
    use_hunter: bool = True  # use Hunter.io as a backup when configured (counts against its cap)


class FindEmailsJobRequest(BaseModel):
    force: bool = False
    use_hunter: bool = True


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str  # search | enrich | emails
    query: str
    location: str
    source: str
    limit: int
    enrich: bool
    parent_job_id: int | None
    params: dict = {}
    status: str
    progress_message: str | None
    total: int
    processed: int
    result_count: int
    error: str | None
    created_at: UtcDatetime
    finished_at: UtcDatetime | None

    @field_validator("params", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


# ----- companies & people --------------------------------------------------------


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int | None = None
    full_name: str | None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None
    seniority: str | None = None
    department: str | None = None
    is_decision_maker: bool = False
    email: str | None
    email_status: str | None
    email_source: str | None = None
    email_confidence: int | None = None
    email_checked_at: UtcDatetime | None = None
    phone: str | None
    phone_type: str | None = None  # mobile|landline|...|invalid (app/phones.py)
    linkedin_url: str | None
    source: str | None
    source_url: str | None
    lead_status: str = "new"
    score: int | None = None
    score_reasons: list[str] = []

    @field_validator("score_reasons", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    @field_validator("is_decision_maker", mode="before")
    @classmethod
    def _none_to_false(cls, v):
        return bool(v)

    @field_validator("lead_status", mode="before")
    @classmethod
    def _default_status(cls, v):
        return v or "new"


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: str | None
    phone: str | None
    phone_type: str | None = None
    email: str | None
    emails: list[str] = []
    email_statuses: dict[str, str] = {}  # email -> valid|invalid|catch_all|unknown|risky
    emails_checked_at: UtcDatetime | None = None
    website: str | None
    domain: str | None
    address: str | None
    city: str | None
    country: str | None
    lat: float | None
    lon: float | None
    rating: float | None
    reviews_count: int | None
    opening_hours: str | None
    google_maps_url: str | None
    business_status: str | None
    socials: dict = {}
    enrich_status: str | None
    enrich_error: str | None
    enriched_at: UtcDatetime | None
    website_signals: dict = {}
    lead_status: str = "new"
    score: int | None = None
    score_reasons: list[str] = []
    # Company size (app/sizing.py): band None = unknown; every estimate has its reasons.
    size_band: SizeBand | None = None
    employees: int | None = None
    employees_source: str | None = None  # website | ppp | team_page
    size_confidence: str | None = None  # high | medium | low
    size_reasons: list[str] = []
    founded_year: int | None = None
    locations_count: int | None = None
    ppp: dict = {}  # matching US PPP loan (app/ppp.py), {} = none found / not checked
    list_ids: list[int] = []  # lists this company is in (filled by the router)
    source: str
    contacts: list[ContactOut] = []  # sorted: decision-makers / most senior first

    # Rows created before a column existed may hold NULL.
    @field_validator("emails", "contacts", "score_reasons", "size_reasons", "list_ids", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    @field_validator("socials", "website_signals", "email_statuses", "ppp", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}

    @field_validator("lead_status", mode="before")
    @classmethod
    def _default_status(cls, v):
        return v or "new"


class CompanyPage(BaseModel):
    total: int
    items: list[CompanyOut]


class CompanyBrief(BaseModel):
    """The company shown next to a person in the People view."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: str | None
    website: str | None
    domain: str | None
    phone: str | None
    city: str | None
    country: str | None
    rating: float | None = None
    reviews_count: int | None = None
    size_band: SizeBand | None = None
    employees: int | None = None
    employees_source: str | None = None  # website | ppp | team_page
    size_confidence: str | None = None  # high | medium | low
    size_reasons: list[str] = []

    @field_validator("size_reasons", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []


class PersonOut(ContactOut):
    company: CompanyBrief | None = None
    list_ids: list[int] = []

    @field_validator("list_ids", mode="before")
    @classmethod
    def _none_to_list_ids(cls, v):
        return v or []


class PersonPage(BaseModel):
    total: int
    items: list[PersonOut]


class CompanyUpdate(BaseModel):
    lead_status: LeadStatus | None = None


class ContactUpdate(BaseModel):
    lead_status: LeadStatus | None = None


class BulkStatusRequest(BaseModel):
    company_ids: list[int] = Field(default=[], max_length=5000)
    contact_ids: list[int] = Field(default=[], max_length=5000)
    lead_status: LeadStatus


class NoteCreate(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    contact_id: int | None = None


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    contact_id: int | None
    kind: str  # note | status | merge
    text: str
    created_at: UtcDatetime


# ----- email verification --------------------------------------------------------


class VerifyEmailsRequest(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=20)
    force: bool = False  # ignore cached results


class EmailCheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email: str
    status: str  # valid|invalid|catch_all|unknown|risky
    reason: str | None
    is_role: bool
    is_free: bool
    is_disposable: bool
    mx_found: bool | None
    smtp_code: int | None
    checked_at: UtcDatetime


# ----- lists ---------------------------------------------------------------------


class ListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class ListUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class ListOut(BaseModel):
    id: int
    name: str
    description: str | None
    company_count: int
    contact_count: int
    created_at: UtcDatetime


class ListItemsRequest(BaseModel):
    company_ids: list[int] = Field(default=[], max_length=5000)
    contact_ids: list[int] = Field(default=[], max_length=5000)


class ListItemsResult(BaseModel):
    changed: int  # items added (or removed)


# ----- sources, usage, export ----------------------------------------------------


class SourceOut(BaseModel):
    name: str
    label: str
    description: str
    available: bool
    unavailable_reason: str | None = None


class UsageOut(BaseModel):
    provider: str
    period: str
    count: int
    limit: int
    configured: bool = True  # False = no API key set, so this provider is never used


class SheetsStatus(BaseModel):
    configured: bool
    service_account_email: str | None = None
    reason: str | None = None  # why it isn't configured / what to do


class SheetsExportRequest(BaseModel):
    kind: Literal["companies", "people"]
    # Same filter names as the GET list endpoints (e.g. {"job_id": 4, "has_email": true}).
    filters: dict = {}
    # Google Sheets URL or spreadsheet id, shared (Editor) with the service account email.
    spreadsheet: str = Field(min_length=10, max_length=500)
    tab_name: str | None = Field(default=None, max_length=100)  # default: "Leads <date time>"


class SheetsExportResult(BaseModel):
    url: str
    tab_name: str
    rows: int


class ScoreRecomputeResult(BaseModel):
    companies: int
    contacts: int


# ----- login & users -------------------------------------------------------------


def _check_email(v: str) -> str:
    v = v.strip().lower()
    local, _, domain = v.partition("@")
    if not local or "." not in domain or " " in v:
        raise ValueError("Enter a valid email address.")
    return v


LoginEmail = Annotated[str, Field(min_length=3, max_length=254), AfterValidator(_check_email)]
Role = Literal["admin", "member"]


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=254)  # not validated: a typo is just a wrong login
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    role: Role
    is_active: bool
    last_login_at: UtcDatetime | None
    created_at: UtcDatetime


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class UserCreate(BaseModel):
    email: LoginEmail
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)
    role: Role = "member"


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=128)  # admin sets a new one
