"use client";

// Side panel with everything about one company: contact details, lead status and score,
// decision-makers with verified emails, website facts, and a notes/activity timeline.
import { useEffect, useId, useRef, useState } from "react";
import { AddToListButton } from "@/components/AddToListButton";
import { EmailStatusBadge, LeadStatusSelect, ScoreBadge, SeniorityBadge } from "@/components/ui";
import { api, type Activity, type Company, type Contact, type EmailStatus, type Job, type LeadStatus } from "@/lib/api";

// ----- small helpers shared with the results table ---------------------------------

const isActive = (j: Job) => j.status === "queued" || j.status === "running";

/** Local date + time. The backend sends UTC times with an offset, so new Date() is right. */
export function formatDate(iso: string | null | undefined) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Decision-makers first; otherwise keep the backend order (most senior first). */
export function sortTeam(contacts: Contact[]) {
  return [...contacts].sort((a, b) => Number(b.is_decision_maker) - Number(a.is_decision_maker));
}

/** Verification result for one of the company's emails (keys may be lower-case). */
export function emailStatusOf(company: Company, email: string): EmailStatus | null {
  const statuses = company.email_statuses ?? {};
  return statuses[email] ?? statuses[email.toLowerCase()] ?? null;
}

const EMAIL_SOURCE_LABEL: Record<string, string> = {
  website: "Found on the website",
  pattern: "Guessed from the company's email pattern",
  hunter: "Found by Hunter.io",
};

/** An email address with its verification badge. Invalid addresses are struck through. */
export function EmailLine({ email, status }: { email: string; status: EmailStatus | null }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {status === "invalid" ? (
        <span className="break-all text-[var(--muted)] line-through" title="This address does not exist">
          {email}
        </span>
      ) : (
        <a href={`mailto:${email}`} className="link break-all">
          {email}
        </a>
      )}
      <EmailStatusBadge status={status} />
    </span>
  );
}

/** A person's email with status badge, plus where it came from and how sure we are. */
export function ContactEmail({ contact: p, showDetails = false }: { contact: Contact; showDetails?: boolean }) {
  if (!p.email) return null;
  const source = p.email_source ? EMAIL_SOURCE_LABEL[p.email_source] ?? p.email_source : null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <EmailLine email={p.email} status={p.email_status} />
      {showDetails && p.email_confidence !== null && (
        <span className="text-[11px] tabular-nums text-[var(--muted)]" title="How confident we are this is their email">
          {p.email_confidence}% sure
        </span>
      )}
      {showDetails && source && <span className="text-[11px] text-[var(--muted)]">· {source}</span>}
    </span>
  );
}

/** Gold star marking a decision-maker. */
export function DecisionMakerStar() {
  return (
    <span className="text-[var(--warn)]" title="Decision-maker">
      ★<span className="sr-only">Decision-maker</span>
    </span>
  );
}

// ----- the drawer --------------------------------------------------------------------

// Elements that can take keyboard focus (used to keep Tab inside the panel).
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

type Loaded = { company: Company | null; activities: Activity[]; error: string | null };

