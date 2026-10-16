"use client";

import { useEffect, useState } from "react";

export const PAGE_SIZE = 50;

type Page<T> = { total: number; items: T[] };

/**
 * One page of rows from the backend, plus paging and tick-box selection.
 * `fetchPage` must be a module-level function (so it never changes); `filterKey` is the
 * JSON of the filters. Changing the filters goes back to page 1 and clears the selection.
 */
export function useRows<T extends { id: number }>(
  fetchPage: (filterKey: string, limit: number, offset: number) => Promise<Page<T>>,
  filterKey: string,
) {
  const [paging, setPaging] = useState({ key: "", page: 0 });
  const [reloadKey, setReloadKey] = useState(0);
  const [data, setData] = useState<Page<T>>({ total: 0, items: [] });
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<{ key: string; ids: Set<number> }>({ key: "", ids: new Set() });

  // If rows were removed, stay on the last page that still exists.
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  const page = Math.min(paging.key === filterKey ? paging.page : 0, pages - 1);

  useEffect(() => {
    let cancelled = false;
    fetchPage(filterKey, PAGE_SIZE, page * PAGE_SIZE)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setLoaded(true);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Request failed");
      });
    return () => {
      cancelled = true;
    };
  }, [fetchPage, filterKey, page, reloadKey]);

  const selected = selection.key === filterKey ? selection.ids : new Set<number>();
  const pageIds = data.items.map((r) => r.id);
  const setSelected = (ids: Set<number>) => setSelection({ key: filterKey, ids });

  return {
    data,
    loaded,
    error,
    page,
    pages,
    setPage: (p: number) => setPaging({ key: filterKey, page: p }),
    reload: () => setReloadKey((k) => k + 1),
    /** Change one row in place (e.g. its status) without re-reading the page. */
    patchRow: (id: number, changes: Partial<T>) =>
      setData((d) => ({ ...d, items: d.items.map((r) => (r.id === id ? { ...r, ...changes } : r)) })),
    selected,
    selectedIds: [...selected],
    allOnPageSelected: pageIds.length > 0 && pageIds.every((id) => selected.has(id)),
    someOnPageSelected: pageIds.some((id) => selected.has(id)),
    clearSelection: () => setSelected(new Set()),
    toggleRow: (id: number) => {
      const next = new Set(selected);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      setSelected(next);
    },
    togglePage: () => {
      const all = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
      const next = new Set(selected);
      for (const id of pageIds) {
        if (all) next.delete(id);
        else next.add(id);
      }
      setSelected(next);
    },
  };
}

/** Run a button's action: tracks "busy", shows the success message it returns or the error. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<string | void>, failMessage: string) {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      setNotice((await action()) ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : failMessage);
    } finally {
      setBusy(false);
    }
  }

  return { busy, notice, error, setError, run };
}
