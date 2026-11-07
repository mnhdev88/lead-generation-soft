"use client";

import { useEffect, useState } from "react";
import {
  api,
  DO_NOT_CONTACT_REASONS,
  type BouncedResult,
  type DoNotContactEntry,
  type DoNotContactReason,
} from "@/lib/api";

const errorText = (err: unknown, fallback: string) => (err instanceof Error ? err.message : fallback);
const plural = (n: number, one: string, many: string) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

function Alert({ text }: { text: string }) {
  return (
    <p role="alert" className="rounded-lg bg-[var(--danger-soft)] px-3 py-2 text-sm text-[var(--danger)]">
      {text}
    </p>
  );
}

function InvalidNote({ invalid }: { invalid: string[] }) {
  if (invalid.length === 0) return null;
  return (
    <p className="text-xs text-[var(--muted)]">
      Not understood (neither an email nor a domain): {invalid.slice(0, 10).join(", ")}
      {invalid.length > 10 && "…"}
    </p>
  );
}

/** Paste the emails that bounced: they become invalid everywhere and their pattern is trusted less. */
export function BouncedSection() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BouncedResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await api.reportBounced(text));
      setText("");
    } catch (err) {
      setError(errorText(err, "Could not save the bounced emails."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="text-base font-semibold">Bounced emails</h2>
        <p className="text-sm text-[var(--muted)]">
          Paste the addresses that bounced (from your email tool&apos;s bounce report). They are marked invalid
          everywhere and stay invalid. When one was guessed from the company&apos;s email pattern, the other guesses
          made with that pattern are trusted less too.
        </p>
      </div>
      <form onSubmit={submit} className="space-y-2">
        <label className="label" htmlFor="bounced-emails">
          Bounced addresses (one per line, or separated by commas)
        </label>
        <textarea
          id="bounced-emails"
          className="input min-h-24 font-mono text-xs"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={"jane.doe@acme.com\ninfo@old-business.com"}
          required
        />
        <button className="btn-primary" disabled={busy || !text.trim()}>
          {busy ? "Saving…" : "Mark as bounced"}
        </button>
      </form>
      {error && <Alert text={error} />}
      {result && (
        <div role="status" className="space-y-1 text-sm">
          <p className="text-[var(--success)]">
            {plural(result.emails, "email", "emails")} marked invalid: found at{" "}
            {plural(result.companies, "business", "businesses")} and {plural(result.people, "person", "people")}.
            {result.patterns_weakened > 0 &&
              ` The email pattern of ${plural(result.patterns_weakened, "domain is", "domains are")} now trusted less.`}
          </p>
          <InvalidNote invalid={result.invalid} />
        </div>
      )}
    </section>
  );
}

const KIND_LABEL: Record<DoNotContactEntry["kind"], string> = {
  email: "Email",
  domain: "Domain",
  company: "Business",
};

/** Emails, domains and businesses never to show, check or export. */
export function DoNotContactSection() {
  const [entries, setEntries] = useState<DoNotContactEntry[] | null>(null);
  const [text, setText] = useState("");
  const [reason, setReason] = useState<DoNotContactReason>("client");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; invalid: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api.listDoNotContact().then(
      (rows) => !cancelled && setEntries(rows),
      (err) => !cancelled && setError(errorText(err, "Could not load the list.")),
    );
    return () => {
      cancelled = true;
    };
  }, [reload]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.addDoNotContact(text, reason);
      setMessage({
        text: `Added ${r.added.toLocaleString()}${r.already ? ` (${r.already.toLocaleString()} were on the list already)` : ""}.`,
        invalid: r.invalid,
      });
      setText("");
      setReload((n) => n + 1);
    } catch (err) {
      setError(errorText(err, "Could not add them."));
    } finally {
      setBusy(false);
    }
  }

  async function remove(entry: DoNotContactEntry) {
    setError(null);
    try {
      await api.removeDoNotContact(entry.id);
      setEntries((prev) => prev?.filter((x) => x.id !== entry.id) ?? null);
    } catch (err) {
      setError(errorText(err, "Could not remove it."));
    }
  }

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="text-base font-semibold">Do not contact</h2>
        <p className="text-sm text-[var(--muted)]">
          Current clients, people who said no, competitors… Businesses and people matching this list are left out of
          searches, lists, exports and background checks (no paid lookups are spent on them). A domain covers every
          business with that website or email domain, and everyone emailing from it. To add one business, open it and
          use “Do not contact”.
        </p>
      </div>
      <form onSubmit={add} className="space-y-2">
        <label className="label" htmlFor="dnc-text">
          Emails, domains or website addresses
        </label>
        <textarea
          id="dnc-text"
          className="input min-h-24 font-mono text-xs"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={"ourclient.com\nhttps://www.competitor.com\nowner@said-no.com"}
          required
        />
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-sm">
            <span className="text-[var(--muted)]">Reason</span>
            <select
              className="input w-auto py-1"
              value={reason}
              onChange={(e) => setReason(e.target.value as DoNotContactReason)}
            >
              {Object.entries(DO_NOT_CONTACT_REASONS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <button className="btn-primary" disabled={busy || !text.trim()}>
            {busy ? "Adding…" : "Add to the list"}
          </button>
        </div>
      </form>
      {error && <Alert text={error} />}
      {message && (
        <div role="status" className="space-y-1 text-sm">
          <p className="text-[var(--success)]">{message.text}</p>
          <InvalidNote invalid={message.invalid} />
        </div>
      )}

      {entries === null ? (
        !error && <p className="text-sm text-[var(--muted)]">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">The list is empty.</p>
      ) : (
        <div className="relative overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-[var(--border)] text-left text-xs text-[var(--muted)]">
              <tr>
                <th className="py-2 pr-3 font-medium">What</th>
                <th className="py-2 pr-3 font-medium">Reason</th>
                <th className="py-2 pr-3 font-medium">Hides now</th>
                <th className="py-2 pr-3 font-medium">Added</th>
                <th className="py-2 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {entries.map((x) => (
                <tr key={x.id} className="border-b border-[var(--border)] last:border-0">
                  <td className="py-2 pr-3">
                    <span className="badge badge-muted mr-1.5">{KIND_LABEL[x.kind]}</span>
                    <span className="break-all">{x.kind === "company" ? (x.label ?? `#${x.value}`) : x.value}</span>
                  </td>
                  <td className="py-2 pr-3">
                    {x.reason ? (DO_NOT_CONTACT_REASONS[x.reason as DoNotContactReason] ?? x.reason) : "—"}
                  </td>
                  <td className="py-2 pr-3 tabular-nums">{x.matches.toLocaleString()}</td>
                  <td className="py-2 pr-3 whitespace-nowrap text-xs">{new Date(x.created_at).toLocaleDateString()}</td>
                  <td className="py-2 text-right">
                    <button type="button" className="btn-xs" onClick={() => void remove(x)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
