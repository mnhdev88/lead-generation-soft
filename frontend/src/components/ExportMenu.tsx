"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { onMenuKeyDown, usePopover } from "@/components/AddToListButton";
import { api, type CompanyFilters, type PeopleFilters, type SheetsStatus } from "@/lib/api";

type Kind = "companies" | "people";

const SHEET_URL_KEY = "leadgen.sheetUrl"; // the last Google Sheet you exported to

// localStorage can throw (private windows, blocked storage), so every access is wrapped.
function loadSavedSheet() {
  try {
    return window.localStorage.getItem(SHEET_URL_KEY) ?? "";
  } catch {
    return "";
  }
}
function saveSheet(url: string) {
  try {
    window.localStorage.setItem(SHEET_URL_KEY, url);
  } catch {
    // not important: the user just types it again next time
  }
}

/** "Export ▾": download as CSV / Excel, or send the rows to a Google Sheet. */
export function ExportMenu({ kind, filters }: { kind: Kind; filters: CompanyFilters | PeopleFilters }) {
  const { open, setOpen, rootRef, buttonRef, onRootKeyDown } = usePopover();
  const menuRef = useRef<HTMLDivElement>(null);
  const [sheetsOpen, setSheetsOpen] = useState(false);

  // Close the Sheets dialog if Next.js hides this page during navigation
  // (an open modal dialog would block clicks on the next page).
  useLayoutEffect(() => () => setSheetsOpen(false), []);

  useEffect(() => {
    if (open) menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
  }, [open]);

  const fileUrl = (format: "csv" | "xlsx") =>
    kind === "people"
      ? api.peopleExportUrl(filters as PeopleFilters, format)
      : api.exportUrl(filters as CompanyFilters, format);

  const itemCls =
    "block w-full rounded-md px-2 py-1.5 text-left hover:bg-[var(--muted-bg)] focus:bg-[var(--muted-bg)] focus:outline-none";

  return (
    <div ref={rootRef} onKeyDown={onRootKeyDown} className="relative inline-block">
      <button
        ref={buttonRef}
        type="button"
        className="btn-secondary"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        Export ▾
      </button>
      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label="Export format"
          onKeyDown={onMenuKeyDown}
          className="absolute left-0 top-full z-40 mt-1 w-56 rounded-lg border border-[var(--border)] bg-[var(--card)] p-1 text-sm shadow-lg"
        >
          {/* The backend sends these as file downloads. */}
          <a role="menuitem" href={fileUrl("csv")} className={itemCls} onClick={() => setOpen(false)}>
            CSV
            <span className="block text-xs text-[var(--muted)]">Opens in Excel, Google Sheets, any CRM</span>
          </a>
          <a role="menuitem" href={fileUrl("xlsx")} className={itemCls} onClick={() => setOpen(false)}>
            Excel (.xlsx)
          </a>
          <button
            type="button"
            role="menuitem"
            className={itemCls}
            aria-haspopup="dialog"
            onClick={() => {
              setOpen(false);
              setSheetsOpen(true);
            }}
          >
            Google Sheets…
          </button>
        </div>
      )}
      {sheetsOpen && (
        <SheetsDialog
          kind={kind}
          filters={filters}
          onClose={() => {
            setSheetsOpen(false);
            buttonRef.current?.focus();
          }}
        />
      )}
    </div>
  );
}

// ----- Google Sheets dialog -------------------------------------------------------------

