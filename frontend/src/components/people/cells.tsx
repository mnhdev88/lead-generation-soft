"use client";

// Table cells shared by the People page and the People tab of a list.
import Link from "next/link";
import { DecisionMakerStar, EmailLine } from "@/components/CompanyDrawer";
import { SeniorityBadge } from "@/components/ui";
import type { CompanyBrief, Person } from "@/lib/api";

const EMAIL_SOURCE_LABEL: Record<string, string> = {
  website: "website",
  pattern: "pattern guess",
  hunter: "Hunter.io",
};

/** Link to a company on the Search page, which opens its detail panel. */
export function companyHref(id: number) {
  return `/?company=${id}`;
}

export function Missing() {
  return <span className="text-[var(--muted)]">—</span>;
}

export function PersonNameCell({ person: p }: { person: Person }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="font-medium">{p.full_name ?? "Unknown name"}</span>
      {p.linkedin_url && (
        <a
          href={p.linkedin_url}
          target="_blank"
          rel="noreferrer"
          className="inline-grid h-4 w-4 shrink-0 place-items-center rounded-sm bg-[#0a66c2] text-[9px] font-bold leading-none text-white hover:opacity-80"
          title="Open LinkedIn profile"
          aria-label={`LinkedIn profile of ${p.full_name ?? "this person"}`}
        >
          in
        </a>
      )}
    </div>
  );
}

export function PersonTitleCell({ person: p }: { person: Person }) {
  return (
    <div className="min-w-40 space-y-0.5">
      {p.title ? <div className="text-sm">{p.title}</div> : <Missing />}
      {(p.seniority || p.is_decision_maker) && (
        <div className="flex flex-wrap items-center gap-1">
          {p.is_decision_maker && <DecisionMakerStar />}
          <SeniorityBadge seniority={p.seniority} />
        </div>
      )}
    </div>
  );
}

export function CompanyCell({ company: c, showPhone = true }: { company: CompanyBrief | null; showPhone?: boolean }) {
  if (!c) return <Missing />;
  return (
    <div className="min-w-44 space-y-0.5">
      <Link href={companyHref(c.id)} className="font-medium hover:text-[var(--accent)] hover:underline" title="Open company details">
        {c.name}
      </Link>
      <div className="flex flex-wrap gap-x-2 text-xs text-[var(--muted)]">
        {c.category && <span className="capitalize">{c.category}</span>}
        {c.city && <span>{c.city}</span>}
      </div>
      <div className="flex flex-wrap gap-x-2 text-xs">
        {c.website && (
          <a href={c.website} target="_blank" rel="noreferrer" className="link break-all">
            {c.domain ?? c.website}
          </a>
        )}
        {showPhone && c.phone && <span className="whitespace-nowrap text-[var(--muted)]">{c.phone}</span>}
      </div>
    </div>
  );
}

/** Email with its check result; hover the line below for the confidence. */
export function PersonEmailCell({ person: p }: { person: Person }) {
  if (!p.email) return <Missing />;
  const source = p.email_source ? (EMAIL_SOURCE_LABEL[p.email_source] ?? p.email_source) : null;
  return (
    <div className="space-y-0.5">
      <EmailLine email={p.email} status={p.email_status} />
      {(source || p.email_confidence !== null) && (
        <div
          className="text-[11px] text-[var(--muted)]"
          title={
            p.email_confidence !== null ? `We are ${p.email_confidence}% confident this is their email` : undefined
          }
        >
          {source && <>From {source}</>}
          {source && p.email_confidence !== null && " · "}
          {p.email_confidence !== null && <span className="tabular-nums">{p.email_confidence}%</span>}
        </div>
      )}
    </div>
  );
}
