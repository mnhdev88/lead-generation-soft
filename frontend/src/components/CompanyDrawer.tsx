"use client";

// Side panel with everything about one company: contact details, lead status and score,
// decision-makers with verified emails, website facts, and a notes/activity timeline.
import { useEffect, useId, useRef, useState } from "react";
import { AddToListButton } from "@/components/AddToListButton";
import {
  EmailStatusBadge,
  FindOnLinkedIn,
  IntentBadge,
  LeadStatusSelect,
  Phone,
  ScoreBadge,
  SeniorityBadge,
} from "@/components/ui";
import {
  api,
  bandFor,
  HIRING_ROLE_LABEL,
  HOT_INTENT,
  linkedinCompanySearchUrl,
  NO_SIZE_INFO_REASON,
  OFFER_LABEL,
  oldCrawlNote,
  OFFERS,
  PAIN_POINT_LABEL,
  salesFactRows,
  savedLinkedinUrl,
  siteFactsUsable,
  SIZE_BAND_LABEL,
  SIZE_BANDS,
  type Activity,
  type Company,
  type Contact,
  type EmailStatus,
  type Job,
  type LeadStatus,
  type ReviewPainPoint,
  type SizeConfidence,
  type WebsiteSignals,
  websiteNotRead,
} from "@/lib/api";

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
  apollo: "Found by Apollo.io",
};

