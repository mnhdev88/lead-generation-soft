"use client";

import { useEffect, useState } from "react";
import { api, type Insights } from "@/lib/api";

const pct = (x: number | null) => (x === null ? "—" : `${Math.round(x * 100)}%`);

/** Which lead signals come with replies and wins, from the statuses you set on your leads. */
export function WhatWinsSection() {
  const [data, setData] = useState<Insights | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getInsights().then(setData, (err) => setError(err instanceof Error ? err.message : "Could not load."));
  }, []);

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="text-base font-semibold">What wins</h2>
        <p className="text-sm text-[var(--muted)]">
          Learned from the statuses you set on your leads. A good result is <b>Replied</b>, <b>Interested</b> or{" "}
          <b>Won</b>; a poor one is <b>Lost</b>, <b>Not interested</b>, or <b>Contacted</b> with no reply after{" "}
          {data?.no_reply_days ?? 14} days. Keep setting statuses and this gets more accurate.
        </p>
      </div>
      {error && (
        <p role="alert" className="text-sm text-[var(--danger)]">
          {error}
        </p>
      )}
      {!data ? (
        !error && <p className="text-sm text-[var(--muted)]">Loading…</p>
      ) : data.worked < data.min_leads * 2 ? (
        <p className="text-sm text-[var(--muted)]">
          Not enough results yet: {data.worked} {data.worked === 1 ? "lead has" : "leads have"} a result
          {data.open > 0 && ` (${data.open} more contacted, waiting for a reply)`}. Mark your leads Replied, Interested,
          Won, Lost or Not interested as you work them; the report appears after {data.min_leads * 2}.
        </p>
      ) : (
        <>
          <p className="text-sm">
            <b>{pct(data.baseline)}</b> of your {data.worked.toLocaleString()} worked leads had a good result
            {data.open > 0 && ` (${data.open.toLocaleString()} more waiting for a reply)`}.
          </p>

          {data.by_score.length > 0 && (
            <div className="space-y-1">
              <h3 className="text-sm font-medium">Does the lead score predict it?</h3>
              <div className="flex flex-wrap gap-2 text-sm">
                {data.by_score.map((b) => (
                  <span key={b.band} className="rounded-lg border border-[var(--border)] px-2.5 py-1">
                    Score {b.band}: <b>{pct(b.rate)}</b>{" "}
                    <span className="text-xs text-[var(--muted)]">({b.leads.toLocaleString()} leads)</span>
                  </span>
                ))}
              </div>
              <p className="text-xs text-[var(--muted)]">
                Higher scores should win more often. If they don&apos;t, use the table below to see which points to
                change.
              </p>
            </div>
          )}

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-[var(--border)] text-left text-xs text-[var(--muted)]">
                <tr>
                  <th className="py-2 pr-3 font-medium">Signal</th>
                  <th className="py-2 pr-3 font-medium">Leads</th>
                  <th className="py-2 pr-3 font-medium">Good results</th>
                  <th className="py-2 pr-3 font-medium">Without it</th>
                  <th className="py-2 font-medium">What it means</th>
                </tr>
              </thead>
              <tbody>
                {data.signals.map((s) => {
                  const better = s.lift !== null && s.lift > 0;
                  return (
                    <tr key={s.signal} className="border-b border-[var(--border)] last:border-0">
                      <td className="py-1.5 pr-3">{s.signal}</td>
                      <td className="py-1.5 pr-3 tabular-nums">{s.leads.toLocaleString()}</td>
                      <td className="py-1.5 pr-3 tabular-nums">
                        <b>{pct(s.rate)}</b>
                        {s.lift !== null && Math.abs(s.lift) >= 0.005 && (
                          <span className={`ml-1.5 text-xs ${better ? "text-[var(--success)]" : "text-[var(--danger)]"}`}>
                            {better ? "+" : "−"}
                            {Math.round(Math.abs(s.lift) * 100)} pts
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 pr-3 tabular-nums text-[var(--muted)]">{pct(s.rate_without)}</td>
                      <td className="py-1.5 text-xs">
                        {s.advice ?? <span className="text-[var(--muted)]">No clear difference yet</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-[var(--muted)]">
            Only signals seen on at least {data.min_leads} worked leads are listed; advice needs 10 leads and a
            difference of 10 points or more. The scores are not changed automatically.
          </p>
        </>
      )}
    </section>
  );
}
