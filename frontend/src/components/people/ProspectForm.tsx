"use client";

import { useEffect, useState } from "react";
import { api, type Job, type Source } from "@/lib/api";

const QUICK_TITLES = ["Owner", "Founder", "CEO", "Director", "Manager", "Office Manager", "Partner"];
const MAX_TITLES = 20;

// Shown if the backend can't be reached (or is an older version without /api/sources).
const FALLBACK_SOURCES: Source[] = [
  { name: "osm", label: "OpenStreetMap", description: "Free.", available: true, unavailable_reason: null },
];

const sameTitle = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

/** "Find people": search for businesses, crawl their websites and find + verify the emails
 * of people with the chosen job titles, all in one go. */
export function ProspectForm({ onCreated }: { onCreated: (job: Job) => void }) {
  const [titles, setTitles] = useState<string[]>(["Owner", "Founder", "CEO"]);
  const [titleDraft, setTitleDraft] = useState("");
  const [query, setQuery] = useState("");
  const [location, setLocation] = useState("");
  const [limit, setLimit] = useState(50);
  const [sources, setSources] = useState<Source[]>(FALLBACK_SOURCES);
  const [picked, setPicked] = useState<string | null>(null); // null = use the default source
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listSources()
      .then((s) => {
        if (!cancelled && s.length > 0) setSources(s);
      })
      .catch(() => {}); // keep the fallback list
    return () => {
      cancelled = true;
    };
  }, []);

  // Same default as the Search page: Google Maps when it's set up, otherwise the first available source.
  const google = sources.find((s) => s.name === "google");
  const fallback = google?.available ? "google" : (sources.find((s) => s.available)?.name ?? "osm");
  const source = sources.find((s) => s.name === picked && s.available)?.name ?? fallback;

  function toggleTitle(t: string) {
    setTitles((prev) =>
      prev.some((x) => sameTitle(x, t))
        ? prev.filter((x) => !sameTitle(x, t))
        : [...prev, t].slice(0, MAX_TITLES), // the backend accepts at most 20 titles
    );
  }

  /** Add what's typed in the box. Commas add several at once: "Owner, Head chef". */
  function addDraft() {
    const typed = titleDraft
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    if (typed.length === 0) return;
    setTitles((prev) => {
      const next = [...prev];
      for (const t of typed) if (!next.some((x) => sameTitle(x, t))) next.push(t);
      return next.slice(0, MAX_TITLES);
    });
    setTitleDraft("");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const job = await api.createJob({
        query: query.trim(),
        location: location.trim(),
        limit,
        source,
        enrich: true,
        find_emails: true,
        titles,
      });
      onCreated(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the search");
    } finally {
      setBusy(false);
    }
  }

  const customTitles = titles.filter((t) => !QUICK_TITLES.some((q) => sameTitle(q, t)));

  return (
    <form onSubmit={submit} className="card space-y-4">
      <div>
        <h2 className="text-base font-semibold">Find people</h2>
        <p className="text-sm text-[var(--muted)]">
          Finds businesses, visits their websites to find the team, then guesses and verifies each person&apos;s email.
          No emails are sent.
        </p>
      </div>

      <fieldset className="space-y-2">
        <legend className="label mb-1">
          Job titles <span className="font-normal text-[var(--muted)]">(leave empty for everyone)</span>
        </legend>
        <div className="flex flex-wrap gap-1.5">
          {QUICK_TITLES.map((t) => {
            const on = titles.some((x) => sameTitle(x, t));
            return (
              <button
                key={t}
                type="button"
                aria-pressed={on}
                onClick={() => toggleTitle(t)}
                className={`chip normal-case ${on ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]" : ""}`}
              >
                {on ? "✓ " : "+ "}
                {t}
              </button>
            );
          })}
          {customTitles.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => toggleTitle(t)}
              className="chip normal-case border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
              aria-label={`Remove ${t}`}
            >
              {t} ✕
            </button>
          ))}
        </div>
        <div className="flex max-w-md gap-2">
          <input
            className="input py-1.5"
            placeholder="Other title, e.g. Practice Manager"
            aria-label="Add another job title"
            value={titleDraft}
            maxLength={80}
            onChange={(e) => setTitleDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault(); // add the title instead of starting the search
                addDraft();
              }
            }}
          />
          <button type="button" className="btn-secondary" onClick={addDraft} disabled={!titleDraft.trim()}>
            Add
          </button>
        </div>
      </fieldset>

      <div className="grid gap-3 sm:grid-cols-[1fr_1fr_140px]">
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
        <label className="block space-y-1">
          <span className="label">Location</span>
          <input
            className="input"
            placeholder="e.g. Austin, Texas"
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            required
            minLength={2}
          />
        </label>
        <label className="block space-y-1">
          <span className="label">Max companies</span>
          <input
            className="input"
            type="number"
            min={1}
            max={500}
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
          />
        </label>
      </div>

      <fieldset>
        <legend className="label mb-1.5">Data source</legend>
        <div className="grid gap-2 sm:grid-cols-2">
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
                name="prospect-source"
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
        </div>
      </fieldset>

      {error && (
        <p role="alert" className="text-sm text-[var(--danger)]">
          {error}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-primary" disabled={busy}>
          {busy ? "Starting…" : "Find people"}
        </button>
        <span className="text-xs text-[var(--muted)]">
          Takes a few minutes: search → website crawl → email check. You can leave this page; it keeps running.
        </span>
      </div>
    </form>
  );
}
