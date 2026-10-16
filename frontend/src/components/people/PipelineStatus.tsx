"use client";

import type { Job } from "@/lib/api";

export const isActive = (j: Job) => j.status === "queued" || j.status === "running";

/** The newest follow-up job of a given kind for a search (website crawl or email check). */
export function childJob(jobs: Job[], searchId: number, kind: Job["kind"]): Job | null {
  // Jobs come newest first, so the first match is the latest run.
  return jobs.find((j) => j.kind === kind && j.parent_job_id === searchId) ?? null;
}

type StepState = "waiting" | "running" | "done" | "failed" | "skipped";

type Step = { label: string; state: StepState; detail: string; processed?: number; total?: number };

/** Turn the three jobs (search → crawl → email check) into three steps to show. */
function stepsFor(search: Job, crawl: Job | null, emails: Job | null): Step[] {
  const stateOf = (j: Job | null, fallback: StepState): StepState =>
    !j ? fallback : j.status === "queued" ? "waiting" : j.status === "running" ? "running" : j.status;

  const searchStep: Step = {
    label: "Find businesses",
    state: stateOf(search, "waiting"),
    detail:
      search.status === "done"
        ? `${search.result_count} found`
        : search.status === "failed"
          ? (search.error ?? "Search failed")
          : (search.progress_message ?? "Searching…"),
  };

  // Nothing to crawl or check if the search failed or found nothing.
  const nothingToDo = search.status === "failed" || (search.status === "done" && search.result_count === 0);
  const later: StepState = nothingToDo ? "skipped" : "waiting";

  // Searches started without "crawl websites" / "find emails" never queue those steps.
  const wantsCrawl = search.enrich;
  const crawlLater: StepState = nothingToDo || !wantsCrawl ? "skipped" : "waiting";
  const crawlStep: Step = {
    label: "Crawl websites",
    state: stateOf(crawl, crawlLater),
    detail: crawl
      ? crawl.status === "failed"
        ? (crawl.error ?? "Crawl failed")
        : (crawl.progress_message ?? "Waiting…")
      : !wantsCrawl && !nothingToDo
        ? "Not requested for this search"
        : later === "skipped"
          ? "Skipped"
          : "Starts after the search",
    processed: crawl?.processed,
    total: crawl?.total,
  };

  const wantsEmails = search.params.find_emails === true;
  const emailLater: StepState = !wantsEmails || nothingToDo || crawl?.status === "failed" ? "skipped" : "waiting";
  const emailStep: Step = {
    label: "Find & verify emails",
    state: stateOf(emails, emailLater),
    detail: emails
      ? emails.status === "failed"
        ? (emails.error ?? "Email check failed")
        : emails.status === "done"
          ? `${emails.result_count} with a deliverable email`
          : (emails.progress_message ?? "Waiting…")
      : !wantsEmails && !nothingToDo
        ? "Not requested for this search"
        : emailLater === "skipped"
          ? "Skipped"
          : "Starts after the crawl",
    processed: emails?.processed,
    total: emails?.total,
  };

  return [searchStep, crawlStep, emailStep];
}

const ICON: Record<StepState, { symbol: string; cls: string; label: string }> = {
  waiting: { symbol: "○", cls: "text-[var(--muted)]", label: "Waiting" },
  running: { symbol: "◐", cls: "text-[var(--accent)] motion-safe:animate-spin", label: "Running" },
  done: { symbol: "✓", cls: "text-[var(--success)]", label: "Done" },
  failed: { symbol: "✕", cls: "text-[var(--danger)]", label: "Failed" },
  skipped: { symbol: "–", cls: "text-[var(--muted)]", label: "Skipped" },
};

/** Progress of a people search: search → website crawl → email check. */
export function PipelineStatus({ search, jobs }: { search: Job; jobs: Job[] }) {
  const crawl = childJob(jobs, search.id, "enrich");
  const emails = childJob(jobs, search.id, "emails");
  const steps = stepsFor(search, crawl, emails);
  const running = [search, crawl, emails].some((j) => j && isActive(j));
  const titles = search.params.titles ?? [];

  return (
    <section className="card space-y-3" aria-label="Progress of this people search" aria-live="polite">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h2 className="text-base font-semibold">
          {search.query} <span className="font-normal text-[var(--muted)]">in</span> {search.location || "selected companies"}
        </h2>
        {titles.length > 0 && <span className="text-sm text-[var(--muted)]">· {titles.join(", ")}</span>}
        <span className={`badge ml-auto ${running ? "badge-accent" : "badge-muted"}`}>
          {running ? "Running" : "Finished"}
        </span>
      </div>
      <ol className="grid gap-3 md:grid-cols-3">
        {steps.map((s, i) => {
          const icon = ICON[s.state];
          const showBar = s.state === "running" && s.total !== undefined && s.total > 0;
          const pct = showBar ? Math.min(100, Math.round(((s.processed ?? 0) / s.total!) * 100)) : 0;
          return (
            <li key={s.label} className="rounded-lg border border-[var(--border)] px-3 py-2">
              <div className="flex items-center gap-2 text-sm font-medium">
                <span aria-hidden className={`inline-block w-4 text-center ${icon.cls}`}>
                  {icon.symbol}
                </span>
                <span>
                  {i + 1}. {s.label}
                </span>
                <span className="sr-only">: {icon.label}</span>
                {showBar && (
                  <span className="ml-auto text-xs tabular-nums text-[var(--muted)]">
                    {s.processed}/{s.total}
                  </span>
                )}
              </div>
              {showBar && (
                <div
                  className="progress mt-1.5"
                  role="progressbar"
                  aria-label={s.label}
                  aria-valuemin={0}
                  aria-valuemax={s.total}
                  aria-valuenow={s.processed}
                >
                  <div style={{ width: `${pct}%` }} />
                </div>
              )}
              <p
                className={`mt-1 line-clamp-2 text-xs ${s.state === "failed" ? "text-[var(--danger)]" : "text-[var(--muted)]"}`}
                title={s.detail}
              >
                {s.detail}
              </p>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
