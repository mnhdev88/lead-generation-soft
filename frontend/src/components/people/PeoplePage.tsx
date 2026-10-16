"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { PeopleTable } from "@/components/people/PeopleTable";
import { childJob, isActive, PipelineStatus } from "@/components/people/PipelineStatus";
import { ProspectForm } from "@/components/people/ProspectForm";
import { api, type Job } from "@/lib/api";

const FAST_POLL_MS = 2000; // while the selected search is still running
const SLOW_POLL_MS = 15000;

/** ?job_id=4 in the URL → 4 (anything else → null). */
function parseJobId(raw: string | null): number | null {
  const n = Number(raw);
  return raw && Number.isInteger(n) && n > 0 ? n : null;
}

// The selected search lives in the URL (?job_id=4), so the Search page can link here and
// Back/Forward work. Next.js keeps useSearchParams in sync with pushState.
function showJob(id: number | null) {
  window.history.pushState(null, "", id === null ? "/people" : `/people?job_id=${id}`);
}

/** Search + crawl + email jobs that belong to one people search. */
function pipelineOf(jobs: Job[], searchId: number | null): Job[] {
  if (searchId === null) return [];
  return jobs.filter((j) => j.id === searchId || j.parent_job_id === searchId);
}

function jobLabel(j: Job) {
  const titles = j.params.titles?.length ? ` · ${j.params.titles.join(", ")}` : "";
  return `#${j.id} ${j.query} in ${j.location}${titles}`;
}

export function PeoplePage() {
  const searchParams = useSearchParams();
  const jobId = parseJobId(searchParams.get("job_id"));

  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsLoaded, setJobsLoaded] = useState(false);
  const [offline, setOffline] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const progressRef = useRef(""); // last seen progress of the selected search

  const loadJobs = useCallback(async () => {
    try {
      const next = await api.listJobs();
      // Re-read the table whenever the selected search moves on (new step, more websites crawled…).
      const progress = pipelineOf(next, jobId)
        .map((j) => `${j.id}:${j.status}:${j.processed}`)
        .join(",");
      if (progress !== progressRef.current) {
        progressRef.current = progress;
        setRefreshKey((k) => k + 1);
      }
      setJobs(next);
      setJobsLoaded(true);
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, [jobId]);

  const search = jobId === null ? null : (jobs.find((j) => j.id === jobId && j.kind === "search") ?? null);
  const running = pipelineOf(jobs, jobId).some(isActive);
  // A finished search with "find emails" on may not have queued its next step yet: keep polling fast.
  const nextStepPending =
    search !== null &&
    search.status === "done" &&
    search.result_count > 0 &&
    search.enrich &&
    (childJob(jobs, search.id, "enrich") === null ||
      (search.params.find_emails === true &&
        childJob(jobs, search.id, "enrich")?.status === "done" &&
        childJob(jobs, search.id, "emails") === null));
  const fast = running || nextStepPending;

  useEffect(() => {
    const first = setTimeout(loadJobs, 0);
    const timer = setInterval(loadJobs, fast ? FAST_POLL_MS : SLOW_POLL_MS);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [loadJobs, fast]);

  function handleCreated(job: Job) {
    setJobs((prev) => [job, ...prev.filter((j) => j.id !== job.id)]);
    showJob(job.id);
  }

  // Recent searches to pick from (newest first).
  const searches = jobs.filter((j) => j.kind === "search" && j.status !== "failed").slice(0, 30);

  return (
    <div className="mx-auto w-full max-w-[1400px] space-y-6 px-4 py-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">People search</h1>
        <p className="text-sm text-[var(--muted)]">
          Find the owners, managers and decision-makers of local businesses, with verified emails.
        </p>
      </header>

      {offline && (
        <div
          role="alert"
          className="rounded-lg border border-[var(--danger)] bg-[var(--danger-soft)] px-4 py-3 text-sm text-[var(--danger)]"
        >
          Can&apos;t reach the server. On your own computer, start it with <code>start-all.bat</code>.
        </div>
      )}

      <ProspectForm onCreated={handleCreated} />

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label className="flex items-center gap-2">
          <span className="text-[var(--muted)]">Show people from</span>
          <select
            className="input w-auto max-w-[min(32rem,80vw)] py-1"
            value={jobId ?? ""}
            onChange={(e) => showJob(parseJobId(e.target.value))}
          >
            <option value="">All searches</option>
            {/* Keep a job from the URL selectable even if it's older than the recent ones. */}
            {jobId !== null && !searches.some((j) => j.id === jobId) && (
              <option value={jobId}>{search ? jobLabel(search) : `Search #${jobId}`}</option>
            )}
            {searches.map((j) => (
              <option key={j.id} value={j.id}>
                {jobLabel(j)}
              </option>
            ))}
          </select>
        </label>
        {jobId !== null && (
          <button type="button" className="btn-xs" onClick={() => showJob(null)}>
            Show everyone
          </button>
        )}
      </div>

      {jobId !== null && jobsLoaded && !search && !offline && (
        <p role="status" className="text-sm text-[var(--warn)]">
          Search #{jobId} is not among the recent searches. Showing its people anyway.
        </p>
      )}
      {search && <PipelineStatus search={search} jobs={jobs} />}

      <PeopleTable
        key={jobId ?? "all"} // a different search starts with fresh filters
        jobId={jobId}
        jobTitles={search?.params.titles ?? []}
        refreshKey={refreshKey}
        searching={running}
      />
    </div>
  );
}
