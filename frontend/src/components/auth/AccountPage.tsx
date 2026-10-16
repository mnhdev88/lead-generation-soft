"use client";

import { useEffect, useState } from "react";
import { api, type User } from "@/lib/api";

/** Your login details and a form to change your password. */
export function AccountPage() {
  const [user, setUser] = useState<User | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.me().then(setUser, () => undefined);
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (next !== repeat) {
      setMessage({ ok: false, text: "The two new passwords are different." });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setRepeat("");
      setMessage({ ok: true, text: "Password changed. You were logged out on all other devices." });
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : "Could not change the password." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto w-full max-w-md space-y-6 px-4 py-6">
      <h1 className="text-lg font-semibold">Your account</h1>

      {user && (
        <dl className="card grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-[var(--muted)]">Name</dt>
          <dd>{user.name}</dd>
          <dt className="text-[var(--muted)]">Email</dt>
          <dd className="break-all">{user.email}</dd>
          <dt className="text-[var(--muted)]">Role</dt>
          <dd>{user.role === "admin" ? "Admin" : "Member"}</dd>
        </dl>
      )}

      <form onSubmit={submit} className="card space-y-4">
        <h2 className="text-base font-semibold">Change password</h2>
        {message && (
          <p
            role={message.ok ? "status" : "alert"}
            className={`rounded-lg px-3 py-2 text-sm ${
              message.ok
                ? "bg-[var(--success-soft)] text-[var(--success)]"
                : "bg-[var(--danger-soft)] text-[var(--danger)]"
            }`}
          >
            {message.text}
          </p>
        )}
        <label className="block space-y-1">
          <span className="label">Current password</span>
          <input
            className="input"
            type="password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </label>
        <label className="block space-y-1">
          <span className="label">New password</span>
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
          <span className="block text-xs text-[var(--muted)]">
            At least 12 characters. A few random words work well, e.g. &ldquo;river candle orbit maple&rdquo;.
          </span>
        </label>
        <label className="block space-y-1">
          <span className="label">New password again</span>
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            required
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
          />
        </label>
        <button className="btn-primary" disabled={busy}>
          {busy ? "Saving…" : "Change password"}
        </button>
      </form>
    </main>
  );
}
