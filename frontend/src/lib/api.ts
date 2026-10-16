// Talks to the FastAPI backend. These types mirror backend/app/schemas.py.
// Same address as the dashboard: next.config.ts (or nginx on the server) forwards /api to the
// backend, so the login cookie is sent with every call.
export const API_URL = "";

export type JobStatus = "queued" | "running" | "done" | "failed";
export type JobKind = "search" | "enrich" | "emails"; // enrich = website crawl; emails = find + verify emails

export type Job = {
  id: number;
  kind: JobKind;
  query: string;
  location: string;
  source: string;
  limit: number;
  enrich: boolean; // search jobs: crawl websites automatically when the search finishes
  parent_job_id: number | null; // enrich/emails jobs: the job whose results they work on
  params: { company_ids?: number[]; force?: boolean; find_emails?: boolean; titles?: string[]; crawl?: boolean };
  status: JobStatus;
  progress_message: string | null;
  total: number;
  processed: number;
  result_count: number; // search: businesses found; enrich: websites that gave an email; emails: companies with a deliverable (verified or catch-all) email
  error: string | null;
  created_at: string;
  finished_at: string | null;
};

export const LEAD_STATUSES = ["new", "contacted", "replied", "interested", "won", "lost", "not_interested"] as const;
export type LeadStatus = (typeof LEAD_STATUSES)[number];
export const LEAD_STATUS_LABEL: Record<LeadStatus, string> = {
  new: "New",
  contacted: "Contacted",
  replied: "Replied",
  interested: "Interested",
  won: "Won",
  lost: "Lost",
  not_interested: "Not interested",
};

export type EmailStatus = "valid" | "invalid" | "catch_all" | "unknown" | "risky";
// What the phone check (backend/app/phones.py) found out about a number.
export type PhoneType = "mobile" | "landline" | "landline_or_mobile" | "toll_free" | "voip" | "other" | "invalid";
export const SENIORITIES = ["owner", "c_level", "director", "manager", "professional", "staff"] as const;
export type Seniority = (typeof SENIORITIES)[number];
export const SENIORITY_LABEL: Record<Seniority, string> = {
  owner: "Owner / Founder",
  c_level: "C-level",
  director: "Director",
  manager: "Manager",
  professional: "Professional",
  staff: "Staff",
};

export type Contact = {
  id: number;
  company_id: number | null;
  full_name: string | null;
  first_name: string | null;
  last_name: string | null;
  title: string | null;
  seniority: Seniority | null;
  department: string | null;
  is_decision_maker: boolean;
  email: string | null;
  email_status: EmailStatus | null;
  email_source: string | null; // website | pattern | hunter
  email_confidence: number | null; // 0-100
  email_checked_at: string | null;
  phone: string | null;
  phone_type: PhoneType | null;
  linkedin_url: string | null;
  source: string | null;
  source_url: string | null;
  lead_status: LeadStatus;
  score: number | null;
  score_reasons: string[];
};

// What the crawler learned about a company's website (all keys optional).
export type WebsiteSignals = {
  https?: boolean | null;
  platform?: string | null;
  copyright_year?: number | null;
  has_contact_form?: boolean;
  title?: string | null;
  description?: string | null;
  pages_crawled?: number;
  final_url?: string | null;
  status_code?: number | null;
  // Size clues the website states (backend/app/enrich/extract.py).
  employees_stated?: number | null; // "a team of 25" -> 25
  employees_from?: "text" | "jsonld" | null;
  employees_snippet?: string | null; // the sentence it came from
  founded_year?: number | null;
  founded_snippet?: string | null;
  locations_stated?: number | null; // "12 locations"
  has_careers?: boolean; // a careers / jobs / "we're hiring" page or section
  careers_url?: string | null;
};

export type EnrichStatus = "done" | "failed" | "no_website";

// How big a business is (backend/app/sizing.py).
export const SIZE_BANDS = ["solo", "small", "medium", "large"] as const;
export type SizeBand = (typeof SIZE_BANDS)[number];
export const SIZE_BAND_LABEL: Record<SizeBand, string> = {
  solo: "Solo (1)",
  small: "Small (2-10)",
  medium: "Medium (11-50)",
  large: "Large (51+)",
};
// Fewest people in each band. Same cut-offs as BAND_MIN_PEOPLE in backend/app/sizing.py.
const BAND_MIN_PEOPLE: Record<SizeBand, number> = { solo: 1, small: 2, medium: 11, large: 51 };

