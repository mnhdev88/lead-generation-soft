"use client";

import { useEffect, useState } from "react";
import { api, type Job, type Source, type Usage } from "@/lib/api";

const QUICK_CATEGORIES = ["dentist", "restaurant", "real estate", "gym", "salon", "lawyer", "hotel", "car repair"];

// Shown if the backend can't be reached (or is an older version without /api/sources).
const FALLBACK_SOURCES: Source[] = [
  { name: "osm", label: "OpenStreetMap", description: "Free.", available: true, unavailable_reason: null },
];

// Google returns at most 60 places per search. Asking for more triggers a "deep search"
// that splits the area into tiles, which costs more API requests.
const GOOGLE_MAX_PER_SEARCH = 60;
const GOOGLE_PAGE_SIZE = 20;
const GOOGLE_MAX_TILES = 16;
const GOOGLE_PAGES_PER_TILE = 3;

/** Most Google requests a search can use. Mirrors backend/app/sources/google_places.py:
 * up to 60 results in one area, otherwise a grid of n x n tiles with 3 pages each. */
function googleRequestEstimate(limit: number) {
  if (limit <= GOOGLE_MAX_PER_SEARCH) return Math.ceil(limit / GOOGLE_PAGE_SIZE);
  const tiles = Math.min(Math.ceil(limit / GOOGLE_MAX_PER_SEARCH), GOOGLE_MAX_TILES);
  const n = Math.ceil(Math.sqrt(tiles));
  return n * n * GOOGLE_PAGES_PER_TILE;
}

