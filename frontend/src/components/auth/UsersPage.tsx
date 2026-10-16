"use client";

import { useEffect, useState } from "react";
import { api, type Role, type User } from "@/lib/api";

const errorText = (err: unknown, fallback: string) => (err instanceof Error ? err.message : fallback);

/** Admins only: who can log in. Users are deactivated, never deleted. */
export function UsersPage() {
  const [users, setUsers] = useState<User[] | null>(null);
  const [me, setMe] = useState<User | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.listUsers(), api.me()]).then(
      ([list, self]) => {
        setUsers(list);
        setMe(self);
      },
      (err) => setError(errorText(err, "Could not load the users.")),
    );
  }, []);

  async function update(user: User, body: Parameters<typeof api.updateUser>[1]) {
    try {
      const saved = await api.updateUser(user.id, body);
      setUsers((prev) => prev?.map((u) => (u.id === saved.id ? saved : u)) ?? null);
      setError(null);
      return true;
    } catch (err) {
      setError(errorText(err, "Could not save."));
      return false;
    }
  }

  return (
    <main className="mx-auto w-full max-w-[1000px] space-y-6 px-4 py-6">
      <h1 className="text-lg font-semibold">Users</h1>

      {error && (
        <p role="alert" className="rounded-lg bg-[var(--danger-soft)] px-3 py-2 text-sm text-[var(--danger)]">
          {error}
        </p>
      )}

      <AddUserForm onAdded={(u) => setUsers((prev) => [...(prev ?? []), u])} />

      {users && (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-[var(--border)] text-left text-xs text-[var(--muted)]">
              <tr>
                <th className="px-4 py-2 font-medium">Name</th>
                <th className="px-4 py-2 font-medium">Role</th>
                <th className="px-4 py-2 font-medium">Last login</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">New password</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <UserRow key={u.id} user={u} isMe={u.id === me?.id} onUpdate={(body) => update(u, body)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}

function UserRow({
  user,
  isMe,
  onUpdate,
}: {
  user: User;
  isMe: boolean;
  onUpdate: (body: Parameters<typeof api.updateUser>[1]) => Promise<boolean>;
}) {
  const [password, setPassword] = useState("");
  const [saved, setSaved] = useState(false);

  async function resetPassword(e: React.FormEvent) {
    e.preventDefault();
    if (await onUpdate({ password })) {
      setPassword("");
      setSaved(true);
    }
  }

  return (
    <tr className={`border-b border-[var(--border)] last:border-0 ${user.is_active ? "" : "text-[var(--muted)]"}`}>
      <td className="px-4 py-2">
        <div className="font-medium">
          {user.name}
          {isMe && <span className="ml-1.5 badge badge-muted">you</span>}
        </div>
        <div className="text-xs text-[var(--muted)]">{user.email}</div>
      </td>
      <td className="px-4 py-2">
        <select
          aria-label={`Role of ${user.name}`}
          className="input w-auto py-1 text-xs"
          value={user.role}
          disabled={isMe}
          onChange={(e) => void onUpdate({ role: e.target.value as Role })}
        >
          <option value="member">Member</option>
          <option value="admin">Admin</option>
        </select>
      </td>
      <td className="px-4 py-2 text-xs whitespace-nowrap">
        {user.last_login_at ? new Date(user.last_login_at).toLocaleString() : "Never"}
      </td>
      <td className="px-4 py-2">
        {isMe ? (
          <span className="badge badge-success">Active</span>
        ) : (
          <button
            type="button"
            className="btn-xs"
            title={user.is_active ? "Stop this user from logging in" : "Allow this user to log in again"}
            onClick={() => void onUpdate({ is_active: !user.is_active })}
          >
            {user.is_active ? "Deactivate" : "Reactivate"}
          </button>
        )}
      </td>
      <td className="px-4 py-2">
        <form onSubmit={resetPassword} className="flex items-center gap-1.5">
          <input
            className="input w-40 py-1 text-xs"
            type="password"
            autoComplete="new-password"
            aria-label={`New password for ${user.name}`}
            placeholder="12+ characters"
            minLength={12}
            required
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
              setSaved(false);
            }}
          />
          <button className="btn-xs">Set</button>
          {saved && <span className="text-xs text-[var(--success)]">Saved</span>}
        </form>
      </td>
    </tr>
  );
}

function AddUserForm({ onAdded }: { onAdded: (user: User) => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onAdded(await api.createUser({ name, email, password, role }));
      setName("");
      setEmail("");
      setPassword("");
      setRole("member");
    } catch (err) {
      setError(errorText(err, "Could not add the user."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-3">
      <h2 className="text-base font-semibold">Add a user</h2>
      {error && (
        <p role="alert" className="rounded-lg bg-[var(--danger-soft)] px-3 py-2 text-sm text-[var(--danger)]">
          {error}
        </p>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block space-y-1">
          <span className="label">Name</span>
          <input className="input" required value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block space-y-1">
          <span className="label">Email</span>
          <input
            className="input"
            type="email"
            autoComplete="off"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="block space-y-1">
          <span className="label">Password</span>
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <span className="block text-xs text-[var(--muted)]">
            At least 12 characters. Give it to them privately; they can change it under their name.
          </span>
        </label>
        <label className="block space-y-1">
          <span className="label">Role</span>
          <select className="input" value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="member">Member: uses the app</option>
            <option value="admin">Admin: also manages users</option>
          </select>
        </label>
      </div>
      <button className="btn-primary" disabled={busy}>
        {busy ? "Adding…" : "Add user"}
      </button>
    </form>
  );
}