/** The band a headcount falls in: 1 -> "solo", 8 -> "small", 25 -> "medium", 200 -> "large", 0/null -> null. */
export function bandFor(people: number | null | undefined): SizeBand | null {
  if (!people || people < 1) return null;
  return [...SIZE_BANDS].reverse().find((band) => people >= BAND_MIN_PEOPLE[band]) ?? null;
}

// The reason the backend gives when nothing at all is known (NO_INFO_REASON in backend/app/sizing.py).
export const NO_SIZE_INFO_REASON = "No size information yet";
// Where the headcount came from.
export type EmployeesSource = "website" | "ppp" | "team_page";
export type SizeConfidence = "high" | "medium" | "low";

// A match in the US PPP loan data, 2020-21 (backend/app/ppp.py). Empty {} = no match / not checked.
// {not_applicable: true} = the company is outside the US or has no ZIP, so it was not looked up.
export type PppMatch = {
  not_applicable?: boolean;
  jobs?: number | null; // jobs the borrower reported (self-reported, capped at 500)
  date_approved?: string; // "YYYY-MM-DD"
  name?: string;
  address?: string;
  city?: string;
  state?: string;
  zip?: string;
  naics?: string;
  business_type?: string;
  amount?: number | null;
  loan_number?: string;
  draw?: "first" | "second";
  loans?: number; // how many loans this borrower has
  match?: string; // why we think it is the same business, e.g. "same name and ZIP"
  similarity?: number; // 0..1
  confidence?: "high" | "medium"; // high = same name at the same street address
};

export type Company = {
  id: number;
  name: string;
  category: string | null;
  phone: string | null;
  phone_type: PhoneType | null;
  email: string | null; // best email
  emails: string[]; // all emails, best first
  email_statuses: Record<string, EmailStatus>; // email -> verification result
  emails_checked_at: string | null;
  website: string | null;
  domain: string | null;
  address: string | null;
  city: string | null;
  country: string | null;
  lat: number | null;
  lon: number | null;
  rating: number | null;
  reviews_count: number | null;
  opening_hours: string | null;
  google_maps_url: string | null;
  business_status: string | null; // e.g. OPERATIONAL, CLOSED_TEMPORARILY
  socials: Record<string, string>;
  enrich_status: EnrichStatus | null; // null = never crawled
  enrich_error: string | null;
  enriched_at: string | null;
  website_signals: WebsiteSignals;
  lead_status: LeadStatus;
  score: number | null; // 0-100
  score_reasons: string[]; // e.g. ["Verified email +25", "No website: opportunity +15"]
  size_band: SizeBand | null; // null = size unknown
  employees: number | null;
  employees_source: EmployeesSource | null;
  size_confidence: SizeConfidence | null;
  size_reasons: string[]; // why we think it is this size
  founded_year: number | null;
  locations_count: number | null;
  ppp: PppMatch;
  list_ids: number[];
  source: string;
  contacts: Contact[]; // decision-makers / most senior first
};

export type CompanyBrief = {
  id: number;
  name: string;
  category: string | null;
  website: string | null;
  domain: string | null;
  phone: string | null;
  city: string | null;
  country: string | null;
  rating: number | null;
  reviews_count: number | null;
  size_band?: SizeBand | null;
  employees?: number | null;
  employees_source?: EmployeesSource | null;
  size_confidence?: SizeConfidence | null;
  size_reasons?: string[];
};

export type Person = Contact & { company: CompanyBrief | null; list_ids: number[] };

export type Activity = {
  id: number;
  company_id: number;
  contact_id: number | null;
  kind: "note" | "status" | "merge";
  text: string;
  created_at: string;
};

export type EmailCheck = {
  email: string;
  status: EmailStatus;
  reason: string | null;
  is_role: boolean;
  is_free: boolean;
  is_disposable: boolean;
  mx_found: boolean | null;
  smtp_code: number | null;
  checked_at: string;
};

export type LeadList = {
  id: number;
  name: string;
  description: string | null;
  company_count: number;
  contact_count: number;
  created_at: string;
};

