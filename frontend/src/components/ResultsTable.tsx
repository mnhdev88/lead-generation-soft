"use client";

import { Fragment, useEffect, useState } from "react";
import { AddToListButton } from "@/components/AddToListButton";
import {
  ContactEmail,
  DecisionMakerStar,
  EmailLine,
  emailStatusOf,
  formatDate,
  sortTeam,
} from "@/components/CompanyDrawer";
import { ExportMenu } from "@/components/ExportMenu";
import { LeadStatusSelect, ScoreBadge, SeniorityBadge } from "@/components/ui";
import {
  api,
  LEAD_STATUS_LABEL,
  LEAD_STATUSES,
  type Company,
  type CompanyFilters,
  type Contact,
  type EnrichStatus,
  type Job,
  type LeadStatus,
} from "@/lib/api";

const PAGE_SIZE = 50;
const COLUMNS = 10; // keep in sync with the <th> cells below (used by colSpan)
const TEAM_PREVIEW = 2; // people shown in the Team column before "Show all"

// A copyright year older than this suggests the website hasn't been updated in a while.
const OUTDATED_BEFORE_YEAR = new Date().getFullYear() - 2;

type Toggle = "any" | "yes" | "no";
const toBool = (t: Toggle) => (t === "any" ? undefined : t === "yes");

type CrawlFilter = "any" | EnrichStatus | "none";
type SortBy = NonNullable<CompanyFilters["sort"]>;

const isActive = (j: Job) => j.status === "queued" || j.status === "running";

/** What a running job is still going to do to this company: "crawl", "emails" or nothing.
 * Worked out from the active jobs, so a row stops showing "Queued…" as soon as its job ends
 * (even if the job failed before reaching it). */
type Pending = "crawl" | "emails" | null;
function pendingWork(c: Company, activeJobs: Job[], tableJobId: number | null): Pending {
  for (const j of activeJobs) {
    const ids = j.params.company_ids;
    // Hand-picked companies, or every result of the search shown in the table.
    const covers = ids ? ids.includes(c.id) : j.parent_job_id !== null && j.parent_job_id === tableJobId;
    if (!covers) continue;
    const started = Date.parse(j.created_at);
    const notDoneSince = (stamp: string | null) => stamp === null || Date.parse(stamp) < started;
    if (j.kind === "enrich" && c.website && (j.params.force || c.enrich_status !== "done") && notDoneSince(c.enriched_at))
      return "crawl";
    if (j.kind === "emails" && (c.website || c.emails.length > 0) && notDoneSince(c.emails_checked_at)) return "emails";
  }
  return null;
}

function ToggleFilter({ label, value, onChange }: { label: string; value: Toggle; onChange: (v: Toggle) => void }) {
  return (
    <label className="flex items-center gap-1.5 text-sm">
      <span className="text-[var(--muted)]">{label}</span>
      <select className="input w-auto py-1" value={value} onChange={(e) => onChange(e.target.value as Toggle)}>
        <option value="any">Any</option>
        <option value="yes">Has</option>
        <option value="no">Missing</option>
      </select>
    </label>
  );
}

