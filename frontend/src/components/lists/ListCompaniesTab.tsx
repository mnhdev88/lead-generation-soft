"use client";

import Link from "next/link";
import { useState } from "react";
import { EmailLine, emailStatusOf } from "@/components/CompanyDrawer";
import { ExportMenu } from "@/components/ExportMenu";
import { BulkBar, Feedback, Pager, SelectAllBox } from "@/components/lists/parts";
import { useAction, useRows } from "@/components/lists/useRows";
import { companyHref, Missing } from "@/components/people/cells";
import { LeadStatusSelect, ScoreBadge } from "@/components/ui";
import { api, LEAD_STATUS_LABEL, type Company, type CompanyFilters, type LeadStatus } from "@/lib/api";

type SortBy = "name" | "score";

// Module-level so useRows sees the same function every render.
const fetchCompanies = (key: string, limit: number, offset: number) =>
  api.listCompanies(JSON.parse(key) as CompanyFilters, limit, offset);

/** The businesses saved in one list. */
export function ListCompaniesTab({ listId, onChanged }: { listId: number; onChanged: () => void }) {
  const [sort, setSort] = useState<SortBy>("name");
  const filters: CompanyFilters = { list_id: listId, sort: sort === "name" ? undefined : sort };
  const rows = useRows<Company>(fetchCompanies, JSON.stringify(filters));
  const action = useAction();

  const remove = (ids: number[]) =>
    action.run(async () => {
      const { changed } = await api.removeFromList(listId, { company_ids: ids });
      rows.clearSelection();
      rows.reload();
      onChanged();
      return `Removed ${changed} ${changed === 1 ? "business" : "businesses"} from this list.`;
    }, "Could not remove from the list");

  const setStatus = (status: LeadStatus) =>
    action.run(async () => {
      const { changed } = await api.bulkStatus({ company_ids: rows.selectedIds, lead_status: status });
      rows.reload();
      return `Status set to “${LEAD_STATUS_LABEL[status]}” for ${changed} ${changed === 1 ? "business" : "businesses"}.`;
    }, "Could not change the status");

  // Show the new status at once; undo it if saving fails.
  async function changeStatus(c: Company, status: LeadStatus) {
    const before = c.lead_status;
    rows.patchRow(c.id, { lead_status: status });
    action.setError(null);
    try {
      rows.patchRow(c.id, await api.updateCompany(c.id, { lead_status: status }));
    } catch (err) {
      rows.patchRow(c.id, { lead_status: before });
      action.setError(err instanceof Error ? err.message : "Could not change the status");
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-[var(--muted)]">Sort</span>
          <select className="input w-auto py-1" value={sort} onChange={(e) => setSort(e.target.value as SortBy)}>
            <option value="name">Name</option>
            <option value="score">Score (high first)</option>
          </select>
        </label>
        <ExportMenu kind="companies" filters={filters} />
      </div>

      <Feedback notice={action.notice} error={action.error} />
      <BulkBar
        count={rows.selected.size}
        noun="businesses"
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
        <table className="w-full min-w-[960px] text-sm">
          <thead className="bg-[var(--muted-bg)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
            <tr>
              <th className="w-8 px-3 py-2">
                <SelectAllBox
                  label="Select all businesses on this page"
                  all={rows.allOnPageSelected}
                  some={rows.someOnPageSelected}
                  disabled={rows.data.items.length === 0}
                  onChange={rows.togglePage}
                />
              </th>
              <th className="px-3 py-2">Business</th>
              <th className="px-3 py-2">Phone</th>
              <th className="px-3 py-2">Email</th>
              <th className="px-3 py-2">Website</th>
              <th className="px-3 py-2">Score</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.data.items.map((c) => {
              const isSelected = rows.selected.has(c.id);
              const best = c.email ?? c.emails[0] ?? null;
              return (
                <tr
                  key={c.id}
                  className={`border-t border-[var(--border)] align-top ${
                    isSelected ? "bg-[var(--accent-soft)]" : "hover:bg-[var(--muted-bg)]"
                  }`}
                >
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      className="checkbox mt-0.5"
                      aria-label={`Select ${c.name}`}
                      checked={isSelected}
                      onChange={() => rows.toggleRow(c.id)}
                    />
                  </td>
                  <td className="max-w-72 px-3 py-2">
                    <Link
                      href={companyHref(c.id)}
                      className="font-medium hover:text-[var(--accent)] hover:underline"
                      title="Open company details"
                    >
                      {c.name}
                    </Link>
                    <div className="flex flex-wrap gap-x-2 text-xs text-[var(--muted)]">
                      {c.category && <span className="capitalize">{c.category}</span>}
                      {c.city && <span>{c.city}</span>}
                    </div>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">{c.phone ?? <Missing />}</td>
                  <td className="px-3 py-2">
                    {best ? <EmailLine email={best} status={emailStatusOf(c, best)} /> : <Missing />}
                  </td>
                  <td className="px-3 py-2">
                    {c.website ? (
                      <a href={c.website} target="_blank" rel="noreferrer" className="link break-all">
                        {c.domain ?? c.website}
                      </a>
                    ) : (
                      <span className="badge badge-warn">No website</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <ScoreBadge score={c.score} reasons={c.score_reasons} />
                  </td>
                  <td className="px-3 py-2">
                    <LeadStatusSelect
                      value={c.lead_status}
                      onChange={(s) => void changeStatus(c, s)}
                      label={`Lead status of ${c.name}`}
                    />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      className="btn-xs"
                      disabled={action.busy}
                      onClick={() => void remove([c.id])}
                      aria-label={`Remove ${c.name} from this list`}
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
                      No businesses in this list yet. Tick businesses on the{" "}
                      <Link href="/" className="link">
                        Search page
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
