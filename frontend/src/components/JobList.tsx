"use client";

import { useState } from "react";
import { api, type Job, type JobStatus } from "@/lib/api";

const STATUS_STYLE: Record<JobStatus, string> = {
  queued: "badge-muted",
  running: "badge-accent",
  done: "badge-success",
  failed: "badge-danger",
};

const SOURCE_LABEL: Record<string, string> = {
  osm: "OpenStreetMap",
  google: "Google Maps",
  crawler: "Website crawl",
  emails: "Email check",
};

const isActive = (job: Job) => job.status === "queued" || job.status === "running";

export function JobList({
  jobs,
  selectedId,
  onSelect,
  onJobCreated,
}: {
  jobs: Job[];
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  onJobCreated: (job: Job) => void;
}) {
  const [busyId, setBusyId] = useState<number | null>(null); // the search whose button was just clicked
  const [actionError, setActionError] = useState<{ jobId: number; message: string } | null>(null);

  // Website-crawl and email-check jobs are shown nested under the search they belong to.
  const ids = new Set(jobs.map((j) => j.id));
  const isChild = (j: Job) => j.kind !== "search" && j.parent_job_id !== null && ids.has(j.parent_job_id);
  const childrenOf = new Map<number, Job[]>();
  for (const j of jobs) {
    if (isChild(j)) childrenOf.set(j.parent_job_id!, [...(childrenOf.get(j.parent_job_id!) ?? []), j]);
  }
  const topLevel = jobs.filter((j) => !isChild(j));

  // Start a follow-up job (website crawl or email check) for a finished search.
  async function start(search: Job, create: () => Promise<Job>, failMessage: string) {
    setBusyId(search.id);
    setActionError(null);
    try {
      onJobCreated(await create());
    } catch (err) {
      setActionError({ jobId: search.id, message: err instanceof Error ? err.message : failMessage });
    } finally {
      setBusyId(null);
    }
  }
  const crawl = (search: Job, force: boolean) =>
    start(search, () => api.enrichJob(search.id, force), "Could not start the crawl");
  const verifyEmails = (search: Job) =>
    start(search, () => api.findEmailsForJob(search.id), "Could not start the email check");

  return (
    <div className="card space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">Searches</h2>
        {selectedId !== null && (
          <button className="text-xs text-[var(--accent)] hover:underline" onClick={() => onSelect(null)}>
            Show all leads
          </button>
        )}
      </div>
      {jobs.length === 0 && <p className="text-sm text-[var(--muted)]">No searches yet.</p>}
      <ul className="space-y-1.5">
        {topLevel.map((job) => {
          // A crawl or email check started from the results table (not tied to one search).
          if (job.kind !== "search") {
            return (
              <li key={job.id} className="rounded-lg border border-[var(--border)] px-3 py-2">
                <ChildJobInfo job={job} title={`${KIND_TITLE[job.kind]} · ${job.query}`} />
              </li>
            );
          }

          const children = childrenOf.get(job.id) ?? [];
          const crawls = children.filter((c) => c.kind === "enrich"); // newest first
          const emailChecks = children.filter((c) => c.kind === "emails");
          const busyChild = children.some(isActive);
          const lastCrawlFailed = crawls[0]?.status === "failed";
          const canStart = job.status === "done" && job.result_count > 0 && !busyChild;
          const selected = selectedId === job.id;
          const starting = busyId === job.id;

          return (
            <li
              key={job.id}
              className={`overflow-hidden rounded-lg border transition-colors ${
                selected ? "border-[var(--accent)] bg-[var(--accent-soft)]" : "border-[var(--border)]"
              }`}
            >
              <button
                onClick={() => onSelect(job.id)}
                aria-pressed={selected}
                className={`w-full px-3 py-2 text-left ${selected ? "" : "hover:bg-[var(--muted-bg)]"}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium capitalize">
                    {job.query} · <span className="font-normal">{job.location}</span>
                  </span>
                  <span className={`badge shrink-0 rounded-full ${STATUS_STYLE[job.status]}`}>
                    {job.status === "done" ? job.result_count : job.status}
                  </span>
                </div>
                <p className="mt-0.5 text-[11px] text-[var(--muted)]">{SOURCE_LABEL[job.source] ?? job.source}</p>
                {isActive(job) && job.progress_message && (
                  <p className="mt-1 truncate text-xs text-[var(--muted)]">{job.progress_message}</p>
                )}
                {job.status === "failed" && job.error && <p className="mt-1 text-xs text-[var(--danger)]">{job.error}</p>}
              </button>

              {children.length > 0 && (
                <ul className="mx-3 mb-2 space-y-1 border-l-2 border-[var(--border)] pl-2.5">
                  {children.map((child) => (
                    <li key={child.id}>
                      {/* Clicking a crawl or email check shows the results of its search. */}
                      <button
                        onClick={() => onSelect(job.id)}
                        className="w-full rounded-md px-1.5 py-1 text-left hover:bg-[var(--muted-bg)]"
                        aria-label={`${KIND_TITLE[child.kind]} for ${job.query} in ${job.location}: ${child.status}. Show results.`}
                      >
                        <ChildJobInfo job={child} title={KIND_TITLE[child.kind]} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              {canStart && (
                <div className="flex flex-wrap items-center gap-2 px-3 pb-2">
                  {crawls.length === 0 ? (
                    <button
                      className="btn-xs"
                      disabled={starting}
                      onClick={() => crawl(job, false)}
                      aria-label={`Find emails and team on the websites of ${job.query} in ${job.location}`}
                    >
                      {starting ? "Starting…" : "Find emails"}
                    </button>
                  ) : lastCrawlFailed ? (
                    <button
                      className="btn-xs"
                      disabled={starting}
                      onClick={() => crawl(job, false)}
                      title="Crawl the websites that were not crawled successfully"
                    >
                      {starting ? "Starting…" : "Retry crawl"}
                    </button>
                  ) : (
                    <button
                      className="btn-xs"
                      disabled={starting}
                      onClick={() => crawl(job, true)}
                      title="Visit every website again, including ones already crawled"
                    >
                      {starting ? "Starting…" : "Re-crawl"}
                    </button>
                  )}
                  {emailChecks.length === 0 && (
                    <button
                      className="btn-xs"
                      disabled={starting}
                      onClick={() => verifyEmails(job)}
                      title="Guess the emails of the people found and check with each mail server that they exist"
                      aria-label={`Verify emails for ${job.query} in ${job.location}`}
                    >
                      {starting ? "Starting…" : "Verify emails"}
                    </button>
                  )}
                </div>
              )}
              {actionError?.jobId === job.id && (
                <p className="px-3 pb-2 text-xs text-[var(--danger)]">{actionError.message}</p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const KIND_TITLE: Record<Job["kind"], string> = {
  search: "Search",
  enrich: "Website crawl",
  emails: "Email check",
  people: "Finding owners",
};

/** One website-crawl, email-check or owner-lookup job: label, status, and a progress bar while it runs. */
function ChildJobInfo({ job, title }: { job: Job; title: string }) {
  const pct = job.total > 0 ? Math.round((job.processed / job.total) * 100) : 0;
  const isEmails = job.kind === "emails";
  const isPeople = job.kind === "people";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5 text-xs font-medium">
          {isEmails ? <EnvelopeIcon /> : isPeople ? <PersonIcon /> : <GlobeIcon />}
          <span className="truncate">{title}</span>
        </span>
        <span
          className={`badge shrink-0 rounded-full ${STATUS_STYLE[job.status]}`}
          title={
            job.status !== "done"
              ? undefined
              : isEmails
                ? "Businesses with a deliverable email (verified, or on a server that accepts every address)"
                : isPeople
                  ? "Businesses where we found an owner or decision-maker"
                  : "Websites that gave at least one email"
          }
        >
          {job.status !== "done"
            ? job.status
            : isEmails
              ? `${job.result_count} deliverable`
              : isPeople
                ? `${job.result_count} found`
                : `${job.result_count} with email`}
        </span>
      </div>
      {job.status === "running" && job.total > 0 && (
        <div className="flex items-center gap-2">
          <div
            className="progress"
            role="progressbar"
            aria-label={`${KIND_TITLE[job.kind]} progress`}
            aria-valuemin={0}
            aria-valuemax={job.total}
            aria-valuenow={job.processed}
          >
            <div style={{ width: `${pct}%` }} />
          </div>
          <span className="shrink-0 text-[11px] tabular-nums text-[var(--muted)]">
            {job.processed}/{job.total}
          </span>
        </div>
      )}
      {job.status !== "failed" && job.progress_message && (
        <p className="text-[11px] leading-snug text-[var(--muted)]">{job.progress_message}</p>
      )}
      {job.status === "failed" && job.error && <p className="text-[11px] text-[var(--danger)]">{job.error}</p>}
    </div>
  );
}

function GlobeIcon() {
  return (
    <svg aria-hidden viewBox="0 0 16 16" className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" fill="none" stroke="currentColor" strokeWidth="1.4">
      <circle cx="8" cy="8" r="6.25" />
      <path d="M1.75 8h12.5M8 1.75c1.8 1.9 2.6 4 2.6 6.25S9.8 12.35 8 14.25C6.2 12.35 5.4 10.25 5.4 8S6.2 3.65 8 1.75Z" />
    </svg>
  );
}

function EnvelopeIcon() {
  return (
    <svg aria-hidden viewBox="0 0 16 16" className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" fill="none" stroke="currentColor" strokeWidth="1.4">
      <rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.5" />
      <path d="m2.25 4 5.75 4.5L13.75 4" />
    </svg>
  );
}

function PersonIcon() {
  return (
    <svg aria-hidden viewBox="0 0 16 16" className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" fill="none" stroke="currentColor" strokeWidth="1.4">
      <circle cx="8" cy="5.25" r="2.75" />
      <path d="M2.75 14c.6-2.9 2.7-4.25 5.25-4.25s4.65 1.35 5.25 4.25" />
    </svg>
  );
}
