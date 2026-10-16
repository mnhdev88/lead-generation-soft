"use client";

// Small pieces shared by the Companies and People tabs of a list.
import { LEAD_STATUS_LABEL, LEAD_STATUSES, type LeadStatus } from "@/lib/api";

/** Blue bar shown while rows are ticked: set their status or remove them from the list. */
export function BulkBar({
  count,
  noun,
  busy,
  onSetStatus,
  onRemove,
  onClear,
}: {
  count: number;
  noun: string; // "businesses" / "people"
  busy: boolean;
  onSetStatus: (status: LeadStatus) => void;
  onRemove: () => void;
  onClear: () => void;
}) {
  if (count === 0) return null;
  return (
    <div
      className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--accent)] bg-[var(--accent-soft)] px-3 py-2"
      role="region"
      aria-label={`Actions for the selected ${noun}`}
    >
      <span className="mr-1 text-sm font-medium">{count} selected</span>
      <select
        className="input w-auto py-1 text-sm"
        aria-label={`Set lead status of the selected ${noun}`}
        value=""
        disabled={busy}
        onChange={(e) => {
          if (e.target.value) onSetStatus(e.target.value as LeadStatus);
        }}
      >
        <option value="">Set status…</option>
        {LEAD_STATUSES.map((s) => (
          <option key={s} value={s}>
            {LEAD_STATUS_LABEL[s]}
          </option>
        ))}
      </select>
      <button type="button" className="btn-secondary" disabled={busy} onClick={onRemove}>
        Remove from list
      </button>
      <button type="button" className="btn-secondary" onClick={onClear}>
        Clear
      </button>
      {busy && <span className="text-sm text-[var(--muted)]">Working…</span>}
    </div>
  );
}

export function Pager({ page, pages, onPage }: { page: number; pages: number; onPage: (p: number) => void }) {
  if (pages <= 1) return null;
  return (
    <div className="flex items-center justify-end gap-2 text-sm">
      <button className="btn-secondary" disabled={page === 0} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <span className="text-[var(--muted)]">
        Page {page + 1} of {pages}
      </span>
      <button className="btn-secondary" disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </div>
  );
}

/** Tick box in a table header that selects every row on the page. */
export function SelectAllBox({
  label,
  all,
  some,
  disabled,
  onChange,
}: {
  label: string;
  all: boolean;
  some: boolean;
  disabled: boolean;
  onChange: () => void;
}) {
  return (
    <input
      type="checkbox"
      className="checkbox"
      aria-label={label}
      checked={all}
      ref={(el) => {
        if (el) el.indeterminate = some && !all;
      }}
      disabled={disabled}
      onChange={onChange}
    />
  );
}

/** Success / error line under the toolbar. */
export function Feedback({ notice, error }: { notice: string | null; error: string | null }) {
  if (!notice && !error) return null;
  return (
    <p role={error ? "alert" : "status"} className={`text-sm ${error ? "text-[var(--danger)]" : "text-[var(--success)]"}`}>
      {error ?? notice}
    </p>
  );
}
