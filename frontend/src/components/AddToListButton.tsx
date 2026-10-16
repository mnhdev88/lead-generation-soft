"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type LeadList } from "@/lib/api";

/**
 * Open/close state for a small popover (menu) next to a button.
 * Closes on Esc (focus goes back to the button) or on a click outside `rootRef`.
 * Also closes when Next.js hides the page during navigation, so it isn't still open when you come back.
 */
export function usePopover() {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      // defaultPrevented: onRootKeyDown below already handled this Esc.
      if (e.key === "Escape" && !e.defaultPrevented) {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  useLayoutEffect(() => () => setOpen(false), []);

  /** Put on the popover's root element: Esc inside the menu closes only the menu.
   * preventDefault() tells an enclosing panel (the company drawer) not to close as well. */
  function onRootKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape" && open) {
      e.preventDefault();
      setOpen(false);
      buttonRef.current?.focus();
    }
  }

  return { open, setOpen, rootRef, buttonRef, onRootKeyDown };
}

/** Up/Down arrow keys move between the items of a role="menu" element. */
export function onMenuKeyDown(e: React.KeyboardEvent<HTMLElement>) {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
  const items = [...e.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])')];
  if (items.length === 0) return;
  e.preventDefault();
  const i = items.indexOf(document.activeElement as HTMLElement);
  const next =
    e.key === "Home"
      ? 0
      : e.key === "End"
        ? items.length - 1
        : e.key === "ArrowDown"
          ? (i + 1) % items.length
          : (i - 1 + items.length) % items.length;
  items[next].focus();
}

const FEEDBACK_MS = 4000;

/** "Add to list ▾": pick a saved list (or create one) and add the given companies/people to it. */
export function AddToListButton({
  companyIds = [],
  contactIds = [],
  onDone,
  label = "Add to list",
  size = "sm",
}: {
  companyIds?: number[];
  contactIds?: number[];
  onDone?: () => void;
  label?: string;
  size?: "xs" | "sm";
}) {
  const { open, setOpen, rootRef, buttonRef, onRootKeyDown } = usePopover();
  const menuRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [lists, setLists] = useState<LeadList[] | null>(null); // null = loading
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const count = companyIds.length + contactIds.length;

  // Stop the "Added to…" timer if the button goes away.
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  // When the menu opens with lists loaded, put the keyboard focus on the first item.
  useEffect(() => {
    if (open && lists !== null && !creating) {
      menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    }
  }, [open, lists, creating]);

  function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    setCreating(false);
    setError(null);
    setLists(null);
    api
      .listLists()
      .then(setLists)
      .catch((e) => {
        setLists([]);
        setError(e instanceof Error ? e.message : "Could not load your lists");
      });
  }

  function showFeedback(text: string) {
    setFeedback(text);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setFeedback(null), FEEDBACK_MS);
  }

  async function addTo(list: { id: number; name: string }) {
    setBusy(true);
    setError(null);
    try {
      const { changed } = await api.addToList(list.id, { company_ids: companyIds, contact_ids: contactIds });
      showFeedback(changed === 0 ? `Already in “${list.name}”` : `Added ${changed} to “${list.name}”`);
      setOpen(false);
      buttonRef.current?.focus();
      onDone?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not add to the list");
    } finally {
      setBusy(false);
    }
  }

  async function createAndAdd(e: React.FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    setError(null);
    try {
      const list = await api.createList(name);
      setNewName("");
      setCreating(false);
      setLists((prev) => [...(prev ?? []), list]);
      await addTo(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the list");
      setBusy(false);
    }
  }

  return (
    <div ref={rootRef} onKeyDown={onRootKeyDown} className="relative inline-flex items-center gap-2">
      <button
        ref={buttonRef}
        type="button"
        className={size === "xs" ? "btn-xs" : "btn-secondary"}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={count === 0}
        onClick={toggle}
      >
        {label} ▾
      </button>
      {feedback && (
        <span role="status" className="text-xs text-[var(--success)]">
          {feedback}
        </span>
      )}

      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label="Add to which list?"
          onKeyDown={onMenuKeyDown}
          className="absolute left-0 top-full z-40 mt-1 w-64 rounded-lg border border-[var(--border)] bg-[var(--card)] p-1 text-sm shadow-lg"
        >
          {lists === null ? (
            <p className="px-2 py-1.5 text-[var(--muted)]">Loading lists…</p>
          ) : (
            <>
              {lists.length === 0 && !error && (
                <p className="px-2 py-1.5 text-xs text-[var(--muted)]">No lists yet. Create your first one:</p>
              )}
              <div className="max-h-60 overflow-y-auto">
                {lists.map((l) => (
                  <button
                    key={l.id}
                    type="button"
                    role="menuitem"
                    disabled={busy}
                    onClick={() => void addTo(l)}
                    className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left hover:bg-[var(--muted-bg)] focus:bg-[var(--muted-bg)] focus:outline-none disabled:opacity-50"
                  >
                    <span className="truncate">{l.name}</span>
                    <span className="shrink-0 text-xs tabular-nums text-[var(--muted)]">
                      {l.company_count + l.contact_count}
                    </span>
                  </button>
                ))}
              </div>
              <div className="mt-1 border-t border-[var(--border)] pt-1">
                {creating ? (
                  <form onSubmit={createAndAdd} className="space-y-1.5 p-1">
                    <input
                      className="input py-1"
                      placeholder="List name, e.g. Call this week"
                      aria-label="New list name"
                      maxLength={100}
                      value={newName}
                      autoFocus
                      onChange={(e) => setNewName(e.target.value)}
                    />
                    <div className="flex gap-1.5">
                      <button className="btn-primary px-3 py-1 text-xs" disabled={busy || !newName.trim()}>
                        {busy ? "Saving…" : "Create & add"}
                      </button>
                      <button type="button" className="btn-xs" onClick={() => setCreating(false)}>
                        Cancel
                      </button>
                    </div>
                  </form>
                ) : (
                  <button
                    type="button"
                    role="menuitem"
                    disabled={busy}
                    onClick={() => setCreating(true)}
                    className="w-full rounded-md px-2 py-1.5 text-left text-[var(--accent)] hover:bg-[var(--muted-bg)] focus:bg-[var(--muted-bg)] focus:outline-none"
                  >
                    + New list…
                  </button>
                )}
              </div>
            </>
          )}
          {error && (
            <p role="alert" className="px-2 py-1.5 text-xs text-[var(--danger)]">
              {error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
