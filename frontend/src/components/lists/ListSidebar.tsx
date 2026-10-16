"use client";

import { useState } from "react";
import { api, type LeadList } from "@/lib/api";

function countsText(l: LeadList) {
  const companies = `${l.company_count} ${l.company_count === 1 ? "business" : "businesses"}`;
  const people = `${l.contact_count} ${l.contact_count === 1 ? "person" : "people"}`;
  return `${companies} · ${people}`;
}

/** Left column of the Lists page: every list, plus create / rename / delete. */
export function ListSidebar({
  lists,
  selectedId,
  onSelect,
  onCreated,
  onChanged,
  onDeleted,
}: {
  lists: LeadList[] | null; // null = loading
  selectedId: number | null;
  onSelect: (id: number) => void;
  onCreated: (list: LeadList) => void;
  onChanged: () => void; // a list was renamed / deleted: reload them
  onDeleted: (id: number) => void;
}) {
  return (
    <aside className="card space-y-4" aria-label="Your lists">
      <NewListForm onCreated={onCreated} />
      {lists === null ? (
        <p className="text-sm text-[var(--muted)]">Loading lists…</p>
      ) : lists.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">
          No lists yet. Create one above, or use “Add to list” on the Search or People page.
        </p>
      ) : (
        <ul className="-mx-1 space-y-0.5">
          {lists.map((l) => (
            <ListRow
              key={l.id}
              list={l}
              selected={l.id === selectedId}
              onSelect={() => onSelect(l.id)}
              onChanged={onChanged}
              onDeleted={() => onDeleted(l.id)}
            />
          ))}
        </ul>
      )}
    </aside>
  );
}

function NewListForm({ onCreated }: { onCreated: (list: LeadList) => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const list = await api.createList(name.trim());
      setName("");
      onCreated(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the list");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-1.5">
      <label className="label block" htmlFor="new-list-name">
        New list
      </label>
      <div className="flex gap-2">
        <input
          id="new-list-name"
          className="input py-1.5"
          placeholder="e.g. Call this week"
          maxLength={100}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <button className="btn-primary py-1.5" disabled={busy || !name.trim()}>
          {busy ? "…" : "Create"}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-[var(--danger)]">
          {error}
        </p>
      )}
    </form>
  );
}

function ListRow({
  list,
  selected,
  onSelect,
  onChanged,
  onDeleted,
}: {
  list: LeadList;
  selected: boolean;
  onSelect: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(list.name);
  const [armed, setArmed] = useState(false); // the first click on Delete only arms it
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function startRename() {
    setName(list.name);
    setError(null);
    setRenaming(true);
  }

  async function saveName(e: React.FormEvent) {
    e.preventDefault();
    const next = name.trim();
    if (!next || next === list.name) {
      setRenaming(false);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.updateList(list.id, { name: next });
      setRenaming(false);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not rename the list");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!armed) {
      setArmed(true);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.deleteList(list.id);
      onDeleted();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete the list");
      setArmed(false);
      setBusy(false);
    }
  }

  if (renaming) {
    return (
      <li className="rounded-lg bg-[var(--muted-bg)] p-2">
        <form onSubmit={saveName} className="space-y-1.5">
          <input
            className="input py-1"
            aria-label={`New name for ${list.name}`}
            maxLength={100}
            value={name}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setRenaming(false);
            }}
          />
          <div className="flex gap-1.5">
            <button className="btn-primary px-3 py-1 text-xs" disabled={busy || !name.trim()}>
              Save
            </button>
            <button type="button" className="btn-xs" onClick={() => setRenaming(false)}>
              Cancel
            </button>
          </div>
          {error && (
            <p role="alert" className="text-xs text-[var(--danger)]">
              {error}
            </p>
          )}
        </form>
      </li>
    );
  }

  return (
    <li
      className={`group rounded-lg px-1 ${selected ? "bg-[var(--accent-soft)]" : "hover:bg-[var(--muted-bg)]"}`}
    >
      <div className="flex items-start gap-1">
        {/* A real link, so it can also be opened in a new tab; a normal click just switches lists. */}
        <a
          href={`/lists?id=${list.id}`}
          aria-current={selected ? "page" : undefined}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
            e.preventDefault();
            onSelect();
          }}
          className="min-w-0 flex-1 rounded-md px-2 py-1.5"
        >
          <span className={`block truncate text-sm font-medium ${selected ? "text-[var(--accent)]" : ""}`}>
            {list.name}
          </span>
          <span className="block text-xs text-[var(--muted)]">{countsText(list)}</span>
        </a>
        <div className="flex shrink-0 items-center gap-1 py-1.5">
          <button
            type="button"
            className="btn-xs"
            onClick={startRename}
            aria-label={`Rename ${list.name}`}
            title="Rename"
          >
            ✎
          </button>
          <button
            type="button"
            className={`btn-xs ${armed ? "border-[var(--danger)] bg-[var(--danger-soft)] text-[var(--danger)] hover:border-[var(--danger)] hover:text-[var(--danger)]" : ""}`}
            disabled={busy}
            onClick={() => void remove()}
            onBlur={() => setArmed(false)}
            aria-label={armed ? `Click again to delete ${list.name}` : `Delete ${list.name}`}
            title={armed ? "Click again to delete. The leads themselves are kept." : "Delete list"}
          >
            {armed ? "Delete?" : "✕"}
          </button>
        </div>
      </div>
      {armed && (
        <p className="px-2 pb-1.5 text-xs text-[var(--danger)]" role="status">
          Click “Delete?” again to delete this list. Its leads are kept.
        </p>
      )}
      {error && (
        <p role="alert" className="px-2 pb-1.5 text-xs text-[var(--danger)]">
          {error}
        </p>
      )}
    </li>
  );
}
