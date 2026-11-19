"use client";

import { useEffect, useState } from "react";
import { api, type Duplicates } from "@/lib/api";

const plural = (n: number, one: string, many: string) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
const SOURCE_LABEL: Record<string, string> = { google: "Google", osm: "OpenStreetMap" };

/** Businesses and people saved twice: see them, then merge them all with one click. */
export function DuplicatesSection() {
  const [data, setData] = useState<Duplicates | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  function fetchDuplicates() {
    return api
      .listDuplicates()
      .then(setData, (err) => setError(err instanceof Error ? err.message : "Could not look for duplicates."))
      .finally(() => setLoading(false));
  }

  function load() {
    setLoading(true);
    setError(null);
    void fetchDuplicates();
  }

  useEffect(() => {
    void fetchDuplicates(); // `loading` starts true, so nothing to set before the request
  }, []);

  async function mergeAll() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.mergeDuplicates();
      setDone(
        `Merged ${plural(r.businesses, "duplicate business", "duplicate businesses")} and ${plural(r.people, "duplicate person", "duplicate people")}.`,
      );
      setConfirming(false);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not merge the duplicates.");
    } finally {
      setBusy(false);
    }
  }

  const total = data ? data.business_copies + data.people_copies : 0;

  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold">Duplicates</h2>
          <p className="text-sm text-[var(--muted)]">
            The same business found twice (for example on Google and OpenStreetMap), or the same person saved twice at
            one business. Merging keeps the older copy with its lists, notes and status, and fills its empty facts from
            the other copy. New searches and website crawls merge duplicates on their own; this cleans up older ones.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={load} disabled={loading || busy}>
          {loading ? "Checking…" : "Check again"}
        </button>
      </div>

      {error && (
        <p role="alert" className="rounded-lg bg-[var(--danger-soft)] px-3 py-2 text-sm text-[var(--danger)]">
          {error}
        </p>
      )}
      {done && (
        <p role="status" className="rounded-lg bg-[var(--success-soft)] px-3 py-2 text-sm text-[var(--success)]">
          {done}
        </p>
      )}

      {!data ? (
        loading && <p className="text-sm text-[var(--muted)]">Looking for duplicates… (can take a while)</p>
      ) : total === 0 ? (
        <p className="text-sm">No duplicates found.</p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-sm">
              <b>{plural(data.business_copies, "business", "businesses")}</b> and{" "}
              <b>{plural(data.people_copies, "person", "people")}</b> saved twice.
            </p>
            {confirming ? (
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-sm">This can&apos;t be undone.</span>
                <button type="button" className="btn-primary" onClick={() => void mergeAll()} disabled={busy}>
                  {busy ? "Merging…" : `Yes, merge ${total.toLocaleString()}`}
                </button>
                <button type="button" className="btn-secondary" onClick={() => setConfirming(false)} disabled={busy}>
                  Cancel
                </button>
              </span>
            ) : (
              <button type="button" className="btn-primary" onClick={() => setConfirming(true)}>
                Merge all
              </button>
            )}
          </div>

          {data.businesses.length > 0 && (
            <div className="space-y-1.5">
              <h3 className="text-sm font-medium">Businesses</h3>
              <ul className="space-y-2 text-sm">
                {data.businesses.map((group) => (
                  <li key={group[0].id} className="rounded-lg border border-[var(--border)] px-3 py-2">
                    {group.map((b, i) => (
                      <div key={b.id} className={i ? "mt-1 pl-4 text-[var(--muted)]" : ""}>
                        {i > 0 && "= "}
                        <span className={i ? "" : "font-medium"}>{b.name}</span>
                        <span className="text-xs text-[var(--muted)]">
                          {" "}
                          · {SOURCE_LABEL[b.source ?? ""] ?? b.source}
                          {b.phone && ` · ${b.phone}`}
                          {b.address && ` · ${b.address}`}
                        </span>
                        {b.reason && <span className="ml-1 text-xs">({b.reason})</span>}
                      </div>
                    ))}
                  </li>
                ))}
              </ul>
              {data.businesses.length < data.business_copies && (
                <p className="text-xs text-[var(--muted)]">Showing the first {data.businesses.length} groups.</p>
              )}
            </div>
          )}

          {data.people.length > 0 && (
            <div className="space-y-1.5">
              <h3 className="text-sm font-medium">People</h3>
              <ul className="space-y-2 text-sm">
                {data.people.map((g) => (
                  <li key={g.company_id} className="rounded-lg border border-[var(--border)] px-3 py-2">
                    <div className="text-xs text-[var(--muted)]">{g.company_name}</div>
                    {g.pairs.map(([keep, copy]) => (
                      <div key={copy.id}>
                        <span className="font-medium">{keep.name ?? keep.email}</span>
                        {keep.email && keep.name && <span className="text-xs text-[var(--muted)]"> · {keep.email}</span>}
                        <span className="text-[var(--muted)]"> = {copy.name ?? copy.email}</span>
                        {copy.email && copy.name && <span className="text-xs text-[var(--muted)]"> · {copy.email}</span>}
                      </div>
                    ))}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}