export function CompanyDrawer({
  companyId,
  jobs,
  jobsVersion,
  onClose,
  onJobCreated,
  onChanged,
}: {
  companyId: number;
  jobs: Job[];
  jobsVersion: number; // changes when any job finishes, so the panel re-reads the company
  onClose: () => void;
  onJobCreated: (job: Job) => void;
  onChanged: () => void; // the company was edited here, so the table should reload
}) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const [loaded, setLoaded] = useState<Loaded>({ company: null, activities: [], error: null });
  const [reload, setReload] = useState(0); // bump to re-read the company and its timeline
  const [startedJobId, setStartedJobId] = useState<number | null>(null);
  const [busy, setBusy] = useState<"people" | "verify" | "status" | "note" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [note, setNote] = useState("");

  // Load the company and its activity timeline (again after a job finishes or an edit).
  useEffect(() => {
    let cancelled = false;
    Promise.all([api.getCompany(companyId), api.listActivities(companyId)])
      .then(([company, activities]) => {
        if (!cancelled) setLoaded({ company, activities, error: null });
      })
      .catch((e: Error) => {
        if (!cancelled) setLoaded((prev) => ({ ...prev, error: e.message }));
      });
    return () => {
      cancelled = true;
    };
  }, [companyId, jobsVersion, reload]);

  // Focus the close button when the panel opens, stop the page behind it from scrolling,
  // and give focus back to whatever had it (e.g. the business name) when it closes.
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      if (previous && document.contains(previous)) previous.focus();
    };
  }, []);

  // Esc closes; Tab and Shift+Tab cycle inside the panel. Listening on the whole document
  // (not just the panel) keeps this working when the focused button becomes disabled and
  // the browser moves focus to <body> (e.g. after clicking "Verify emails").
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      // defaultPrevented: a menu inside the panel (e.g. "Add to list") already used this Esc.
      if (e.key === "Escape" && !e.defaultPrevented) {
        onCloseRef.current();
        return;
      }
      const panel = panelRef.current;
      if (e.key !== "Tab" || !panel) return;
      const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement;
      if (!panel.contains(current)) {
        // Focus fell out of the panel: bring it back in.
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && (current === first || current === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && current === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const company = loaded.company;
  // A job working on this company: the one started here, or one started from the table.
  const runningJob = jobs.find(
    (j) => isActive(j) && (j.id === startedJobId || (j.params.company_ids ?? []).includes(companyId)),
  );
  const startedJob = startedJobId !== null ? jobs.find((j) => j.id === startedJobId) : undefined;

  async function startJob(kind: "people" | "verify") {
    setBusy(kind);
    setActionError(null);
    try {
      const job =
        kind === "people"
          ? await api.findDecisionMakers(companyId)
          : await api.findEmails([companyId], { force: true }); // re-check even recently checked emails
      setStartedJobId(job.id);
      onJobCreated(job);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Could not start");
    } finally {
      setBusy(null);
    }
  }

  async function changeStatus(status: LeadStatus) {
    if (!company) return;
    const before = company.lead_status;
    // Show the new status at once; undo it if saving fails.
    setLoaded((prev) => (prev.company ? { ...prev, company: { ...prev.company, lead_status: status } } : prev));
    setBusy("status");
    setActionError(null);
    try {
      const updated = await api.updateCompany(companyId, { lead_status: status });
      setLoaded((prev) => ({ ...prev, company: updated }));
      setReload((r) => r + 1); // the timeline got a "Status: New → Contacted" entry
      onChanged();
    } catch (err) {
      setLoaded((prev) => (prev.company ? { ...prev, company: { ...prev.company, lead_status: before } } : prev));
      setActionError(err instanceof Error ? err.message : "Could not change the status");
    } finally {
      setBusy(null);
    }
  }

  async function addNote(e: React.FormEvent) {
    e.preventDefault();
    const text = note.trim();
    if (!text) return;
    setBusy("note");
    setActionError(null);
    try {
      const activity = await api.addNote(companyId, text);
      setLoaded((prev) => ({ ...prev, activities: [activity, ...prev.activities] }));
      setNote("");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Could not save the note");
    } finally {
      setBusy(null);
    }
  }

  const canFindPeople = Boolean(company?.website || company?.domain);
  const canVerify = Boolean(company && (company.website || company.domain || company.emails.length > 0));

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* Dimmed page behind the panel; clicking it closes the panel. */}
      <div className="absolute inset-0 bg-black/40" onClick={onClose} aria-hidden />

      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="drawer-panel relative flex h-full w-full flex-col border-l border-[var(--border)] bg-[var(--background)] shadow-2xl outline-none sm:max-w-2xl"
      >
        {/* Header */}
        <div className="border-b border-[var(--border)] bg-[var(--card)] px-4 py-3 sm:px-5">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <h2 id={titleId} className="text-lg font-semibold leading-snug">
                {company?.name ?? (loaded.error ? "Could not load this lead" : "Loading…")}
              </h2>
              {company && <CompanySubtitle company={company} />}
            </div>
            <button
              ref={closeRef}
              className="btn-secondary shrink-0 px-2.5"
              onClick={onClose}
              aria-label="Close details"
              title="Close (Esc)"
            >
              ✕
            </button>
          </div>

          {company && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <LeadStatusSelect value={company.lead_status} onChange={changeStatus} disabled={busy === "status"} />
              <span className="flex items-center gap-1 text-xs text-[var(--muted)]">
                Score <ScoreBadge score={company.score} reasons={company.score_reasons} />
              </span>
              {company.list_ids.length > 0 && (
                <span className="badge badge-accent">
                  In {company.list_ids.length} {company.list_ids.length === 1 ? "list" : "lists"}
                </span>
              )}
              <span className="flex-1" />
              <button
                className="btn-secondary text-xs"
                disabled={!canFindPeople || busy !== null || runningJob !== undefined}
                onClick={() => startJob("people")}
                title={
                  canFindPeople
                    ? "Crawl the website (if needed), then find and verify the emails of the owners and managers"
                    : "This business has no website, so there is no one to look up"
                }
              >
                {busy === "people" ? "Starting…" : "Find decision-makers"}
              </button>
              <button
                className="btn-secondary text-xs"
                disabled={!canVerify || busy !== null || runningJob !== undefined}
                onClick={() => startJob("verify")}
                title="Ask the mail servers whether these emails exist (no email is sent)"
              >
                {busy === "verify" ? "Starting…" : "Verify emails"}
              </button>
              <AddToListButton companyIds={[company.id]} onDone={() => setReload((r) => r + 1)} size="sm" />
            </div>
          )}
        </div>

        {/* Body */}
        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4 sm:px-5">
          {actionError && (
            <p role="alert" className="text-sm text-[var(--danger)]">
              {actionError}
            </p>
          )}
          {loaded.error && (
            <div className="card flex flex-wrap items-center gap-3 text-sm text-[var(--danger)]">
              Could not load: {loaded.error}
              <button className="btn-secondary" onClick={() => setReload((r) => r + 1)}>
                Try again
              </button>
            </div>
          )}

          <JobProgress running={runningJob} finished={runningJob ? undefined : startedJob} />

          {company && (
            <>
              {company.score_reasons.length > 0 && (
                <Section title="Why this score">
                  <ul className="space-y-0.5 text-sm">
                    {company.score_reasons.map((r) => (
                      <li key={r} className="flex gap-2">
                        <span aria-hidden className="text-[var(--muted)]">
                          •
                        </span>
                        {r}
                      </li>
                    ))}
                  </ul>
                </Section>
              )}
              <EmailsSection company={company} />
              <PeopleSection contacts={company.contacts} />
              <WebsiteSection company={company} />
              <ActivitySection
                company={company}
                activities={loaded.activities}
                note={note}
                onNoteChange={setNote}
                onSubmit={addNote}
                saving={busy === "note"}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ----- parts of the drawer -----------------------------------------------------------

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section className="card space-y-2">
      <h3 className="text-sm font-semibold">
        {title}
        {count !== undefined && <span className="ml-1 font-normal text-[var(--muted)]">({count})</span>}
      </h3>
      {children}
    </section>
  );
}

