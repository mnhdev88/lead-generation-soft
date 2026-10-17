"use client";

// Small building blocks shared by every page (companies table, people, lists).
import {
  bandFor,
  HOT_INTENT,
  intentBadgeSpeech,
  LEAD_STATUS_LABEL,
  LEAD_STATUSES,
  linkedinPeopleSearchUrl,
  NO_SIZE_INFO_REASON,
  OFFER_LABEL,
  SENIORITY_LABEL,
  SIZE_BAND_LABEL,
  type EmailStatus,
  type EmployeesSource,
  type LeadStatus,
  type Offer,
  type PhoneType,
  type Seniority,
  type SizeBand,
  type SizeConfidence,
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

const PHONE_TYPE: Partial<Record<PhoneType, { label: string; cls: string; hint: string }>> = {
  mobile: { label: "Mobile", cls: "badge-success", hint: "A mobile number: you can also reach it on WhatsApp or by SMS." },
  landline: { label: "Landline", cls: "badge-muted", hint: "A landline: calls only." },
  toll_free: { label: "Toll-free", cls: "badge-muted", hint: "A toll-free number, often a call centre." },
  invalid: { label: "Invalid", cls: "badge-danger", hint: "This number can't exist in its country. Probably a typo." },
};

/** Phone number with a "Mobile" / "Invalid"... tag, and a WhatsApp link for mobiles. */
export function Phone({ phone, type, link = true }: { phone: string; type?: PhoneType | null; link?: boolean }) {
  const tag = type ? PHONE_TYPE[type] : undefined;
  const digits = phone.replace(/\D/g, "");
  const whatsapp = link && type === "mobile" && phone.startsWith("+");
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {link && type !== "invalid" ? (
        <a href={`tel:${phone.replace(/\s/g, "")}`} className="link">
          {phone}
        </a>
      ) : (
        <span className={type === "invalid" ? "line-through opacity-70" : undefined}>{phone}</span>
      )}
      {tag && (
        <span className={`badge ${tag.cls}`} title={tag.hint}>
          {tag.label}
        </span>
      )}
      {whatsapp && (
        <a href={`https://wa.me/${digits}`} target="_blank" rel="noreferrer" className="link text-xs">
          WhatsApp ↗
        </a>
      )}
    </span>
  );
}

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

/** Lead score 0-100 as a coloured pill; hover shows why. Screen readers hear the score and the
 * reasons from hidden text (most of them ignore an aria-label on a plain <span>). */
export function ScoreBadge({ score, reasons }: { score: number | null | undefined; reasons?: string[] }) {
  if (score === null || score === undefined) return <span className="text-xs text-[var(--muted)]">—</span>;
  const cls = score >= 70 ? "badge-success" : score >= 40 ? "badge-warn" : "badge-muted";
  const hasReasons = !!reasons && reasons.length > 0;
  return (
    <span className={`badge ${cls} tabular-nums`} title={hasReasons ? reasons.join("\n") : undefined}>
      <span aria-hidden>{score}</span>
      <span className="sr-only">
        Lead score {score} out of 100{hasReasons ? `. ${reasons.join(". ")}` : ""}
      </span>
    </span>
  );
}

// Added to the tooltip of a lead whose website was not read yet.
const UNCHECKED_HINT = "Website not read yet: the score leaves out website clues and may go up after a crawl.";

/** Buying intent 0-100 for one offer, e.g. "Hot · Website 72". `offer` adds the offer's name
 * (used when the table shows each lead's best offer). `unchecked` = the website was not read
 * yet, shown as a "?" after the score. Hover shows why; screen readers hear the same from
 * hidden text, and touch users see the reasons when they open the business. */
export function IntentBadge({
  score,
  offer,
  reasons,
  unchecked = false,
}: {
  score: number | null | undefined;
  offer?: Offer | null;
  reasons?: string[];
  unchecked?: boolean;
}) {
  if (score === null || score === undefined) return <span className="text-xs text-[var(--muted)]">—</span>;
  const hot = score >= HOT_INTENT;
  const cls = hot ? "badge-success" : score >= HOT_INTENT / 2 ? "badge-warn" : "badge-muted";
  const label = offer ? OFFER_LABEL[offer] ?? offer : null;
  const tip = [...(reasons ?? []), ...(unchecked ? [UNCHECKED_HINT] : [])];
  return (
    <span className={`badge ${cls} gap-1 tabular-nums`} title={tip.length ? tip.join("\n") : undefined}>
      {/* What you see. "Hot" in words: colour alone is not enough for colour-blind users. */}
      <span aria-hidden className="contents">
        {hot && <span className="font-semibold">Hot ·</span>}
        {label && <span>{label}</span>}
        {score}
        {unchecked && <span className="opacity-70">?</span>}
      </span>
      {/* What a screen reader says: the score in words plus the reasons. */}
      <span className="sr-only">{intentBadgeSpeech({ score, label, reasons, unchecked })}</span>
    </span>
  );
}

const SIZE_SHORT: Record<SizeBand, string> = { solo: "Solo", small: "Small", medium: "Medium", large: "Large" };

/** Company size as a pill, e.g. "Small · 8" ("8+" = at least 8 people listed on the website).
 * Hover shows why we think so. A guess we are not sure about is greyed out and starts with "~". */
export function SizeBadge({
  band,
  employees,
  source,
  confidence,
  reasons,
}: {
  band: SizeBand | null | undefined;
  employees?: number | null;
  source?: EmployeesSource | null;
  confidence?: SizeConfidence | null;
  reasons?: string[];
}) {
  if (!band) return <span className="text-xs text-[var(--muted)]">—</span>;
  // Show the headcount only when it fits the band. If other clues moved the band
  // (say "Medium" but the website says 5 people), "Medium · 5" would look like a bug,
  // so we show the band alone and the hover text explains why.
  // "Solo · 1" says the same thing twice, so solo shows no number.
  const showCount = employees && band !== "solo" && bandFor(employees) === band;
  const count = showCount ? ` · ${employees}${source === "team_page" ? "+" : ""}` : "";
  const roughGuess = confidence === "low";
  const hint = [
    SIZE_BAND_LABEL[band],
    ...(reasons ?? []).filter((r) => r !== NO_SIZE_INFO_REASON),
    confidence ? `Confidence: ${confidence}` : null,
  ].filter(Boolean);
  return (
    <span className={`badge ${roughGuess ? "badge-muted" : "badge-accent"} tabular-nums`} title={hint.join("\n")}>
      {/* "~" = roughly. Colour alone is not enough for colour-blind and screen-reader users. */}
      {roughGuess && <span aria-hidden>~</span>}
      {SIZE_SHORT[band] ?? band}
      {count}
      {roughGuess && <span className="sr-only"> (rough guess)</span>}
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

/** "Find on LinkedIn ↗": opens LinkedIn's own people search for this person in a new tab.
 *  Only for people we have a name for but no LinkedIn profile link. */
export function FindOnLinkedIn({ name, companyName }: { name: string; companyName?: string | null }) {
  return (
    <a
      href={linkedinPeopleSearchUrl(name, companyName)}
      target="_blank"
      rel="noreferrer"
      className="link text-xs"
      title="Search LinkedIn for this person (opens LinkedIn in a new tab; you may need to log in there)"
    >
      Find on LinkedIn ↗
    </a>
  );
}