export type Source = {
  name: string; // "osm" | "google"
  label: string;
  description: string;
  available: boolean;
  unavailable_reason: string | null;
};

export type Usage = {
  provider: string; // "google_places" | "hunter"
  period: string; // "YYYY-MM"
  count: number;
  limit: number;
  configured?: boolean; // false = no API key set, so this provider is never used
};

export type Role = "admin" | "member";
export type User = {
  id: number;
  email: string;
  name: string;
  role: Role;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
};

export type SheetsStatus = { configured: boolean; service_account_email: string | null; reason: string | null };

export type CompanyFilters = {
  job_id?: number;
  list_id?: number;
  q?: string;
  has_website?: boolean;
  has_phone?: boolean;
  has_email?: boolean;
  enrich_status?: EnrichStatus | "none"; // "none" = not crawled yet
  lead_status?: LeadStatus;
  min_score?: number;
  size_band?: SizeBand | "unknown"; // "unknown" = size not worked out
  sort?: "name" | "score" | "rating" | "employees"; // default "name"; the others sort high to low
};

export type PeopleFilters = {
  job_id?: number; // people at companies found by this search
  list_id?: number;
  company_id?: number;
  q?: string; // name, title or company name
  title?: string; // comma-separated keywords, any match: "owner,manager"
  seniority?: string; // comma-separated Seniority values: "owner,c_level"
  decision_makers?: boolean; // only is_decision_maker
  has_email?: boolean;
  email_status?: string; // comma-separated EmailStatus values: "valid,catch_all"
  lead_status?: LeadStatus;
  city?: string;
  category?: string;
  min_score?: number;
  sort?: "name" | "score" | "seniority"; // default "seniority" (most senior first)
};

export type NewJob = {
  query: string;
  location: string;
  limit: number;
  source: string;
  enrich: boolean;
  find_emails?: boolean; // after crawling, guess + verify emails
  titles?: string[]; // prospect search: job titles to look for
};

type QueryValue = string | number | boolean | undefined;

/** Sends you to the login page, then back here once logged in. */
export function goToLogin() {
  if (window.location.pathname === "/login") return;
  const here = window.location.pathname + window.location.search;
  // A full page load on purpose: nothing from the previous login stays in memory.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`/login?next=${encodeURIComponent(here)}`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "same-origin",
    // X-Requested-With proves the call comes from the dashboard (the backend refuses changes without it).
    headers: { "Content-Type": "application/json", "X-Requested-With": "leadgen", ...init?.headers },
  });
  // Logged out (session expired, or an admin deactivated you): back to the login page.
  if (res.status === 401 && !path.startsWith("/api/auth/")) goToLogin();
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export function filtersToQuery(filters: Record<string, QueryValue>, extra: Record<string, number> = {}) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...filters, ...extra })) {
    if (v !== undefined && v !== "") params.set(k, String(v));
  }
  return params.toString();
}

const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const patch = (body: unknown): RequestInit => ({ method: "PATCH", body: JSON.stringify(body) });