function CompanySubtitle({ company: c }: { company: Company }) {
  const closed = c.business_status && c.business_status !== "OPERATIONAL";
  return (
    <div className="mt-1 space-y-1 text-xs text-[var(--muted)]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {c.category && <span className="capitalize">{c.category}</span>}
        {c.rating !== null && (
          <span title={`Rated ${c.rating} out of 5`}>
            <span className="text-[var(--warn)]">★</span> {c.rating.toFixed(1)}
            {c.reviews_count !== null && ` (${c.reviews_count.toLocaleString()} reviews)`}
          </span>
        )}
        {closed && <span className="badge badge-danger">{c.business_status!.toLowerCase().replaceAll("_", " ")}</span>}
      </div>
      {c.address && <div>{c.address}</div>}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        {c.phone && (
          <a href={`tel:${c.phone}`} className="link">
            {c.phone}
          </a>
        )}
        {c.website && (
          <a href={c.website} target="_blank" rel="noreferrer" className="link break-all">
            {c.domain ?? c.website} ↗
          </a>
        )}
        {c.google_maps_url && (
          <a href={c.google_maps_url} target="_blank" rel="noreferrer" className="link">
            Google Maps ↗
          </a>
        )}
      </div>
    </div>
  );
}

/** Progress of a crawl / email check working on this company, or how the last one ended. */
function JobProgress({ running, finished }: { running?: Job; finished?: Job }) {
  if (running) {
    const pct = running.total > 0 ? Math.round((running.processed / running.total) * 100) : 0;
    const what = running.kind === "emails" ? "Finding and checking emails" : "Crawling the website";
    return (
      <div className="card space-y-2 border-[var(--accent)]" role="status" aria-live="polite">
        <div className="flex items-center justify-between gap-2 text-sm">
          <span className="font-medium">{running.status === "queued" ? "Waiting in queue…" : `${what}…`}</span>
          {running.total > 0 && (
            <span className="text-xs tabular-nums text-[var(--muted)]">
              {running.processed}/{running.total}
            </span>
          )}
        </div>
        {running.status === "running" && running.total > 0 && (
          <div className="progress" aria-hidden>
            <div style={{ width: `${pct}%` }} />
          </div>
        )}
        {running.progress_message && <p className="text-xs text-[var(--muted)]">{running.progress_message}</p>}
      </div>
    );
  }
  if (!finished || isActive(finished)) return null;
  return (
    <p role="status" className={`text-sm ${finished.status === "failed" ? "text-[var(--danger)]" : "text-[var(--success)]"}`}>
      {finished.status === "failed" ? `Failed: ${finished.error ?? "unknown error"}` : finished.progress_message ?? "Done."}
    </p>
  );
}

