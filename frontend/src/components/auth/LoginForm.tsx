"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";

/** Only go back to a page on this site ("/people?x=1"), never to another website. */
function safeNext(next: string | null) {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\") || next.startsWith("/login")) {
    return "/";
  }
  return next;
}

export function LoginForm() {
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password);
      // A full page load, so every page starts fresh with the new login.
      window.location.assign(safeNext(searchParams.get("next")));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not log in.");
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card w-full max-w-sm space-y-4 p-6" aria-labelledby="login-title">
      <div className="flex items-center gap-2">
        <span
          aria-hidden
          className="grid h-8 w-8 place-items-center rounded-lg bg-[var(--accent)] text-sm font-bold text-[var(--accent-fg)]"
        >
          L
        </span>
        <h1 id="login-title" className="text-lg font-semibold tracking-tight">
          Log in to LeadGen
        </h1>
      </div>

      {error && (
        <p role="alert" className="rounded-lg bg-[var(--danger-soft)] px-3 py-2 text-sm text-[var(--danger)]">
          {error}
        </p>
      )}

      <label className="block space-y-1">
        <span className="label">Email</span>
        <input
          className="input"
          type="email"
          autoComplete="username"
          required
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>

      <label className="block space-y-1">
        <span className="label">Password</span>
        <input
          className="input"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>

      <button className="btn-primary w-full" disabled={busy}>
        {busy ? "Logging in…" : "Log in"}
      </button>

      <p className="text-xs text-[var(--muted)]">
        Forgot your password? Ask an admin to set a new one for you.
      </p>
    </form>
  );
}