export const api = {
  // ----- login & users -----
  login: (email: string, password: string) => request<User>("/api/auth/login", post({ email, password })),
  logout: () => request<void>("/api/auth/logout", post({})),
  me: () => request<User>("/api/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/api/auth/password", post({ current_password, new_password })),
  listUsers: () => request<User[]>("/api/users"),
  createUser: (body: { email: string; name: string; password: string; role: Role }) =>
    request<User>("/api/users", post(body)),
  updateUser: (id: number, body: { name?: string; role?: Role; is_active?: boolean; password?: string }) =>
    request<User>(`/api/users/${id}`, patch(body)),

  listSources: () => request<Source[]>("/api/sources"),
  listUsage: () => request<Usage[]>("/api/usage"),

  // ----- jobs -----
  listJobs: () => request<Job[]>("/api/jobs"),
  createJob: (body: NewJob) => request<Job>("/api/jobs", post(body)),
  // Crawl the websites of every result of a finished search. force = also re-crawl ones already done.
  enrichJob: (jobId: number, force = false) => request<Job>(`/api/jobs/${jobId}/enrich`, post({ force })),
  // Crawl the websites of hand-picked companies.
  enrichCompanies: (companyIds: number[], force = false) =>
    request<Job>("/api/companies/enrich", post({ company_ids: companyIds, force })),
  // Find + verify emails for all results of a finished search (Step 4).
  findEmailsForJob: (jobId: number, force = false) =>
    request<Job>(`/api/jobs/${jobId}/find-emails`, post({ force })),
  // Find + verify emails for hand-picked companies. crawl=true = "Find decision-makers"
  // (crawl the website first if needed, then find + verify everyone's emails).
  findEmails: (companyIds: number[], opts: { force?: boolean; crawl?: boolean } = {}) =>
    request<Job>("/api/companies/find-emails", post({ company_ids: companyIds, ...opts })),
  findDecisionMakers: (companyId: number) =>
    request<Job>("/api/companies/find-emails", post({ company_ids: [companyId], crawl: true })),

  // ----- companies -----
  listCompanies: (filters: CompanyFilters, limit: number, offset: number) =>
    request<{ total: number; items: Company[] }>(`/api/companies?${filtersToQuery(filters, { limit, offset })}`),
  getCompany: (id: number) => request<Company>(`/api/companies/${id}`),
  updateCompany: (id: number, body: { lead_status?: LeadStatus }) =>
    request<Company>(`/api/companies/${id}`, patch(body)),
  listActivities: (companyId: number) => request<Activity[]>(`/api/companies/${companyId}/activities`),
  addNote: (companyId: number, text: string, contactId?: number) =>
    request<Activity>(`/api/companies/${companyId}/notes`, post({ text, contact_id: contactId ?? null })),
  exportUrl: (filters: CompanyFilters, format: "csv" | "xlsx" = "csv") =>
    `${API_URL}/api/companies/export.${format}?${filtersToQuery(filters)}`,

  // ----- people (Step 5) -----
  listPeople: (filters: PeopleFilters, limit: number, offset: number) =>
    request<{ total: number; items: Person[] }>(`/api/people?${filtersToQuery(filters, { limit, offset })}`),
  updatePerson: (id: number, body: { lead_status?: LeadStatus }) => request<Person>(`/api/people/${id}`, patch(body)),
  peopleExportUrl: (filters: PeopleFilters, format: "csv" | "xlsx" = "csv") =>
    `${API_URL}/api/people/export.${format}?${filtersToQuery(filters)}`,

  // Set the status of many companies and/or people at once.
  bulkStatus: (body: { company_ids?: number[]; contact_ids?: number[]; lead_status: LeadStatus }) =>
    request<{ changed: number }>("/api/status/bulk", post({ company_ids: [], contact_ids: [], ...body })),

  // ----- email verification (Step 4) -----
  verifyEmails: (emails: string[], force = false) => request<EmailCheck[]>("/api/emails/verify", post({ emails, force })),

  // ----- lists (Step 6) -----
  listLists: () => request<LeadList[]>("/api/lists"),
  createList: (name: string, description?: string) =>
    request<LeadList>("/api/lists", post({ name, description: description ?? null })),
  updateList: (id: number, body: { name?: string; description?: string | null }) =>
    request<LeadList>(`/api/lists/${id}`, patch(body)),
  deleteList: (id: number) => request<void>(`/api/lists/${id}`, { method: "DELETE" }),
  addToList: (listId: number, items: { company_ids?: number[]; contact_ids?: number[] }) =>
    request<{ changed: number }>(`/api/lists/${listId}/items`, post({ company_ids: [], contact_ids: [], ...items })),
  removeFromList: (listId: number, items: { company_ids?: number[]; contact_ids?: number[] }) =>
    request<{ changed: number }>(
      `/api/lists/${listId}/items/remove`,
      post({ company_ids: [], contact_ids: [], ...items }),
    ),

  // ----- scores & export (Step 6) -----
  recomputeScores: () => request<{ companies: number; contacts: number }>("/api/scores/recompute", post({})),
  sheetsStatus: () => request<SheetsStatus>("/api/export/sheets/status"),
  exportToSheets: (body: {
    kind: "companies" | "people";
    filters: CompanyFilters | PeopleFilters;
    spreadsheet: string;
    tab_name?: string;
  }) => request<{ url: string; tab_name: string; rows: number }>("/api/export/sheets", post(body)),
};