function EmailsSection({ company: c }: { company: Company }) {
  // The best email first, then the rest (each address once, ignoring upper/lower case).
  const seen = new Set<string>();
  const emails = [c.email, ...c.emails].filter((e): e is string => {
    if (!e || seen.has(e.toLowerCase())) return false;
    seen.add(e.toLowerCase());
    return true;
  });
  return (
    <Section title="Emails" count={emails.length}>
      {emails.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">No email found yet. Try “Find decision-makers”.</p>
      ) : (
        <ul className="space-y-1 text-sm">
          {emails.map((e) => (
            <li key={e}>
              <EmailLine email={e} status={emailStatusOf(c, e)} />
            </li>
          ))}
        </ul>
      )}
      {c.emails_checked_at && (
        <p className="text-[11px] text-[var(--muted)]">Checked {formatDate(c.emails_checked_at)}</p>
      )}
    </Section>
  );
}

function PeopleSection({ contacts }: { contacts: Contact[] }) {
  const team = sortTeam(contacts);
  return (
    <Section title="People" count={team.length}>
      {team.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">
          No people found yet. “Find decision-makers” looks for owners and managers on the website.
        </p>
      ) : (
        <ul className="space-y-2">
          {team.map((p) => (
            <li key={p.id} className="rounded-lg border border-[var(--border)] px-3 py-2">
              <div className="flex flex-wrap items-center gap-1.5">
                {p.is_decision_maker && <DecisionMakerStar />}
                <span className="font-medium">{p.full_name ?? "Unknown name"}</span>
                <SeniorityBadge seniority={p.seniority} />
                {p.department && <span className="badge badge-muted capitalize">{p.department}</span>}
              </div>
              {p.title && <div className="text-xs text-[var(--muted)]">{p.title}</div>}
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                <ContactEmail contact={p} showDetails />
                {p.phone && (
                  <a href={`tel:${p.phone}`} className="link">
                    {p.phone}
                  </a>
                )}
                {p.linkedin_url && (
                  <a href={p.linkedin_url} target="_blank" rel="noreferrer" className="link">
                    LinkedIn ↗
                  </a>
                )}
              </div>
              {(p.source || p.source_url) && (
                <div className="mt-1 text-[11px] text-[var(--muted)]">
                  Source:{" "}
                  {p.source_url ? (
                    <a href={p.source_url} target="_blank" rel="noreferrer" className="link break-all">
                      {p.source ?? p.source_url}
                    </a>
                  ) : (
                    p.source
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

const OUTDATED_BEFORE_YEAR = new Date().getFullYear() - 2;

function WebsiteSection({ company: c }: { company: Company }) {
  const s = c.website_signals ?? {};
  // Website facts only mean something after a successful crawl.
  const crawled = c.enrich_status === "done";
  const socials = Object.entries(c.socials ?? {});
  const facts: [string, React.ReactNode][] = [];
  if (crawled) {
    if (s.https !== null && s.https !== undefined) facts.push(["Secure (HTTPS)", s.https ? "Yes" : "No — no padlock"]);
    if (s.platform) facts.push(["Platform", s.platform]);
    if (s.copyright_year)
      facts.push([
        "Copyright year",
        s.copyright_year < OUTDATED_BEFORE_YEAR ? `${s.copyright_year} (may be outdated)` : String(s.copyright_year),
      ]);
    if (s.has_contact_form !== undefined) facts.push(["Contact form", s.has_contact_form ? "Yes" : "No"]);
    if (s.pages_crawled !== undefined) facts.push(["Pages read", String(s.pages_crawled)]);
    if (s.title) facts.push(["Page title", s.title]);
    if (s.description) facts.push(["Description", s.description]);
  }

  let crawlText = "Not crawled yet";
  if (c.enrich_status === "done") crawlText = `Crawled ${formatDate(c.enriched_at)}`;
  else if (c.enrich_status === "failed") crawlText = `Crawl failed: ${c.enrich_error ?? "unknown error"}`;
  else if (c.enrich_status === "no_website" || !c.website) crawlText = "No website";

  return (
    <Section title="Website signals">
      <p className={`text-xs ${c.enrich_status === "failed" ? "text-[var(--danger)]" : "text-[var(--muted)]"}`}>
        {crawlText}
      </p>
      {facts.length > 0 && (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
          {facts.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-[var(--muted)]">{label}</dt>
              <dd className="min-w-0 break-words">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {socials.length > 0 && (
        <div className="flex flex-wrap gap-x-3 text-sm">
          {socials.map(([net, url]) => (
            <a
              key={net}
              href={url.startsWith("http") ? url : `https://${url}`}
              target="_blank"
              rel="noreferrer"
              className="link capitalize"
            >
              {net} ↗
            </a>
          ))}
        </div>
      )}
    </Section>
  );
}

function ActivitySection({
  company,
  activities,
  note,
  onNoteChange,
  onSubmit,
  saving,
}: {
  company: Company;
  activities: Activity[];
  note: string;
  onNoteChange: (text: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  saving: boolean;
}) {
  const noteId = useId();
  const names = new Map(company.contacts.map((p) => [p.id, p.full_name ?? "a team member"]));
  return (
    <Section title="Notes & activity">
      <form onSubmit={onSubmit} className="space-y-2">
        <label htmlFor={noteId} className="label">
          Add a note
        </label>
        <textarea
          id={noteId}
          className="input min-h-20 resize-y"
          placeholder="e.g. Called, spoke to the owner. Call back next week."
          value={note}
          onChange={(e) => onNoteChange(e.target.value)}
          maxLength={5000}
        />
        <button className="btn-primary" disabled={saving || note.trim() === ""}>
          {saving ? "Saving…" : "Add note"}
        </button>
      </form>

      {activities.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">No notes or status changes yet.</p>
      ) : (
        <ol className="space-y-2 border-l-2 border-[var(--border)] pl-3">
          {activities.map((a) => (
            <li key={a.id} className="text-sm">
              <div className="flex flex-wrap items-center gap-x-2 text-[11px] text-[var(--muted)]">
                <span className={`badge ${a.kind === "note" ? "badge-accent" : "badge-muted"}`}>
                  {a.kind === "note" ? "Note" : "Status"}
                </span>
                <time dateTime={a.created_at}>{formatDate(a.created_at)}</time>
                {a.contact_id !== null && names.has(a.contact_id) && <span>· about {names.get(a.contact_id)}</span>}
              </div>
              <p className="mt-0.5 whitespace-pre-wrap break-words">{a.text}</p>
            </li>
          ))}
        </ol>
      )}
    </Section>
  );
}
