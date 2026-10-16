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
};

export type EnrichStatus = "done" | "failed" | "no_website";

export type Company = {
  id: number;
  name: string;
  category: string | null;
  phone: string | null;
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
};

export type Person = Contact & { company: CompanyBrief | null; list_ids: number[] };

export type Activity = {
  id: number;
  company_id: number;
  contact_id: number | null;
  kind: "note" | "status";
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
  sort?: "name" | "score" | "rating"; // default "name"; score/rating sort high to low
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