// Where a person came from, for the sources that need explaining.
const CONTACT_SOURCE_LABEL: Record<string, string> = {
  linkedin_search: "via LinkedIn search",
  apollo: "via Apollo",
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
  const [busy, setBusy] = useState<"people" | "owner" | "verify" | "status" | "note" | null>(null);
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

  async function startJob(kind: "people" | "owner" | "verify") {
    setBusy(kind);
    setActionError(null);
    try {
      const job =
        kind === "people"
          ? await api.findDecisionMakers(companyId)
          : kind === "owner"
            ? await api.findPeople([companyId]) // then finds their emails too
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
                disabled={busy !== null || runningJob !== undefined}
                onClick={() => startJob("owner")}
                title="Look for the owner in web search results that point to LinkedIn profiles (and in Apollo.io if it is set up), then find their email. We never open LinkedIn pages ourselves."
              >
                {busy === "owner" ? "Starting…" : "Find owner (LinkedIn search / Apollo)"}
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
              <IntentSection company={company} />
              <SizeSection company={company} />
              <EmailsSection company={company} />
              <PeopleSection contacts={company.contacts} companyName={company.name} />
              <GoogleProfileSection company={company} />
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
        {c.phone && <Phone phone={c.phone} type={c.phone_type} />}
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
        <CompanyLinkedinLink company={c} />
      </div>
    </div>
  );
}

/** The company's saved LinkedIn page, or else a LinkedIn company search for its name. */
function CompanyLinkedinLink({ company: c }: { company: Company }) {
  const saved = savedLinkedinUrl(c.socials);
  return (
    <a
      href={saved ?? linkedinCompanySearchUrl(c.name)}
      target="_blank"
      rel="noreferrer"
      className="link"
      title={
        saved
          ? "Open this business's LinkedIn page"
          : "Search LinkedIn for this business (opens LinkedIn in a new tab; you may need to log in there)"
      }
    >
      {saved ? "LinkedIn page ↗" : "Search LinkedIn ↗"}
    </a>
  );
}

/** Progress of a crawl / email check / owner lookup working on this company, or how the last one ended. */
function JobProgress({ running, finished }: { running?: Job; finished?: Job }) {
  if (running) {
    const pct = running.total > 0 ? Math.round((running.processed / running.total) * 100) : 0;
    const what =
      running.kind === "emails"
        ? "Finding and checking emails"
        : running.kind === "people"
          ? "Looking up owners"
          : "Crawling the website";
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

function PeopleSection({ contacts, companyName }: { contacts: Contact[]; companyName: string }) {
  const team = sortTeam(contacts);
  return (
    <Section title="People" count={team.length}>
      {team.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">
          No people found yet. “Find decision-makers” looks for owners and managers on the website; “Find owner”
          looks in LinkedIn search results (and Apollo.io if set up).
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
                {p.phone && <Phone phone={p.phone} type={p.phone_type} />}
                {p.linkedin_url ? (
                  <a href={p.linkedin_url} target="_blank" rel="noreferrer" className="link">
                    LinkedIn ↗
                  </a>
                ) : (
                  p.full_name && <FindOnLinkedIn name={p.full_name} companyName={companyName} />
                )}
              </div>
              <ContactSourceLine contact={p} />
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/** Where we found this person: "Found via LinkedIn search ↗", or "Source: website" for the others. */
function ContactSourceLine({ contact: p }: { contact: Contact }) {
  if (!p.source && !p.source_url) return null;
  const known = p.source ? CONTACT_SOURCE_LABEL[p.source] : undefined;
  const text = known ?? p.source ?? p.source_url;
  return (
    <div className="mt-1 text-[11px] text-[var(--muted)]">
      {known ? "Found " : "Source: "}
      {p.source_url ? (
        <a href={p.source_url} target="_blank" rel="noreferrer" className="link break-all">
          {text}
          {known && " ↗"}
        </a>
      ) : (
        text
      )}
    </div>
  );
}

/** How likely the business is to buy each service now, with the reasons. */
function IntentSection({ company: c }: { company: Company }) {
  const intent = c.intent ?? {};
  const offers = OFFERS.filter((o) => intent[o]);
  // Scores worked out before the website was read leave out every website clue (booking,
  // ads, schema…), so a low number here is not final yet.
  const unread = websiteNotRead(c);
  return (
    <Section title="Sales opportunities">
      {unread && offers.length > 0 && (
        <p className="text-xs text-[var(--muted)]">
          Website not read yet. These scores leave out website clues and may go up after a crawl.
        </p>
      )}
      {offers.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">Not worked out yet. Crawling the website adds the most clues.</p>
      ) : (
        <ul className="space-y-3">
          {offers.map((o) => {
            const { score, reasons = [] } = intent[o]!;
            const pct = Math.max(0, Math.min(100, score));
            return (
              <li key={o} className="space-y-1">
                <div className="flex items-center gap-3 text-sm">
                  <span className="w-20 shrink-0 font-medium">{OFFER_LABEL[o]}</span>
                  {/* The bar is decoration: the badge next to it says the number (and "Hot"). */}
                  <div className="progress flex-1" aria-hidden>
                    <div
                      style={{ width: `${pct}%`, ...(score >= HOT_INTENT ? { background: "var(--success)" } : {}) }}
                    />
                  </div>
                  <IntentBadge score={score} />
                </div>
                {reasons.length > 0 && (
                  <ul className="space-y-0.5 pl-[5.75rem] text-xs text-[var(--muted)]">
                    {reasons.map((r) => (
                      <li key={r} className="flex gap-2">
                        <span aria-hidden>•</span>
                        {r}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

/** Facts from the Google Maps profile, its reviews, and Google's mobile speed test.
 * The whole section is left out when we know none of them. */
function GoogleProfileSection({ company: c }: { company: Company }) {
  const r = c.review_signals ?? {};
  const ps = c.pagespeed ?? {};
  const speedOk = !ps.error;
  const facts: [string, React.ReactNode][] = [];
  if (c.google_photos_count !== null && c.google_photos_count !== undefined)
    // Google sends at most 10 photos, so 10 means "10 or more".
    facts.push(["Photos on Google", c.google_photos_count >= 10 ? "10 or more" : String(c.google_photos_count)]);
  if (c.search_rank) facts.push(["Google search position", `#${c.search_rank} in our search results`]);
  if (c.peer_reviews_median && c.reviews_count !== null)
    facts.push([
      "Reviews vs similar",
      `${c.reviews_count.toLocaleString()} reviews (similar businesses nearby: about ${c.peer_reviews_median.toLocaleString()})`,
    ]);
  if (r.likely_new) facts.push(["Possibly new business", "Yes — few reviews, all from the last year"]);
  if (r.low_rating_share)
    facts.push(["Low ratings", `${Math.round(r.low_rating_share * 100)}% of the reviews we read are 3 stars or less`]);
  if (speedOk && typeof ps.performance === "number")
    facts.push(["Mobile speed", `${ps.performance}/100 (Google PageSpeed)`]);
  if (speedOk && typeof ps.seo === "number") facts.push(["Google SEO check", `${ps.seo}/100`]);

  const pains = (Object.entries(r.pain_points ?? {}) as [ReviewPainPoint, string[] | undefined][]).filter(
    (entry): entry is [ReviewPainPoint, string[]] => Array.isArray(entry[1]) && entry[1].length > 0,
  );
  if (facts.length === 0 && pains.length === 0) return null;
  return (
    <Section title="Google profile">
      <FactList facts={facts} />
      {pains.map(([kind, quotes]) => (
        <div key={kind} className="text-sm">
          <p className="font-medium">Reviews mention: {PAIN_POINT_LABEL[kind] ?? kind.replaceAll("_", " ")}</p>
          <ul className="mt-0.5 space-y-0.5 text-xs text-[var(--muted)]">
            {quotes.map((q) => (
              <li key={q}>
                <q>{q}</q>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {speedOk && ps.checked_at && typeof ps.performance === "number" && (
        <p className="text-[11px] text-[var(--muted)]">Speed checked {formatDate(ps.checked_at)}</p>
      )}
    </Section>
  );
}

/** The website facts that show what the business has (booking, ads, chat…). The wording
 * rules live in salesFactRows (lib/api.ts): "found" is a fact, "not found" is said carefully
 * and only where it matters for this kind of business. */
function salesFacts(c: Company, s: WebsiteSignals): [string, React.ReactNode][] {
  const facts: [string, React.ReactNode][] = salesFactRows(s, c.business_kind);
  if (s.hiring_roles && s.hiring_roles.length > 0) {
    const titles = (s.hiring_titles ?? []).slice(0, 10);
    facts.push([
      "Hiring for",
      <>
        {s.hiring_roles.map((role) => HIRING_ROLE_LABEL[role] ?? role).join(", ")}
        {titles.length > 0 && <span className="block text-xs text-[var(--muted)]">{titles.join(" · ")}</span>}
      </>,
    ]);
  }
  return facts;
}

/** Where the headcount came from, in words. `s` = the website facts (empty when not crawled). */
function employeesSourceText(c: Company, s: WebsiteSignals) {
  if (c.employees_source === "website") {
    // A number hidden in the page's code (JSON-LD) is often a template's default, not a real count.
    if (s.employees_from === "jsonld") return "from the website's page data (may be a template value)";
    return "website says";
  }
  if (c.employees_source === "team_page") return "a minimum: people listed on the website";
  if (c.employees_source === "ppp") {
    const year = c.ppp?.date_approved?.slice(0, 4);
    return `PPP loan${year ? ` ${year}` : ""} (self-reported)`;
  }
  return null;
}

/** A short note when the size band does not fit the headcount, e.g. "Medium" but 5 employees.
 * Null when they agree. */
function bandMismatchNote(c: Company) {
  const countBand = bandFor(c.employees);
  if (!c.size_band || !countBand || countBand === c.size_band) return null;
  const raised = SIZE_BANDS.indexOf(c.size_band) > SIZE_BANDS.indexOf(countBand);
  if (raised && c.locations_count && c.locations_count > 1)
    return `size raised because of ${c.locations_count} locations`;
  return `size ${raised ? "raised" : "lowered"} by other clues, see below`;
}

// Website links we are happy to open. A crawled page could hold "javascript:" or "data:" links.
const SAFE_LINK = /^https?:\/\//i;

const CONFIDENCE_LABEL: Record<SizeConfidence, string> = {
  high: "High",
  medium: "Medium",
  low: "Low — a rough guess",
};

/** How big the business is, and why we think so. */
function SizeSection({ company: c }: { company: Company }) {
  // Website facts only mean something when they came from a successful crawl (they are kept
  // when a later re-crawl fails, like the size estimate does in backend/app/sizing.py).
  const s: WebsiteSignals = c.website_signals?.pages_crawled ? c.website_signals : {};
  const ppp = c.ppp ?? {};
  // {not_applicable: true} means the PPP data was not checked (not a US company), so show nothing.
  const showPpp = !ppp.not_applicable && Object.keys(ppp).length > 0;
  // "No size information yet" only repeats "Size: Unknown", so we show a helpful hint instead.
  const reasons = (c.size_reasons ?? []).filter((r) => r !== NO_SIZE_INFO_REASON);
  const facts: [string, React.ReactNode][] = [];
  facts.push(["Size", c.size_band ? SIZE_BAND_LABEL[c.size_band] : "Unknown"]);
  if (c.employees) {
    const source = employeesSourceText(c, s);
    const note = bandMismatchNote(c);
    // The sentence the number came from is already quoted in the reasons below.
    facts.push([
      "Employees",
      <>
        {c.employees.toLocaleString()}
        {source && <span className="text-[var(--muted)]"> · {source}</span>}
        {note && <span className="block text-xs text-[var(--muted)]">({note})</span>}
      </>,
    ]);
  }
  if (c.size_band && c.size_confidence) facts.push(["Confidence", CONFIDENCE_LABEL[c.size_confidence]]);
  const founded = c.founded_year ?? s.founded_year;
  if (founded) facts.push(["Founded", String(founded)]);
  if (c.locations_count) facts.push(["Locations", String(c.locations_count)]);
  if (s.has_careers)
    facts.push([
      "Hiring",
      s.careers_url && SAFE_LINK.test(s.careers_url) ? (
        <a href={s.careers_url} target="_blank" rel="noreferrer" className="link">
          Yes — careers page ↗
        </a>
      ) : (
        "Yes — the website mentions jobs"
      ),
    ]);

  return (
    <Section title="Company size">
      <FactList facts={facts} />
      {showPpp && (
        <div className="rounded-lg border border-[var(--border)] px-3 py-2 text-sm">
          <p className="text-xs font-medium text-[var(--muted)]">US PPP loan data, 2020-21</p>
          <p className="font-medium">{ppp.name ?? "Unknown borrower"}</p>
          {(ppp.address || ppp.city) && (
            <p className="text-xs text-[var(--muted)]">
              {[ppp.address, ppp.city, [ppp.state, ppp.zip].filter(Boolean).join(" ")].filter(Boolean).join(", ")}
            </p>
          )}
          <p className="mt-1 text-xs">
            {ppp.jobs !== null && ppp.jobs !== undefined ? `${ppp.jobs} jobs reported` : "No job count given"}
            {ppp.date_approved && ` · approved ${ppp.date_approved}`}
            {ppp.loans && ppp.loans > 1 ? ` · ${ppp.loans} loans` : ""}
          </p>
          {ppp.match && (
            <p className="text-xs text-[var(--muted)]">
              Matched on: {ppp.match}
              {ppp.confidence === "high" && " (sure match)"}
              {ppp.confidence === "medium" && " (likely match, worth a check)"}
            </p>
          )}
        </div>
      )}
      {reasons.length > 0 && (
        <ul className="space-y-0.5 text-sm">
          {reasons.map((r) => (
            <li key={r} className="flex gap-2">
              <span aria-hidden className="text-[var(--muted)]">
                •
              </span>
              {r}
            </li>
          ))}
        </ul>
      )}
      {!c.size_band && (
        <p className="text-xs text-[var(--muted)]">
          No size clues yet. Crawling the website and finding people helps work it out.
        </p>
      )}
    </Section>
  );
}

/** Label / value pairs in two columns. */
function FactList({ facts }: { facts: [string, React.ReactNode][] }) {
  if (facts.length === 0) return null;
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {facts.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-[var(--muted)]">{label}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
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
  // The same rule as the intent score (backend/app/intent.py): a website and a successful crawl.
  // Facts are kept when a later re-crawl fails, so we then say they are from an older crawl.
  const extra = siteFactsUsable(c) ? salesFacts(c, s) : [];
  const oldNote = extra.length > 0 ? oldCrawlNote(c) : null;

  let crawlText = "Not crawled yet";
  if (c.enrich_status === "done") crawlText = `Crawled ${formatDate(c.enriched_at)}`;
  else if (c.enrich_status === "failed") crawlText = `Crawl failed: ${c.enrich_error ?? "unknown error"}`;
  else if (c.enrich_status === "no_website" || !c.website) crawlText = "No website";

  return (
    <Section title="Website signals">
      <p className={`text-xs ${c.enrich_status === "failed" ? "text-[var(--danger)]" : "text-[var(--muted)]"}`}>
        {crawlText}
      </p>
      <FactList facts={facts} />
      {oldNote && <p className="text-xs text-[var(--muted)]">{oldNote}</p>}
      <FactList facts={extra} />
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
                  {a.kind === "note" ? "Note" : a.kind === "merge" ? "Merged" : "Status"}
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
