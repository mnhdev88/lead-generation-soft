"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { AddToListButton } from "@/components/AddToListButton";
import { ExportMenu } from "@/components/ExportMenu";
import { CompanyCell, Missing, PersonEmailCell, PersonNameCell, PersonTitleCell } from "@/components/people/cells";
import { FindOnLinkedIn, LeadStatusSelect, ScoreBadge } from "@/components/ui";
import {
  api,
  LEAD_STATUS_LABEL,
  LEAD_STATUSES,
  SENIORITIES,
  SENIORITY_LABEL,
  type LeadStatus,
  type PeopleFilters,
  type Person,
  type Seniority,
} from "@/lib/api";

const PAGE_SIZE = 50;
const COLUMNS = 8; // keep in sync with the <th> cells below (used by colSpan)

type EmailFilter = "any" | "has" | "verified" | "deliverable";
type SortBy = NonNullable<PeopleFilters["sort"]>;

const EMAIL_FILTERS: Record<EmailFilter, Pick<PeopleFilters, "has_email" | "email_status">> = {
  any: {},
  has: { has_email: true },
  verified: { email_status: "valid" },
  deliverable: { email_status: "valid,catch_all" }, // catch-all domains usually accept mail too
};

export function PeopleTable({
  jobId,
  jobTitles,
  refreshKey,
  searching,
}: {
  jobId: number | null; // only people at the companies found by this search
  jobTitles: string[]; // the job titles that search was looking for
  refreshKey: number; // changes when the search makes progress
  searching: boolean; // the search / crawl / email check is still running
}) {
  const [q, setQ] = useState("");
  const [titleText, setTitleText] = useState("");
  const [everyone, setEveryone] = useState(false); // false = only people matching the search's job titles
  const [seniority, setSeniority] = useState<Seniority[]>([]);
  const [decisionMakers, setDecisionMakers] = useState(false);
  const [email, setEmail] = useState<EmailFilter>("any");
  const [leadStatus, setLeadStatus] = useState<LeadStatus | "any">("any");
  const [city, setCity] = useState("");
  const [category, setCategory] = useState("");
  const [minScore, setMinScore] = useState(0);
  const [sort, setSort] = useState<SortBy>("seniority");

  const [paging, setPaging] = useState({ key: "", page: 0 });
  const [reloadKey, setReloadKey] = useState(0); // bump to re-read after our own changes
  const [data, setData] = useState<{ total: number; items: Person[] }>({ total: 0, items: [] });
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<{ key: string; ids: Set<number> }>({ key: "", ids: new Set() });
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Typed keywords win; otherwise use the search's job titles unless "Everyone" is picked.
  const matchingTitles = !everyone && jobTitles.length > 0 ? jobTitles.join(",") : undefined;
  const filters: PeopleFilters = {
    job_id: jobId ?? undefined,
    q: q.trim() || undefined,
    title: titleText.trim() || matchingTitles,
    seniority: seniority.length ? seniority.join(",") : undefined,
    decision_makers: decisionMakers || undefined,
    ...EMAIL_FILTERS[email],
    lead_status: leadStatus === "any" ? undefined : leadStatus,
    city: city.trim() || undefined,
    category: category.trim() || undefined,
    min_score: minScore || undefined,
    sort: sort === "seniority" ? undefined : sort,
  };
  const filterKey = JSON.stringify(filters);

  // Filters the user set here (not counting the search and its job titles).
  const ownFilters =
    q.trim() !== "" ||
    titleText.trim() !== "" ||
    seniority.length > 0 ||
    decisionMakers ||
    email !== "any" ||
    leadStatus !== "any" ||
    city.trim() !== "" ||
    category.trim() !== "" ||
    minScore > 0;

  function clearFilters() {
    setQ("");
    setTitleText("");
    setSeniority([]);
    setDecisionMakers(false);
    setEmail("any");
    setLeadStatus("any");
    setCity("");
    setCategory("");
    setMinScore(0);
  }

  // Changing a filter goes back to page 1; if results shrink, stay on the last page that exists.
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  const page = Math.min(paging.key === filterKey ? paging.page : 0, pages - 1);
  const setPage = (p: number) => setPaging({ key: filterKey, page: p });

  useEffect(() => {
    let cancelled = false;
    api
      .listPeople(JSON.parse(filterKey), PAGE_SIZE, page * PAGE_SIZE)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setLoaded(true);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Request failed");
      });
    return () => {
      cancelled = true;
    };
  }, [filterKey, page, refreshKey, reloadKey]);

  // ----- selection (cleared when the filters change) -----
  const selected = selection.key === filterKey ? selection.ids : new Set<number>();
  const selectedIds = [...selected];
  const pageIds = data.items.map((p) => p.id);
  const allOnPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const someOnPageSelected = pageIds.some((id) => selected.has(id));
  const setSelected = (ids: Set<number>) => setSelection({ key: filterKey, ids });

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

  function toggleSeniority(s: Seniority) {
    setSeniority((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));
  }

  // ----- status changes -----
  function patchRow(id: number, changes: Partial<Person>) {
    setData((d) => ({ ...d, items: d.items.map((x) => (x.id === id ? { ...x, ...changes } : x)) }));
  }
  async function changeStatus(p: Person, status: LeadStatus) {
    const before = p.lead_status;
    patchRow(p.id, { lead_status: status }); // show it at once…
    setActionError(null);
    try {
      patchRow(p.id, await api.updatePerson(p.id, { lead_status: status }));
    } catch (err) {
      patchRow(p.id, { lead_status: before }); // …and undo if saving fails
      setActionError(err instanceof Error ? err.message : "Could not change the status");
    }
  }
  async function bulkSetStatus(status: LeadStatus) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const { changed } = await api.bulkStatus({ contact_ids: selectedIds, lead_status: status });
      setNotice(`Status set to “${LEAD_STATUS_LABEL[status]}” for ${changed} ${changed === 1 ? "person" : "people"}.`);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Could not change the status");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card space-y-3" aria-labelledby="people-heading">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="people-heading" className="mr-auto text-base font-semibold">
          People <span className="font-normal text-[var(--muted)]">({data.total.toLocaleString()})</span>
        </h2>
        {jobTitles.length > 0 && (
          <div
            role="group"
            aria-label="Which people to show"
            className="inline-flex overflow-hidden rounded-lg border border-[var(--border)] text-sm"
          >
            {[
              { value: false, label: "Only matching titles", hint: jobTitles.join(", ") },
              { value: true, label: "Everyone at these companies", hint: "Ignore the job titles" },
            ].map((o) => (
              <button
                key={o.label}
                type="button"
                aria-pressed={everyone === o.value}
                title={o.hint}
                onClick={() => setEveryone(o.value)}
                className={`px-3 py-1 ${
                  everyone === o.value
                    ? "bg-[var(--accent-soft)] font-medium text-[var(--accent)]"
                    : "text-[var(--muted)] hover:bg-[var(--muted-bg)]"
                }`}
              >
                {o.label}
              </button>
            ))}
          </div>
        )}
        <ExportMenu kind="people" filters={filters} />
      </div>

      {/* ----- filters ----- */}
      <div className="space-y-2 rounded-lg bg-[var(--muted-bg)] p-3">
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input w-56 py-1"
            type="search"
            placeholder="Search name, title, company…"
            aria-label="Search people by name, title, email or company"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <input
            className="input w-48 py-1"
            placeholder="Title keywords: owner, manager"
            aria-label="Job title keywords, separated by commas (any of them)"
            title="Comma-separated; people matching any keyword are shown"
            value={titleText}
            onChange={(e) => setTitleText(e.target.value)}
          />
          <input
            className="input w-32 py-1"
            placeholder="City"
            aria-label="City"
            value={city}
            onChange={(e) => setCity(e.target.value)}
          />
          <input
            className="input w-36 py-1"
            placeholder="Business type"
            aria-label="Business type (category)"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          />
          <label className="flex items-center gap-1.5 text-sm">
            <span className="text-[var(--muted)]">Email</span>
            <select className="input w-auto py-1" value={email} onChange={(e) => setEmail(e.target.value as EmailFilter)}>
              <option value="any">Any</option>
              <option value="has">Has email</option>
              <option value="verified">Verified only</option>
              <option value="deliverable">Deliverable (verified + catch-all)</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-sm">
            <span className="text-[var(--muted)]">Status</span>
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
              <option value="seniority">Seniority</option>
              <option value="score">Score (high first)</option>
              <option value="name">Name</option>
            </select>
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Seniority">
          <span className="mr-1 text-sm text-[var(--muted)]">Seniority</span>
          {SENIORITIES.map((s) => {
            const on = seniority.includes(s);
            return (
              <button
                key={s}
                type="button"
                aria-pressed={on}
                onClick={() => toggleSeniority(s)}
                className={`chip normal-case ${on ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]" : "bg-[var(--card)]"}`}
              >
                {SENIORITY_LABEL[s]}
              </button>
            );
          })}
          <label className="ml-2 flex cursor-pointer items-center gap-1.5 text-sm">
            <input
              type="checkbox"
              className="checkbox"
              checked={decisionMakers}
              onChange={(e) => setDecisionMakers(e.target.checked)}
            />
            <span>
              <span className="text-[var(--warn)]" aria-hidden>
                ★
              </span>{" "}
              Decision-makers only
            </span>
          </label>
          {ownFilters && (
            <button type="button" className="link ml-auto text-sm" onClick={clearFilters}>
              Clear filters
            </button>
          )}
        </div>
      </div>

      {(notice || actionError) && (
        <p role={actionError ? "alert" : "status"} className={`text-sm ${actionError ? "text-[var(--danger)]" : "text-[var(--success)]"}`}>
          {actionError ?? notice}
        </p>
      )}

      {selected.size > 0 && (
        <div
          className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--accent)] bg-[var(--accent-soft)] px-3 py-2"
          role="region"
          aria-label="Actions for the selected people"
        >
          <span className="mr-1 text-sm font-medium">{selected.size} selected</span>
          <AddToListButton contactIds={selectedIds} onDone={() => setReloadKey((k) => k + 1)} size="sm" />
          <select
            className="input w-auto py-1 text-sm"
            aria-label="Set lead status of the selected people"
            value=""
            disabled={busy}
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
          <button type="button" className="btn-secondary" onClick={() => setSelected(new Set())}>
            Clear
          </button>
          {busy && <span className="text-sm text-[var(--muted)]">Working…</span>}
        </div>
      )}

      {error && (
        <p role="alert" className="text-sm text-[var(--danger)]">
          Could not load people: {error}. Is the backend running?
        </p>
      )}

      {/* On small screens the table scrolls sideways inside the card. */}
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full min-w-[1100px] text-sm">
          <thead className="bg-[var(--muted-bg)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
            <tr>
              <th className="w-8 px-3 py-2">
                <input
                  type="checkbox"
                  className="checkbox"
                  aria-label="Select everyone on this page"
                  checked={allOnPageSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someOnPageSelected && !allOnPageSelected;
                  }}
                  disabled={pageIds.length === 0}
                  onChange={togglePage}
                />
              </th>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Title</th>
              <th className="px-3 py-2">Company</th>
              <th className="px-3 py-2">Email</th>
              <th className="px-3 py-2">Phone</th>
              <th className="px-3 py-2">Score</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((p) => {
              const isSelected = selected.has(p.id);
              return (
                <tr
                  key={p.id}
                  className={`border-t border-[var(--border)] align-top ${
                    isSelected ? "bg-[var(--accent-soft)]" : "hover:bg-[var(--muted-bg)]"
                  }`}
                >
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      className="checkbox mt-0.5"
                      aria-label={`Select ${p.full_name ?? "this person"}`}
                      checked={isSelected}
                      onChange={() => toggleRow(p.id)}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <PersonNameCell person={p} />
                    {!p.linkedin_url && p.full_name && (
                      <FindOnLinkedIn name={p.full_name} companyName={p.company?.name} />
                    )}
                  </td>
                  <td className="max-w-60 px-3 py-2">
                    <PersonTitleCell person={p} />
                  </td>
                  <td className="max-w-64 px-3 py-2">
                    <CompanyCell company={p.company} />
                  </td>
                  <td className="px-3 py-2">
                    <PersonEmailCell person={p} />
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">{p.phone ?? <Missing />}</td>
                  <td className="px-3 py-2">
                    <ScoreBadge score={p.score} reasons={p.score_reasons} />
                  </td>
                  <td className="px-3 py-2">
                    <LeadStatusSelect
                      value={p.lead_status}
                      onChange={(s) => void changeStatus(p, s)}
                      label={`Lead status of ${p.full_name ?? "this person"}`}
                    />
                  </td>
                </tr>
              );
            })}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={COLUMNS} className="px-3 py-10 text-center text-[var(--muted)]">
                  {!loaded ? (
                    "Loading…"
                  ) : ownFilters ? (
                    <>
                      No people match these filters.{" "}
                      <button type="button" className="link" onClick={clearFilters}>
                        Clear filters
                      </button>
                    </>
                  ) : searching ? (
                    "Looking for people… They appear here as each website is crawled."
                  ) : matchingTitles ? (
                    <>
                      Nobody with these job titles was found at these companies.{" "}
                      <button type="button" className="link" onClick={() => setEveryone(true)}>
                        Show everyone at these companies
                      </button>
                    </>
                  ) : jobId !== null ? (
                    "No people were found on these companies' websites."
                  ) : (
                    <>
                      No people yet. Run a people search above, or crawl websites on the{" "}
                      <Link href="/" className="link">
                        Search page
                      </Link>
                      .
                    </>
                  )}
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
    </section>
  );
}