function SheetsDialog({
  kind,
  filters,
  onClose,
}: {
  kind: Kind;
  filters: CompanyFilters | PeopleFilters;
  onClose: () => void;
}) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [status, setStatus] = useState<SheetsStatus | null>(null); // null = loading
  const [loadError, setLoadError] = useState<string | null>(null);
  // The dialog is only shown after a click, so reading localStorage here is safe.
  const [sheet, setSheet] = useState(loadSavedSheet);
  const [tab, setTab] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ url: string; tab_name: string; rows: number } | null>(null);

  // A native <dialog> opened with showModal() handles Esc, keeps focus inside and dims the page.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .sheetsStatus()
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : "Could not reach the backend");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function exportNow(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.exportToSheets({
        kind,
        filters,
        spreadsheet: sheet.trim(),
        tab_name: tab.trim() || undefined,
      });
      saveSheet(sheet.trim());
      setResult(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      onClose={onClose}
      // Clicking the dimmed area around the box closes it.
      onClick={(e) => {
        if (e.target === e.currentTarget) e.currentTarget.close();
      }}
      className="m-auto w-[min(34rem,calc(100vw-2rem))] rounded-xl border border-[var(--border)] bg-[var(--card)] p-0 text-[var(--foreground)] shadow-xl backdrop:bg-black/50"
    >
      <div className="space-y-4 p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 id={titleId} className="text-base font-semibold">
              Export to Google Sheets
            </h2>
            <p className="text-xs text-[var(--muted)]">
              Adds a new tab with the {kind === "people" ? "people" : "businesses"} that match your current filters.
            </p>
          </div>
          <button
            type="button"
            className="btn-xs"
            aria-label="Close"
            onClick={() => dialogRef.current?.close()}
          >
            ✕
          </button>
        </div>

        {loadError && (
          <p role="alert" className="text-sm text-[var(--danger)]">
            {loadError}
          </p>
        )}
        {!status && !loadError && <p className="text-sm text-[var(--muted)]">Checking the Google Sheets setup…</p>}

        {status && !status.configured && <SetupSteps reason={status.reason} />}

        {status?.configured && (
          <form onSubmit={exportNow} className="space-y-4">
            <ServiceAccount email={status.service_account_email ?? ""} />

            <label className="block space-y-1">
              <span className="label">Google Sheet link</span>
              <input
                className="input"
                type="text"
                inputMode="url"
                placeholder="https://docs.google.com/spreadsheets/d/…"
                value={sheet}
                onChange={(e) => setSheet(e.target.value)}
                required
                minLength={10}
              />
            </label>
            <label className="block space-y-1">
              <span className="label">
                Tab name <span className="font-normal text-[var(--muted)]">(optional)</span>
              </span>
              <input
                className="input"
                placeholder="Leads <date and time>"
                maxLength={100}
                value={tab}
                onChange={(e) => setTab(e.target.value)}
              />
            </label>

            {error && (
              <p role="alert" className="text-sm text-[var(--danger)]">
                {error}
              </p>
            )}
            {result && (
              <p role="status" className="rounded-lg bg-[var(--success-soft)] px-3 py-2 text-sm text-[var(--success)]">
                Exported to the tab “{result.tab_name}”.{" "}
                <a href={result.url} target="_blank" rel="noreferrer" className="font-medium underline">
                  Open sheet ({result.rows.toLocaleString()} rows) ↗
                </a>
              </p>
            )}

            <div className="flex justify-end gap-2">
              <button type="button" className="btn-secondary" onClick={() => dialogRef.current?.close()}>
                {result ? "Done" : "Cancel"}
              </button>
              <button className="btn-primary" disabled={busy || sheet.trim().length < 10}>
                {busy ? "Exporting…" : "Export"}
              </button>
            </div>
          </form>
        )}
      </div>
    </dialog>
  );
}

/** The service-account email the user must share their sheet with, plus a Copy button. */
function ServiceAccount({ email }: { email: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(email);
      setCopied(true);
    } catch {
      setCopied(false); // clipboard blocked: the text is selectable anyway
    }
  }

  return (
    <div className="space-y-1.5 rounded-lg bg-[var(--muted-bg)] px-3 py-2.5">
      <p className="text-sm">
        Share your sheet with this email as <strong>Editor</strong> (Share button in Google Sheets):
      </p>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 select-all break-all rounded bg-[var(--card)] px-2 py-1 font-mono text-xs">
          {email}
        </code>
        <button type="button" className="btn-xs" onClick={() => void copy()}>
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>
    </div>
  );
}

/** Shown when the backend has no Google credentials yet. */
function SetupSteps({ reason }: { reason: string | null }) {
  return (
    <div className="space-y-3 text-sm">
      <p className="rounded-lg bg-[var(--warn-soft)] px-3 py-2 text-[var(--warn)]">
        {reason ?? "Google Sheets export is not set up yet."}
      </p>
      <div>
        <p className="font-medium">One-time setup (about 5 minutes, free):</p>
        <ol className="mt-1.5 list-decimal space-y-1 pl-5 text-[var(--muted)]">
          <li>
            In{" "}
            <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer" className="link">
              Google Cloud Console
            </a>
            , create a project and enable the <strong>Google Sheets API</strong>.
          </li>
          <li>
            Go to <em>IAM &amp; Admin → Service accounts</em>, create a service account, then{" "}
            <em>Keys → Add key → JSON</em>. A key file downloads.
          </li>
          <li>
            Put the file in the <code>backend</code> folder and add this line to <code>backend/.env</code>:
            <code className="mt-1 block rounded bg-[var(--muted-bg)] px-2 py-1 font-mono text-xs text-[var(--foreground)]">
              GOOGLE_SHEETS_CREDENTIALS_FILE=your-key-file.json
            </code>
          </li>
          <li>Restart the backend (close it and run start-all.bat again), then reopen this window.</li>
        </ol>
        <p className="mt-2 text-xs text-[var(--muted)]">
          Keep the key file private. It is a password for that service account. See “Google Sheets export” in the
          README.
        </p>
      </div>
    </div>
  );
}
