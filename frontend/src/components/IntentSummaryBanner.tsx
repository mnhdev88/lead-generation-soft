"use client";

import { useEffect, useState } from "react";
import { api, HOT_INTENT, lookingForParam, OFFER_LABEL, type IntentSummary, type Offer } from "@/lib/api";
import { offersLabel } from "@/lib/offers";

/** Above the results of a search made with "What are you selling?" picks: how many hot
 * leads it found for those services, the most common reasons, and what is still being checked. */
export function IntentSummaryBanner({
  jobId,
  batchId,
  offers,
  refreshKey,
  running,
  hotOnly,
  onShowHot,
}: {
  jobId: number | null;
  batchId: string | null;
  offers: Offer[];
  refreshKey: number; // changes when a job finishes or makes progress
  running: boolean; // the search or a website crawl is still going
  hotOnly: boolean;
  onShowHot: () => void;
}) {
  const [summary, setSummary] = useState<IntentSummary | null>(null);
  const lookingFor = lookingForParam(offers);

  useEffect(() => {
    let cancelled = false;
    api
      .intentSummary({ job_id: jobId ?? undefined, batch_id: batchId ?? undefined, looking_for: lookingFor })
      .then((s) => {
        if (!cancelled) setSummary(s);
      })
      .catch(() => {
        if (!cancelled) setSummary(null); // the summary is optional: the table still works
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, batchId, lookingFor, refreshKey]);

  if (!summary) return null;
  const several = summary.offers.length > 1;
  const speedMatters = offers.includes("website") || offers.includes("seo");

  return (
    <section
      className="space-y-2 rounded-lg border border-[var(--accent)] bg-[var(--accent-soft)] px-3 py-2.5 text-sm"
      aria-label={`What this search found for ${offersLabel(offers, OFFER_LABEL)}`}
      aria-live="polite"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <p>
          Selling <strong>{offersLabel(offers, OFFER_LABEL)}</strong>:{" "}
          {summary.total === 0 ? (
            running ? "searching…" : "no businesses found."
          ) : (
            <>
              <strong>
                {summary.any_hot} hot lead{summary.any_hot === 1 ? "" : "s"}
              </strong>{" "}
              of {summary.total}
              {running && <span className="text-[var(--muted)]"> (still checking)</span>}
            </>
          )}
        </p>
        {summary.any_hot > 0 && !hotOnly && (
          <button type="button" className="btn-secondary py-1 text-xs" onClick={onShowHot}>
            Show only hot leads
          </button>
        )}
      </div>

      {summary.total > 0 && (
        <ul className="space-y-1">
          {summary.offers.map((o) => (
            <li key={o.offer} className="text-xs">
              {several && <strong>{OFFER_LABEL[o.offer]}: </strong>}
              <span className="tabular-nums">
                {o.hot} hot · {o.warm} maybe
              </span>
              {o.top_reasons.length > 0 && (
                <span className="text-[var(--muted)]">
                  {" "}
                  · most common: {o.top_reasons.map((r) => `${r.reason} (${r.count})`).join(", ")}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      {(summary.website_not_read > 0 || (speedMatters && summary.speed_pending > 0)) && (
        <p className="text-xs text-[var(--muted)]">
          {summary.website_not_read > 0 && (
            <>
              {summary.website_not_read} website{summary.website_not_read === 1 ? "" : "s"} not read yet
              {running ? " (being crawled now)" : ""}: their scores may go up.{" "}
            </>
          )}
          {speedMatters && summary.speed_pending > 0 && (
            <>
              {summary.speed_pending} website{summary.speed_pending === 1 ? "" : "s"} still waiting for a Google
              speed test. Those run one at a time in the background, so scores keep updating.
            </>
          )}
        </p>
      )}
      <p className="sr-only">A hot lead has an intent score of {HOT_INTENT} or more.</p>
    </section>
  );
}
