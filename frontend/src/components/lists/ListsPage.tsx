"use client";

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ListCompaniesTab } from "@/components/lists/ListCompaniesTab";
import { ListPeopleTab } from "@/components/lists/ListPeopleTab";
import { ListSidebar } from "@/components/lists/ListSidebar";
import { api, type LeadList } from "@/lib/api";

/** ?id=3 → 3 (anything else → null). */
function parseId(raw: string | null): number | null {
  const n = Number(raw);
  return raw && Number.isInteger(n) && n > 0 ? n : null;
}

// The open list lives in the URL (?id=3) so it can be bookmarked and Back works.
// Next.js keeps useSearchParams in sync with pushState / replaceState.
function showList(id: number | null, replace = false) {
  const url = id === null ? "/lists" : `/lists?id=${id}`;
  if (replace) window.history.replaceState(null, "", url);
  else window.history.pushState(null, "", url);
}

export function ListsPage() {
  const searchParams = useSearchParams();
  const idParam = parseId(searchParams.get("id"));

  const [lists, setLists] = useState<LeadList[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const reload = () => setReloadKey((k) => k + 1);

  useEffect(() => {
    let cancelled = false;
    api
      .listLists()
      .then((l) => {
        if (cancelled) return;
        setLists(l);
        setError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setLists((prev) => prev ?? []);
        setError(e instanceof Error ? e.message : "Could not load your lists");
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  // No ?id= yet: open the first list.
  const selected = lists?.find((l) => l.id === idParam) ?? (idParam === null ? (lists?.[0] ?? null) : null);

  return (
    <div className="mx-auto w-full max-w-[1400px] space-y-6 px-4 py-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Lead lists</h1>
        <p className="text-sm text-[var(--muted)]">
          Group businesses and people into lists, track their status, and export each list.
        </p>
      </header>

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-[var(--danger)] bg-[var(--danger-soft)] px-4 py-3 text-sm text-[var(--danger)]"
        >
          {error}. Is the backend running? Start it with <code>start-backend.bat</code>.
        </div>
      )}

      <div className="grid items-start gap-6 lg:grid-cols-[300px_minmax(0,1fr)]">
        <ListSidebar
          lists={lists}
          selectedId={selected?.id ?? null}
          onSelect={(id) => showList(id)}
          onCreated={(l) => {
            // Show the new list straight away, then re-read all lists (for the sort order).
            setLists((prev) => [...(prev ?? []), l]);
            showList(l.id);
            reload();
          }}
          onChanged={reload}
          onDeleted={(id) => {
            if (id === selected?.id) showList(null, true);
          }}
        />
        <main className="min-w-0">
          {selected ? (
            <ListDetail key={selected.id} list={selected} onChanged={reload} />
          ) : (
            <div className="card py-12 text-center text-sm text-[var(--muted)]">
              {lists === null
                ? "Loading…"
                : idParam !== null
                  ? "That list doesn't exist any more. Pick another one on the left."
                  : "Create a list on the left to start organising your leads."}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

type Tab = "companies" | "people";

/** Right side: one list with a Businesses tab and a People tab. */
function ListDetail({ list, onChanged }: { list: LeadList; onChanged: () => void }) {
  // Open the tab that has something in it.
  const [tab, setTab] = useState<Tab>(list.company_count > 0 || list.contact_count === 0 ? "companies" : "people");
  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ companies: null, people: null });

  const tabs: { id: Tab; label: string; count: number }[] = [
    { id: "companies", label: "Businesses", count: list.company_count },
    { id: "people", label: "People", count: list.contact_count },
  ];

  // Left/Right arrows move between tabs (standard keyboard pattern for tabs).
  function onTabKey(e: React.KeyboardEvent) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const next: Tab = tab === "companies" ? "people" : "companies";
    setTab(next);
    tabRefs.current[next]?.focus();
  }

  return (
    <section className="card space-y-4" aria-labelledby="list-title">
      <div>
        <h2 id="list-title" className="text-lg font-semibold">
          {list.name}
        </h2>
        {list.description && <p className="text-sm text-[var(--muted)]">{list.description}</p>}
      </div>

      <div role="tablist" aria-label="What to show" className="flex gap-1 border-b border-[var(--border)]">
        {tabs.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              tabRefs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => setTab(t.id)}
            onKeyDown={onTabKey}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
              tab === t.id
                ? "border-[var(--accent)] text-[var(--accent)]"
                : "border-transparent text-[var(--muted)] hover:text-[var(--foreground)]"
            }`}
          >
            {t.label} <span className="ml-0.5 text-xs tabular-nums opacity-80">{t.count}</span>
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "companies" ? (
          <ListCompaniesTab listId={list.id} onChanged={onChanged} />
        ) : (
          <ListPeopleTab listId={list.id} onChanged={onChanged} />
        )}
      </div>
    </section>
  );
}
