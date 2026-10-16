"use client";

// Small building blocks shared by every page (companies table, people, lists).
import {
  LEAD_STATUS_LABEL,
  LEAD_STATUSES,
  SENIORITY_LABEL,
  type EmailStatus,
  type LeadStatus,
  type Seniority,
} from "@/lib/api";

const EMAIL_STATUS: Record<EmailStatus, { label: string; cls: string; hint: string }> = {
  valid: { label: "Verified", cls: "badge-success", hint: "The mail server confirmed this address exists." },
  catch_all: {
    label: "Catch-all",
    cls: "badge-warn",
    hint: "The domain accepts every address, so this one can't be confirmed. Usually OK to use.",
  },
  risky: { label: "Risky", cls: "badge-warn", hint: "Role or free-mail address, or the check was inconclusive." },
  unknown: { label: "Unknown", cls: "badge-muted", hint: "The mail server didn't answer clearly." },
  invalid: { label: "Invalid", cls: "badge-danger", hint: "This address does not exist. Don't email it." },
};

/** "Verified" / "Catch-all" / "Invalid"... badge for an email check result. */
export function EmailStatusBadge({ status, title }: { status: EmailStatus | null | undefined; title?: string }) {
  if (!status) return null;
  const s = EMAIL_STATUS[status] ?? EMAIL_STATUS.unknown;
  return (
    <span className={`badge ${s.cls}`} title={title ?? s.hint}>
      {s.label}
    </span>
  );
}

const SENIORITY_CLS: Record<Seniority, string> = {
  owner: "badge-accent",
  c_level: "badge-accent",
  director: "badge-success",
  manager: "badge-success",
  professional: "badge-muted",
  staff: "badge-muted",
};

export function SeniorityBadge({ seniority }: { seniority: Seniority | null | undefined }) {
  if (!seniority) return null;
  return <span className={`badge ${SENIORITY_CLS[seniority] ?? "badge-muted"}`}>{SENIORITY_LABEL[seniority] ?? seniority}</span>;
}

/** Lead score 0-100 as a coloured pill; hover shows why. */
export function ScoreBadge({ score, reasons }: { score: number | null | undefined; reasons?: string[] }) {
  if (score === null || score === undefined) return <span className="text-xs text-[var(--muted)]">—</span>;
  const cls = score >= 70 ? "badge-success" : score >= 40 ? "badge-warn" : "badge-muted";
  return (
    <span
      className={`badge ${cls} tabular-nums`}
      title={reasons && reasons.length ? reasons.join("\n") : undefined}
      aria-label={`Lead score ${score} out of 100`}
    >
      {score}
    </span>
  );
}

const STATUS_CLS: Record<LeadStatus, string> = {
  new: "",
  contacted: "text-[var(--accent)]",
  replied: "text-[var(--accent)]",
  interested: "text-[var(--success)]",
  won: "text-[var(--success)]",
  lost: "text-[var(--danger)]",
  not_interested: "text-[var(--muted)]",
};

/** Dropdown to change a lead's sales status. */
export function LeadStatusSelect({
  value,
  onChange,
  disabled,
  label = "Lead status",
}: {
  value: LeadStatus;
  onChange: (status: LeadStatus) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <select
      aria-label={label}
      className={`input w-auto py-1 text-xs ${STATUS_CLS[value] ?? ""}`}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as LeadStatus)}
    >
      {LEAD_STATUSES.map((s) => (
        <option key={s} value={s}>
          {LEAD_STATUS_LABEL[s]}
        </option>
      ))}
    </select>
  );
}
