"use client";

import Link from "next/link";
import { useState } from "react";
import { ExportMenu } from "@/components/ExportMenu";
import { BulkBar, Feedback, Pager, SelectAllBox } from "@/components/lists/parts";
import { useAction, useRows } from "@/components/lists/useRows";
import { CompanyCell, PersonEmailCell, PersonNameCell, PersonTitleCell } from "@/components/people/cells";
import { LeadStatusSelect, ScoreBadge } from "@/components/ui";
import { api, LEAD_STATUS_LABEL, type LeadStatus, type PeopleFilters, type Person } from "@/lib/api";

type SortBy = NonNullable<PeopleFilters["sort"]>;

// Module-level so useRows sees the same function every render.
const fetchPeople = (key: string, limit: number, offset: number) =>
  api.listPeople(JSON.parse(key) as PeopleFilters, limit, offset);

/** The people saved in one list. */
export function ListPeopleTab({ listId, onChanged }: { listId: number; onChanged: () => void }) {
  const [sort, setSort] = useState<SortBy>("seniority");
  const filters: PeopleFilters = { list_id: listId, sort: sort === "seniority" ? undefined : sort };
  const rows = useRows<Person>(fetchPeople, JSON.stringify(filters));
  const action = useAction();

  const remove = (ids: number[]) =>
    action.run(async () => {
      const { changed } = await api.removeFromList(listId, { contact_ids: ids });
      rows.clearSelection();
      rows.reload();
      onChanged();
      return `Removed ${changed} ${changed === 1 ? "person" : "people"} from this list.`;
    }, "Could not remove from the list");

  const setStatus = (status: LeadStatus) =>
    action.run(async () => {
      const { changed } = await api.bulkStatus({ contact_ids: rows.selectedIds, lead_status: status });
      rows.reload();
      return `Status set to “${LEAD_STATUS_LABEL[status]}” for ${changed} ${changed === 1 ? "person" : "people"}.`;
    }, "Could not change the status");

  // Show the new status at once; undo it if saving fails.
  async function changeStatus(p: Person, status: LeadStatus) {
    const before = p.lead_status;
    rows.patchRow(p.id, { lead_status: status });
    action.setError(null);
    try {
      rows.patchRow(p.id, await api.updatePerson(p.id, { lead_status: status }));
    } catch (err) {
      rows.patchRow(p.id, { lead_status: before });
      action.setError(err instanceof Error ? err.message : "Could not change the status");
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-[var(--muted)]">Sort</span>
          <select className="input w-auto py-1" value={sort} onChange={(e) => setSort(e.target.value as SortBy)}>
            <option value="seniority">Seniority</option>
            <option value="score">Score (high first)</option>
            <option value="name">Name</option>
          </select>
        </label>
        <ExportMenu kind="people" filters={filters} />
      </div>

      <Feedback notice={action.notice} error={action.error} />
      <BulkBar
        count={rows.selected.size}
        noun="people"
        busy={action.busy}
        onSetStatus={(s) => void setStatus(s)}
        onRemove={() => void remove(rows.selectedIds)}
        onClear={rows.clearSelection}
      />
      {rows.error && (
        <p role="alert" className="text-sm text-[var(--danger)]">
          Could not load this list: {rows.error}
        </p>
      )}

      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full min-w-[1000px] text-sm">
          <thead className="bg-[var(--muted-bg)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
            <tr>
              <th className="w-8 px-3 py-2">
                <SelectAllBox
                  label="Select everyone on this page"
                  all={rows.allOnPageSelected}
                  some={rows.someOnPageSelected}
                  disabled={rows.data.items.length === 0}
                  onChange={rows.togglePage}
                />
              </th>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Title</th>
              <th className="px-3 py-2">Company</th>
              <th className="px-3 py-2">Email</th>
              <th className="px-3 py-2">Score</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.data.items.map((p) => {
              const isSelected = rows.selected.has(p.id);
              const name = p.full_name ?? "this person";
              return (
                <tr
                  key={p.id}
                  className={`border-t border-[var(--border)] align-top ${
                    isSelected ? "bg-[var(--accent-soft)]" : "hover:bg-[var(--muted-bg)]"
                  }`}
                >
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      className="checkbox mt-0.5"
                      aria-label={`Select ${name}`}
                      checked={isSelected}
                      onChange={() => rows.toggleRow(p.id)}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <PersonNameCell person={p} />
                    {p.phone && <div className="text-xs text-[var(--muted)]">{p.phone}</div>}
                  </td>
                  <td className="max-w-60 px-3 py-2">
                    <PersonTitleCell person={p} />
                  </td>
                  <td className="max-w-64 px-3 py-2">
                    <CompanyCell company={p.company} />
                  </td>
                  <td className="px-3 py-2">
                    <PersonEmailCell person={p} />
                  </td>
                  <td className="px-3 py-2">
                    <ScoreBadge score={p.score} reasons={p.score_reasons} />
                  </td>
                  <td className="px-3 py-2">
                    <LeadStatusSelect
                      value={p.lead_status}
                      onChange={(s) => void changeStatus(p, s)}
                      label={`Lead status of ${name}`}
                    />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      className="btn-xs"
                      disabled={action.busy}
                      onClick={() => void remove([p.id])}
                      aria-label={`Remove ${name} from this list`}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              );
            })}
            {rows.data.items.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-10 text-center text-[var(--muted)]">
                  {!rows.loaded ? (
                    "Loading…"
                  ) : (
                    <>
                      No people in this list yet. Tick people on the{" "}
                      <Link href="/people" className="link">
                        People page
                      </Link>{" "}
                      and use “Add to list”.
                    </>
                  )}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager page={rows.page} pages={rows.pages} onPage={rows.setPage} />
    </div>
  );
}