export function ResultsTable({
  jobId,
  refreshKey,
  jobs,
  onJobCreated,
  onOpenCompany,
}: {
  jobId: number | null;
  refreshKey: number; // changes when a job finishes or makes progress
  jobs: Job[];
  onJobCreated: (job: Job) => void;
  onOpenCompany: (id: number) => void;
}) {
  const [q, setQ] = useState("");
  const [website, setWebsite] = useState<Toggle>("any");
  const [phone, setPhone] = useState<Toggle>("any");
  const [email, setEmail] = useState<Toggle>("any");
  const [crawl, setCrawl] = useState<CrawlFilter>("any");
  const [leadStatus, setLeadStatus] = useState<LeadStatus | "any">("any");
  const [minScore, setMinScore] = useState(0);
  const [sort, setSort] = useState<SortBy>("name");
  const [paging, setPaging] = useState({ key: "", page: 0 });
  const [reloadKey, setReloadKey] = useState(0); // bump to re-read the table after our own changes
  const [data, setData] = useState<{ total: number; items: Company[] }>({ total: 0, items: [] });
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set()); // companies whose team row is open
  // Ticked rows. Tied to the filters they were picked under, so changing a filter clears them.
  const [selection, setSelection] = useState<{ key: string; ids: Set<number> }>({ key: "", ids: new Set() });
  const [busy, setBusy] = useState<number | "page" | "bulk" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const filters: CompanyFilters = {
    job_id: jobId ?? undefined,
    q: q || undefined,
    has_website: toBool(website),
    has_phone: toBool(phone),
    has_email: toBool(email),
    enrich_status: crawl === "any" ? undefined : crawl,
    lead_status: leadStatus === "any" ? undefined : leadStatus,
    min_score: minScore || undefined,
    sort: sort === "name" ? undefined : sort,
  };
  const filterKey = JSON.stringify(filters);

  // Changing any filter jumps back to page 1. If live updates shrink the results
  // (e.g. "Not crawled" rows get crawled), stay on the last page that still exists.
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  const page = Math.min(paging.key === filterKey ? paging.page : 0, pages - 1);
  const setPage = (p: number) => setPaging({ key: filterKey, page: p });

  useEffect(() => {
    let cancelled = false;
    api
      .listCompanies(JSON.parse(filterKey), PAGE_SIZE, page * PAGE_SIZE)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [filterKey, page, refreshKey, reloadKey]);

  const activeJobs = jobs.filter(isActive);
  const pendingOf = (c: Company) => pendingWork(c, activeJobs, jobId);

  // "Crawl all on this page" skips companies without a website, already crawled, or already queued
  // (so clicking twice never creates a duplicate job).
  const pageToCrawl = data.items.filter((c) => c.website && c.enrich_status !== "done" && !pendingOf(c));

  const selected = selection.key === filterKey ? selection.ids : new Set<number>();
  const selectedIds = [...selected];
  const pageIds = data.items.map((c) => c.id);
  const allOnPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const someOnPageSelected = pageIds.some((id) => selected.has(id));

  function setSelected(ids: Set<number>) {
    setSelection({ key: filterKey, ids });
  }
  function toggleRow(id: number) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }
  function togglePage() {
    const next = new Set(selected);
    for (const id of pageIds) {
      if (allOnPageSelected) next.delete(id);
      else next.add(id);
    }
    setSelected(next);
  }

  /** Run an action that may fail, showing "Starting…" on the clicked button and any error. */
  async function run(busyKey: number | "page" | "bulk", action: () => Promise<void>, failMessage: string) {
    setBusy(busyKey);
    setActionError(null);
    setNotice(null);
    try {
      await action();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : failMessage);
    } finally {
      setBusy(null);
    }
  }

  const startCrawl = (ids: number[], force: boolean, busyKey: number | "page" | "bulk") =>
    run(busyKey, async () => onJobCreated(await api.enrichCompanies(ids, force)), "Could not start the crawl");

  const bulkFindEmails = () =>
    run("bulk", async () => onJobCreated(await api.findEmails(selectedIds)), "Could not start the email check");

  const bulkSetStatus = (status: LeadStatus) =>
    run(
      "bulk",
      async () => {
        const { changed } = await api.bulkStatus({ company_ids: selectedIds, lead_status: status });
        setNotice(`Status set to “${LEAD_STATUS_LABEL[status]}” for ${changed} ${changed === 1 ? "lead" : "leads"}.`);
        setReloadKey((k) => k + 1);
      },
      "Could not change the status",
    );

  // Change one row's status: show it at once, undo if saving fails.
  function patchRow(id: number, changes: Partial<Company>) {
    setData((d) => ({ ...d, items: d.items.map((x) => (x.id === id ? { ...x, ...changes } : x)) }));
  }
  async function changeStatus(c: Company, status: LeadStatus) {
    const before = c.lead_status;
    patchRow(c.id, { lead_status: status });
    setActionError(null);
    try {
      patchRow(c.id, await api.updateCompany(c.id, { lead_status: status }));
    } catch (err) {
      patchRow(c.id, { lead_status: before });
      setActionError(err instanceof Error ? err.message : "Could not change the status");
    }
  }

  function toggleTeam(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="card space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="mr-auto text-base font-semibold">
          Leads <span className="font-normal text-[var(--muted)]">({data.total})</span>
        </h2>
        <input
          className="input w-48 py-1"
          placeholder="Search name…"
          aria-label="Search by business name"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <ToggleFilter label="Website" value={website} onChange={setWebsite} />
        <ToggleFilter label="Phone" value={phone} onChange={setPhone} />
        <ToggleFilter label="Email" value={email} onChange={setEmail} />
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-[var(--muted)]">Crawl status</span>
          <select className="input w-auto py-1" value={crawl} onChange={(e) => setCrawl(e.target.value as CrawlFilter)}>
            <option value="any">Any</option>
            <option value="none">Not crawled</option>
            <option value="done">Crawled</option>
            <option value="failed">Failed</option>
            <option value="no_website">No website</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-[var(--muted)]">Lead status</span>
          <select
            className="input w-auto py-1"
            value={leadStatus}
            onChange={(e) => setLeadStatus(e.target.value as LeadStatus | "any")}
          >
            <option value="any">Any</option>
            {LEAD_STATUSES.map((s) => (
              <option key={s} value={s}>
                {LEAD_STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-[var(--muted)]">Min score</span>
          <select className="input w-auto py-1" value={minScore} onChange={(e) => setMinScore(Number(e.target.value))}>
            <option value={0}>Any</option>
            <option value={40}>40+</option>
            <option value={70}>70+</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-[var(--muted)]">Sort</span>
          <select className="input w-auto py-1" value={sort} onChange={(e) => setSort(e.target.value as SortBy)}>
            <option value="name">Name</option>
            <option value="score">Score (high first)</option>
            <option value="rating">Rating (high first)</option>
          </select>
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          className="btn-secondary"
          disabled={pageToCrawl.length === 0 || busy !== null}
          onClick={() =>
            startCrawl(
              pageToCrawl.map((c) => c.id),
              false,
              "page",
            )
          }
          title={
            pageToCrawl.length === 0
              ? "Every website on this page is already crawled or queued"
              : "Visit the websites on this page to find emails, phones and team members"
          }
        >
          {busy === "page" ? "Starting…" : `Crawl all on this page (${pageToCrawl.length})`}
        </button>
        <ExportMenu kind="companies" filters={filters} />
        {notice && (
          <span role="status" className="text-sm text-[var(--success)]">
            {notice}
          </span>
        )}
        {actionError && (
          <span role="alert" className="text-sm text-[var(--danger)]">
            {actionError}
          </span>
        )}
      </div>

      {selected.size > 0 && (
        <div
          className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--accent)] bg-[var(--accent-soft)] px-3 py-2"
          role="region"
          aria-label="Actions for the selected leads"
        >
          <span className="mr-1 text-sm font-medium">{selected.size} selected</span>
          <AddToListButton companyIds={selectedIds} onDone={() => setReloadKey((k) => k + 1)} size="sm" />
          <select
            className="input w-auto py-1 text-sm"
            aria-label="Set lead status of the selected leads"
            value=""
            disabled={busy !== null}
            onChange={(e) => {
              if (e.target.value) void bulkSetStatus(e.target.value as LeadStatus);
            }}
          >
            <option value="">Set status…</option>
            {LEAD_STATUSES.map((s) => (
              <option key={s} value={s}>
                {LEAD_STATUS_LABEL[s]}
              </option>
            ))}
          </select>
          <button
            className="btn-secondary"
            disabled={busy !== null}
            onClick={bulkFindEmails}
            title="Find and verify the emails of these businesses and their people"
          >
            Find emails
          </button>
          <button
            className="btn-secondary"
            disabled={busy !== null}
            onClick={() => startCrawl(selectedIds, false, "bulk")}
            title="Visit the websites of these businesses (ones without a website are skipped)"
          >
            Crawl
          </button>
          <button className="btn-secondary" onClick={() => setSelected(new Set())}>
            Clear
          </button>
          {busy === "bulk" && <span className="text-sm text-[var(--muted)]">Working…</span>}
        </div>
      )}

      {error && <p className="text-sm text-[var(--danger)]">Could not load leads: {error}. Is the backend running?</p>}

      {/* On small screens the table scrolls sideways inside the card. */}
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full min-w-[1280px] text-sm">
          <thead className="bg-[var(--muted-bg)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
            <tr>
              <th className="w-8 px-3 py-2">
                <input
                  type="checkbox"
                  className="checkbox"
                  aria-label="Select all leads on this page"
                  checked={allOnPageSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someOnPageSelected && !allOnPageSelected;
                  }}
                  disabled={pageIds.length === 0}
                  onChange={togglePage}
                />
              </th>
              <th className="px-3 py-2">Business</th>
              <th className="px-3 py-2">Score</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Phone</th>
              <th className="px-3 py-2">Emails</th>
              <th className="px-3 py-2">Website</th>
              <th className="px-3 py-2">Team</th>
              <th className="px-3 py-2">Crawl</th>
              <th className="px-3 py-2">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((c) => {
              const open = expanded.has(c.id) && c.contacts.length > 0;
              const pending = pendingOf(c);
              const isSelected = selected.has(c.id);
              return (
                <Fragment key={c.id}>
                  <tr
                    className={`border-t border-[var(--border)] align-top ${
                      isSelected ? "bg-[var(--accent-soft)]" : "hover:bg-[var(--muted-bg)]"
                    }`}
                  >
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        className="checkbox mt-0.5"
                        aria-label={`Select ${c.name}`}
                        checked={isSelected}
                        onChange={() => toggleRow(c.id)}
                      />
                    </td>
                    <td className="max-w-72 px-3 py-2">
                      <BusinessCell company={c} onOpen={() => onOpenCompany(c.id)} />
                    </td>
                    <td className="px-3 py-2">
                      <ScoreBadge score={c.score} reasons={c.score_reasons} />
                    </td>
                    <td className="px-3 py-2">
                      <LeadStatusSelect
                        value={c.lead_status}
                        onChange={(s) => void changeStatus(c, s)}
                        label={`Lead status of ${c.name}`}
                      />
                    </td>
                    <td className="whitespace-nowrap px-3 py-2">{c.phone ?? <Missing />}</td>
                    <td className="px-3 py-2">
                      <EmailsCell company={c} />
                    </td>
                    <td className="px-3 py-2">
                      <WebsiteCell company={c} />
                    </td>
                    <td className="px-3 py-2">
                      <TeamCell company={c} open={open} onToggle={() => toggleTeam(c.id)} />
                    </td>
                    <td className="whitespace-nowrap px-3 py-2">
                      <CrawlStatus company={c} pending={pending} />
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right">
                      {c.website && (
                        <button
                          className="btn-xs"
                          disabled={busy !== null || pending !== null}
                          onClick={() => startCrawl([c.id], c.enrich_status !== null, c.id)}
                          aria-label={`${c.enrich_status ? "Re-crawl" : "Crawl"} the website of ${c.name}`}
                        >
                          {busy === c.id ? "…" : c.enrich_status ? "Re-crawl" : "Crawl"}
                        </button>
                      )}
                    </td>
                  </tr>
                  {open && (
                    <tr className="bg-[var(--muted-bg)]">
                      <td colSpan={COLUMNS} className="px-3 pb-3 pt-1">
                        <TeamList contacts={c.contacts} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={COLUMNS} className="px-3 py-10 text-center text-[var(--muted)]">
                  {data.total === 0 && filterKey === "{}"
                    ? "No leads yet. Run a search on the left."
                    : "No leads match these filters."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-end gap-2 text-sm">
        <button className="btn-secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>
          Previous
        </button>
        <span className="text-[var(--muted)]">
          Page {page + 1} of {pages}
        </span>
        <button className="btn-secondary" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>
          Next
        </button>
      </div>
    </div>
  );
}

// ----- cells ------------------------------------------------------------------------

const BUSINESS_STATUS_LABEL: Record<string, string> = {
  CLOSED_TEMPORARILY: "Temporarily closed",
  CLOSED_PERMANENTLY: "Permanently closed",
};

function BusinessCell({ company: c, onOpen }: { company: Company; onOpen: () => void }) {
  const closed = c.business_status && c.business_status !== "OPERATIONAL";
  return (
    <>
      {/* The name opens the details panel. */}
      <button
        className="text-left font-medium hover:text-[var(--accent)] hover:underline"
        onClick={onOpen}
        aria-haspopup="dialog"
        title="Show details, people and notes"
      >
        {c.name}
      </button>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-[var(--muted)]">
        {c.category && <span className="capitalize">{c.category}</span>}
        {c.rating !== null && (
          <span title={`Rated ${c.rating} out of 5`}>
            <span className="text-[var(--warn)]">★</span> {c.rating.toFixed(1)}
            {c.reviews_count !== null && ` (${c.reviews_count.toLocaleString()})`}
          </span>
        )}
        {c.google_maps_url && (
          <a
            href={c.google_maps_url}
            target="_blank"
            rel="noreferrer"
            className="link"
            aria-label={`Open ${c.name} in Google Maps`}
          >
            Maps ↗
          </a>
        )}
        {c.list_ids.length > 0 && (
          <span className="badge badge-accent" title="Saved in lead lists">
            {c.list_ids.length} {c.list_ids.length === 1 ? "list" : "lists"}
          </span>
        )}
      </div>
      {closed && (
        <span className="badge badge-danger mt-1">
          {BUSINESS_STATUS_LABEL[c.business_status!] ?? c.business_status!.toLowerCase().replaceAll("_", " ")}
        </span>
      )}
      {c.address && <div className="mt-1 truncate text-xs text-[var(--muted)]" title={c.address}>{c.address}</div>}
    </>
  );
}

function EmailsCell({ company: c }: { company: Company }) {
  const best = c.email ?? c.emails[0];
  if (!best) return <Missing />;
  // Compare without case: a source may give "Info@Acme.com" while the list has "info@acme.com".
  const others = c.emails.filter((e) => e.toLowerCase() !== best.toLowerCase());
  return (
    <div className="space-y-0.5">
      <EmailLine email={best} status={emailStatusOf(c, best)} />
      {others.length > 0 && (
        // <details> gives an accessible show/hide without extra state.
        <details className="text-xs">
          <summary className="cursor-pointer text-[var(--muted)] hover:text-[var(--accent)]">
            +{others.length} more
          </summary>
          <ul className="mt-1 space-y-0.5">
            {others.map((e) => (
              <li key={e}>
                <EmailLine email={e} status={emailStatusOf(c, e)} />
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function WebsiteCell({ company: c }: { company: Company }) {
  // SSL / platform / outdated badges only mean something after a successful crawl.
  const signals = c.enrich_status === "done" ? (c.website_signals ?? {}) : {};
  const year = signals.copyright_year;
  const outdated = Boolean(year && year < OUTDATED_BEFORE_YEAR);
  const socials = Object.entries(c.socials ?? {});
  return (
    <div className="space-y-1">
      {c.website ? (
        <a href={c.website} target="_blank" rel="noreferrer" className="link break-all">
          {c.domain ?? c.website}
        </a>
      ) : (
        <span className="badge badge-warn">No website</span>
      )}
      {(signals.https === false || signals.platform || outdated) && (
        <div className="flex flex-wrap gap-1">
          {signals.https === false && (
            <span className="badge badge-warn" title="The site does not use HTTPS (no padlock in the browser)">
              No SSL
            </span>
          )}
          {signals.platform && (
            <span className="badge badge-muted" title="Website platform">
              {signals.platform}
            </span>
          )}
          {outdated && (
            <span className="badge badge-warn" title={`Copyright notice says ${year}: the site may be outdated`}>
              © {year} · outdated
            </span>
          )}
        </div>
      )}
      {socials.length > 0 && (
        <div className="flex flex-wrap gap-x-2 text-xs">
          {socials.map(([net, url]) => (
            <a
              key={net}
              href={url.startsWith("http") ? url : `https://${url}`}
              target="_blank"
              rel="noreferrer"
              className="link capitalize"
            >
              {net}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

/** Decision-makers first, with their seniority and email; the full team opens below the row. */
function TeamCell({ company: c, open, onToggle }: { company: Company; open: boolean; onToggle: () => void }) {
  if (c.contacts.length === 0) return <Missing />;
  const team = sortTeam(c.contacts);
  const n = team.length;
  return (
    <div className="min-w-52 space-y-1.5">
      {team.slice(0, TEAM_PREVIEW).map((p) => (
        <div key={p.id} className="text-xs">
          <div className="flex flex-wrap items-center gap-1">
            {p.is_decision_maker && <DecisionMakerStar />}
            <span className="text-sm font-medium">{p.full_name ?? "Unknown name"}</span>
            <SeniorityBadge seniority={p.seniority} />
          </div>
          <ContactEmail contact={p} />
        </div>
      ))}
      <button
        className="link text-xs"
        onClick={onToggle}
        aria-expanded={open}
        aria-label={`${open ? "Hide" : "Show"} all ${n} team members of ${c.name}`}
      >
        {open ? "Hide team ▴" : n > TEAM_PREVIEW ? `Show all ${n} ▾` : "Details ▾"}
      </button>
    </div>
  );
}

function CrawlStatus({ company: c, pending }: { company: Company; pending: Pending }) {
  if (pending === "crawl") return <span className="badge badge-accent">Queued…</span>;
  if (pending === "emails") return <span className="badge badge-accent">Checking emails…</span>;
  switch (c.enrich_status) {
    case "done":
      return (
        <span className="badge badge-success" title={c.enriched_at ? `Crawled ${formatDate(c.enriched_at)}` : undefined}>
          Crawled
        </span>
      );
    case "failed":
      return (
        <span className="badge badge-danger cursor-help" title={c.enrich_error ?? "The website could not be crawled"}>
          Failed
        </span>
      );
    case "no_website":
      return (
        <span className="text-[var(--muted)]" title="No website to crawl">
          —
        </span>
      );
    default:
      return c.website ? <span className="badge badge-muted">Not crawled</span> : <Missing />;
  }
}

function TeamList({ contacts }: { contacts: Contact[] }) {
  return (
    <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
      {sortTeam(contacts).map((p) => (
        <li key={p.id} className="rounded-lg border border-[var(--border)] bg-[var(--card)] px-3 py-2 text-xs">
          <div className="flex flex-wrap items-center gap-1">
            {p.is_decision_maker && <DecisionMakerStar />}
            <span className="text-sm font-medium">{p.full_name ?? "Unknown name"}</span>
            <SeniorityBadge seniority={p.seniority} />
          </div>
          {p.title && <div className="text-[var(--muted)]">{p.title}</div>}
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
            <ContactEmail contact={p} showDetails />
            {p.phone && <span>{p.phone}</span>}
            {p.linkedin_url && (
              <a href={p.linkedin_url} target="_blank" rel="noreferrer" className="link">
                LinkedIn ↗
              </a>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function Missing() {
  return <span className="text-[var(--muted)]">—</span>;
}