export function SearchForm({
  onCreated,
  refreshKey,
  online,
}: {
  onCreated: (job: Job) => void;
  refreshKey: number; // changes when a job finishes, so sources and usage get re-read
  online: boolean;
}) {
  const [query, setQuery] = useState("");
  const [location, setLocation] = useState("");
  const [limit, setLimit] = useState(200);
  const [enrich, setEnrich] = useState(true);
  const [findEmails, setFindEmails] = useState(false);
  const [picked, setPicked] = useState<string | null>(null); // null = use the default source
  const [sources, setSources] = useState<Source[]>(FALLBACK_SOURCES);
  const [usage, setUsage] = useState<Usage[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the list of data sources and this month's API usage.
  useEffect(() => {
    if (!online) return;
    let cancelled = false;
    api
      .listSources()
      .then((s) => {
        if (!cancelled && s.length > 0) setSources(s);
      })
      .catch(() => {}); // keep the fallback list
    api
      .listUsage()
      .then((u) => {
        if (!cancelled) setUsage(u);
      })
      .catch(() => {}); // the meter is optional
    return () => {
      cancelled = true;
    };
  }, [refreshKey, online]);

  const google = sources.find((s) => s.name === "google");
  // Default to Google Maps when it's set up (better coverage), otherwise the first available source.
  const fallback = google?.available ? "google" : (sources.find((s) => s.available)?.name ?? "osm");
  const pickedSource = sources.find((s) => s.name === picked && s.available);
  const source = pickedSource ? pickedSource.name : fallback;
  const isGoogle = source === "google";
  const googleUsage = usage.find((u) => u.provider === "google_places");
  // Hunter.io is an optional backup for finding emails. Its meter shows when a key is set, it
  // has a monthly cap and is relevant: already used this month, or "Find & verify emails" is ticked.
  const hunterUsage = usage.find((u) => u.provider === "hunter" && u.limit > 0 && u.configured !== false);
  const showHunter = hunterUsage !== undefined && (hunterUsage.count > 0 || findEmails);
  // Finding emails needs the website crawl first, so it switches the crawl on.
  const crawl = enrich || findEmails;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.createJob({ query, location, limit, source, enrich: crawl, find_emails: findEmails }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start search");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-4">
      <h2 className="text-base font-semibold">New local search</h2>

      <label className="block space-y-1">
        <span className="label">Business type</span>
        <input
          className="input"
          placeholder="e.g. dentist"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          required
          minLength={2}
        />
      </label>
      <div className="flex flex-wrap gap-1.5">
        {QUICK_CATEGORIES.map((c) => (
          <button key={c} type="button" className="chip" onClick={() => setQuery(c)}>
            {c}
          </button>
        ))}
      </div>

      <label className="block space-y-1">
        <span className="label">Location</span>
        <input
          className="input"
          placeholder="e.g. Karachi, Pakistan or Austin, Texas"
          value={location}
          onChange={(e) => setLocation(e.target.value)}
          required
          minLength={2}
        />
      </label>

      <fieldset className="space-y-1.5">
        <legend className="label mb-1">Data source</legend>
        {sources.map((s) => (
          <label
            key={s.name}
            className={`flex gap-2.5 rounded-lg border px-3 py-2 transition-colors ${
              !s.available
                ? "cursor-not-allowed border-dashed border-[var(--border)] opacity-70"
                : source === s.name
                  ? "cursor-pointer border-[var(--accent)] bg-[var(--accent-soft)]"
                  : "cursor-pointer border-[var(--border)] hover:bg-[var(--muted-bg)]"
            }`}
          >
            <input
              type="radio"
              name="source"
              className="mt-1 accent-[var(--accent)]"
              value={s.name}
              checked={source === s.name}
              disabled={!s.available}
              onChange={() => setPicked(s.name)}
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{s.label}</span>
              <span className="block text-xs text-[var(--muted)]">{s.description}</span>
              {!s.available && s.unavailable_reason && (
                <span className="mt-1 block text-xs text-[var(--warn)]">{s.unavailable_reason}</span>
              )}
            </span>
          </label>
        ))}
      </fieldset>

      {googleUsage && (isGoogle || google?.available) && (
        <UsageMeter
          usage={googleUsage}
          label="Google requests this month"
          fullMessage="Monthly limit reached. Use OpenStreetMap until next month."
        />
      )}

      <label className="block space-y-1">
        <span className="label">Max results</span>
        <input
          className="input"
          type="number"
          min={1}
          max={2000}
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
        />
      </label>
      {isGoogle && (
        <p className="-mt-2 text-xs text-[var(--muted)]">
          {limit <= GOOGLE_MAX_PER_SEARCH ? (
            <>
              Google Maps gives up to {GOOGLE_MAX_PER_SEARCH} results per area. This search uses about{" "}
              {googleRequestEstimate(limit)} API request(s).
            </>
          ) : (
            <>
              Google Maps gives up to {GOOGLE_MAX_PER_SEARCH} results per area. Above {GOOGLE_MAX_PER_SEARCH} we run a{" "}
              <strong>deep search</strong> over a grid of smaller areas, which uses more API requests (up to ~{googleRequestEstimate(limit)}).
            </>
          )}
        </p>
      )}

      <label className="flex cursor-pointer items-start gap-2.5 text-sm">
        <input
          type="checkbox"
          className="mt-0.5 accent-[var(--accent)]"
          checked={crawl}
          disabled={findEmails}
          onChange={(e) => setEnrich(e.target.checked)}
        />
        <span>
          Also find emails &amp; team from websites
          <span className="block text-xs text-[var(--muted)]">
            {findEmails
              ? "Needed for finding emails, so it stays on."
              : "Visits each business's website after the search. Free, takes a few minutes."}
          </span>
        </span>
      </label>

      <label className="flex cursor-pointer items-start gap-2.5 text-sm">
        <input
          type="checkbox"
          className="mt-0.5 accent-[var(--accent)]"
          checked={findEmails}
          onChange={(e) => setFindEmails(e.target.checked)}
        />
        <span>
          Find &amp; verify emails (guess staff emails, check they exist)
          <span className="block text-xs text-[var(--muted)]">
            After the crawl, guesses emails like jane@company.com for the people found and asks each mail server
            whether the address is real. No emails are sent.
          </span>
        </span>
      </label>

      {showHunter && (
        <UsageMeter
          usage={hunterUsage}
          label="Hunter.io searches this month"
          fullMessage="Hunter.io limit reached. Emails are still guessed and checked without it."
        />
      )}

      {error && <p className="text-sm text-[var(--danger)]">{error}</p>}

      <button className="btn-primary w-full" disabled={busy}>
        {busy ? "Starting…" : "Find leads"}
      </button>
    </form>
  );
}

/** "Google requests this month: 120 / 5000" with a small bar. */
function UsageMeter({ usage, label, fullMessage }: { usage: Usage; label: string; fullMessage: string }) {
  const pct = usage.limit > 0 ? Math.min(100, (usage.count / usage.limit) * 100) : 0;
  const color = pct >= 100 ? "var(--danger)" : pct >= 80 ? "var(--warn)" : "var(--accent)";
  return (
    <div className="space-y-1 rounded-lg bg-[var(--muted-bg)] px-3 py-2">
      <div className="flex justify-between text-xs">
        <span className="text-[var(--muted)]">{label}</span>
        <span className="font-medium tabular-nums">
          {usage.count.toLocaleString()} / {usage.limit.toLocaleString()}
        </span>
      </div>
      <div
        className="progress bg-[var(--card)]"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={usage.limit}
        aria-valuenow={usage.count}
      >
        <div style={{ width: `${pct}%`, background: color }} />
      </div>
      {pct >= 100 && <p className="text-xs text-[var(--danger)]">{fullMessage}</p>}
    </div>
  );
}
