"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { CompanyDrawer } from "@/components/CompanyDrawer";
import { JobList } from "@/components/JobList";
import { ResultsTable } from "@/components/ResultsTable";
import { SearchForm } from "@/components/SearchForm";
import { api, type Job } from "@/lib/api";

const isActive = (j: Job) => j.status === "queued" || j.status === "running";
const isFinished = (j: Job) => j.status === "done" || j.status === "failed";

// The open company lives in the URL (?company=12), so the People page can link straight
// to it and the browser Back button closes the panel. We use the browser's own history
// API, which Next.js keeps in sync with useSearchParams (no page reload, no server call).
function openCompany(id: number) {
  // The marker tells closeCompany that this history entry was added by us.
  window.history.pushState({ leadgenDrawer: true }, "", `?company=${id}`);
}
function closeCompany() {
  if (window.history.state?.leadgenDrawer) {
    // Step back to the entry we came from, so Back doesn't reopen the panel.
    window.history.back();
  } else {
    // Arrived with ?company=… in the link (e.g. from the People page): just drop it.
    window.history.replaceState(null, "", window.location.pathname);
  }
}

export default function Home() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [refreshKey, setRefreshKey] = useState(0); // table: a job finished or a crawl/email check progressed
  const [jobsVersion, setJobsVersion] = useState(0); // drawer: a job finished
  const [offline, setOffline] = useState(false);

  const finishedIds = useRef<Set<number> | null>(null);
  const progressRef = useRef("");

  const loadJobs = useCallback(async () => {
    try {
      const next = await api.listJobs();
      // A job (search, website crawl or email check) has just finished.
      const finished = new Set(next.filter(isFinished).map((j) => j.id));
      const justFinished =
        finishedIds.current !== null && [...finished].some((id) => !finishedIds.current!.has(id));
      finishedIds.current = finished;

      // A website crawl or email check made progress, so emails appear row by row.
      const progress = next
        .filter((j) => j.kind !== "search" && j.status === "running")
        .map((j) => `${j.id}:${j.processed}`)
        .join(",");
      const progressed = progress !== "" && progress !== progressRef.current;
      progressRef.current = progress;

      if (justFinished) setJobsVersion((v) => v + 1);
      if (justFinished || progressed) setRefreshKey((k) => k + 1);
      setJobs(next);
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, []);

  // Called after any new job is created (search, "Find emails", "Crawl", drawer buttons):
  // show it at once and reload the list so polling speeds up.
  const handleJobCreated = useCallback(
    (job: Job) => {
      setJobs((prev) => [job, ...prev.filter((j) => j.id !== job.id)]);
      void loadJobs();
    },
    [loadJobs],
  );

  const reloadTable = useCallback(() => setRefreshKey((k) => k + 1), []);

  const active = jobs.some(isActive);

  // Poll fast while a job is queued or running, slowly otherwise.
  useEffect(() => {
    const first = setTimeout(loadJobs, 0);
    const timer = setInterval(loadJobs, active ? 2000 : 10000);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [loadJobs, active]);

  return (
    <div className="mx-auto w-full max-w-[1400px] px-4 py-6">
      <h1 className="sr-only">Search for leads</h1>

      {offline && (
        <div className="mb-4 rounded-lg border border-[var(--danger)] bg-[var(--danger-soft)] px-4 py-3 text-sm text-[var(--danger)]">
          Can&apos;t reach the server. On your own computer, start it with <code>start-all.bat</code>.
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
        <aside className="min-w-0 space-y-6">
          <SearchForm
            refreshKey={jobs.filter(isFinished).length}
            online={!offline}
            onCreated={(job) => {
              handleJobCreated(job);
              setSelectedId(job.id);
            }}
          />
          <JobList jobs={jobs} selectedId={selectedId} onSelect={setSelectedId} onJobCreated={handleJobCreated} />
        </aside>
        <main className="min-w-0">
          <ResultsTable
            jobId={selectedId}
            refreshKey={refreshKey}
            jobs={jobs}
            onJobCreated={handleJobCreated}
            onOpenCompany={openCompany}
          />
        </main>
      </div>

      {/* Reading the URL needs a Suspense boundary so the rest of the page can be prerendered. */}
      <Suspense fallback={null}>
        <DrawerFromUrl
          jobs={jobs}
          jobsVersion={jobsVersion}
          onJobCreated={handleJobCreated}
          onChanged={reloadTable}
        />
      </Suspense>
    </div>
  );
}

/** Shows the company panel when the URL has ?company=<id>. */
function DrawerFromUrl({
  jobs,
  jobsVersion,
  onJobCreated,
  onChanged,
}: {
  jobs: Job[];
  jobsVersion: number;
  onJobCreated: (job: Job) => void;
  onChanged: () => void;
}) {
  const searchParams = useSearchParams();
  const id = Number(searchParams.get("company"));
  if (!Number.isInteger(id) || id <= 0) return null;
  return (
    <CompanyDrawer
      key={id} // a different company starts with a fresh panel
      companyId={id}
      jobs={jobs}
      jobsVersion={jobsVersion}
      onClose={closeCompany}
      onJobCreated={onJobCreated}
      onChanged={onChanged}
    />
  );
}
