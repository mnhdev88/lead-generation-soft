"""Email verification: format -> domain (DNS/MX) -> mailbox (SMTP), with a cache.

verify_emails(["info@acme.com", "john@acme.com"]) -> {"info@acme.com": CheckResult(...), ...}

Final status of each address
----------------------------
=========  =================================================================================
invalid    Bad format, domain doesn't exist / can't receive mail, or the mail server said
           the mailbox doesn't exist (550 "user unknown", 5.1.1 ...). Microsoft 365's
           "5.4.1 Access denied" counts only when the same server accepted another address
           in the same session (otherwise: risky).
valid      The mail server accepted the address AND rejected a made-up address on the same
           domain (so it really checks mailboxes).
catch_all  The mail server accepts every address on the domain (also made-up ones), so we
           can't tell if this mailbox exists.
risky      A disposable (throw-away) address; a free-mail address at a provider that can
           never be checked (Yahoo/AOL accept everything); or Microsoft 365 rejected it
           ("5.4.1 Access denied") but also rejected every other address we asked about, so
           we can't be sure the server isn't simply refusing us. Avoid or double-check.
unknown    We couldn't decide: SMTP checking is off, port 25 is blocked, the server
           greylisted us ("try later"), refused to talk to our IP, or gave an unclear answer.
=========  =================================================================================

Role (info@, sales@) and free-mail (gmail...) addresses keep their normal status; the
is_role / is_free flags tell them apart. A role or Gmail address we merely *couldn't check*
(our network's fault) stays "unknown" rather than "risky", because website-listed addresses
of small businesses are usually real.

Cache: results are stored in the email_checks table and reused for EMAIL_CHECK_MAX_AGE_DAYS
("unknown" results only for 1 day, never once SMTP checking is switched on, and never when
the cause was on our side: port 25 blocked, our IP refused, DNS failure).
Domain facts (mail servers, catch-all) are stored in the domains table. A domain known to be
catch-all is not asked again while that fact is fresh (force=True asks again).

Politeness: at most 4 domains are checked at the same time, one session per mail provider at a
time, at most ~20 addresses per session, and every session has a time limit. A server provider that blocks us is left alone for
a few hours, and if port 25 looks blocked on this network SMTP is paused for 15 minutes.
"""
import logging
import secrets
import socket
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import config
from ..db import SessionLocal
from ..models import DomainInfo, EmailCheck
from . import smtp as smtp_mod
from .dns import REASON_DNS_ERROR, MxResult, Resolver, lookup_mx
from .syntax import check_syntax, is_disposable, is_free, is_role, split_email

log = logging.getLogger("emails.verify")

MAX_PARALLEL_DOMAINS = 4
UNKNOWN_MAX_AGE = timedelta(days=1)  # "unknown" results are re-checked sooner
BLOCKED_PAUSE_SECONDS = 6 * 3600  # leave a provider alone after it blocked us
PORT25_PAUSE_SECONDS = 15 * 60  # pause SMTP when port 25 seems blocked on this network
PORT25_FAILS_TO_PAUSE = 3  # ...after this many domains in a row could not be reached

REASON_NOT_CHECKED = "Domain OK; mailbox not checked"
REASON_VALID = "Mailbox exists"
REASON_CATCH_ALL = "Server accepts every address (catch-all), so the mailbox can't be confirmed"
REASON_MAYBE_CATCH_ALL = "Mail server accepted it, but may accept every address"
REASON_DISPOSABLE = "Disposable (throw-away) email address"
REASON_UNCHECKABLE = "Yahoo/AOL accept every address, so the mailbox can't be checked"
REASON_MS_UNSURE = "Microsoft 365 rejected it; the mailbox probably doesn't exist"
REASON_PORT25 = "SMTP check paused: port 25 seems blocked on this network"

# "unknown" because of OUR side (network, block, pause): not reused from the cache, so
# the address is checked again as soon as the problem is gone.
RETRY_REASONS = {REASON_PORT25, smtp_mod.REASON_BLOCKED, smtp_mod.REASON_CONNECT, REASON_DNS_ERROR}

# Mail providers whose servers say "OK" to every address at RCPT time (they bounce later).
UNCHECKABLE_MX_SUFFIXES = ("yahoodns.net", "aol.com", "yahoo.com")


@dataclass
class CheckResult:
    email: str
    status: str  # valid | invalid | catch_all | risky | unknown
    reason: str | None
    is_role: bool = False
    is_free: bool = False
    is_disposable: bool = False
    mx_found: bool | None = None  # None = not looked up / DNS failed
    smtp_code: int | None = None


# ----- shared SMTP state (protects mail servers and our IP's reputation) -----------

_state_lock = threading.Lock()
_host_locks: dict[str, threading.Lock] = {}
_blocked_until: dict[str, float] = {}  # provider key ("outlook.com") -> time.monotonic() deadline
_connect_fail_streak = 0
_port25_paused_until = 0.0


def reset_smtp_state() -> None:
    """Forget blocked providers and the port-25 pause (used by tests)."""
    global _connect_fail_streak, _port25_paused_until
    with _state_lock:
        _blocked_until.clear()
        _connect_fail_streak = 0
        _port25_paused_until = 0.0


def provider_key(mx_host: str) -> str:
    """'acme-com.mail.protection.outlook.com' -> 'outlook.com'; 'mx.acme.co.uk' -> 'acme.co.uk'."""
    parts = mx_host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "ac", "gov", "edu"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host_lock(mx_host: str) -> threading.Lock:
    """One lock per mail provider (not per host name): every Microsoft 365 customer has its
    own host name, but they all end at Microsoft, which must not get many sessions at once."""
    with _state_lock:
        return _host_locks.setdefault(provider_key(mx_host), threading.Lock())


def _is_blocked(mx_host: str) -> bool:
    with _state_lock:
        return _blocked_until.get(provider_key(mx_host), 0) > time.monotonic()


def _port25_paused() -> bool:
    with _state_lock:
        return _port25_paused_until > time.monotonic()


def _record_session(session: smtp_mod.SessionResult, mx_host: str) -> None:
    """Remember blocks and connection failures so we don't keep hammering."""
    global _connect_fail_streak, _port25_paused_until
    with _state_lock:
        if session.failure == "blocked":
            _blocked_until[provider_key(session.mx_host or mx_host)] = time.monotonic() + BLOCKED_PAUSE_SECONDS
        if session.failure == "connect":
            _connect_fail_streak += 1
            if _connect_fail_streak >= PORT25_FAILS_TO_PAUSE:
                log.warning("Could not reach %d mail servers in a row; pausing SMTP checks "
                            "(is port 25 blocked on this network?)", _connect_fail_streak)
                _port25_paused_until = time.monotonic() + PORT25_PAUSE_SECONDS
                _connect_fail_streak = 0
        else:
            _connect_fail_streak = 0


def _helo_host() -> str:
    """Name we greet mail servers with. Set SMTP_HELO_HOST to a real domain for best results;
    a bare PC name ("MY-PC") is turned into "my-pc.localdomain", as some servers insist on a dot."""
    if config.SMTP_HELO_HOST:
        return config.SMTP_HELO_HOST
    name = (socket.getfqdn() or "localhost").lower()
    return name if "." in name else f"{name}.localdomain"


def _mail_from(helo_host: str) -> str:
    return config.SMTP_MAIL_FROM or f"verify@{helo_host}"


# ----- per-domain work (runs in worker threads; no database access here) -----------

@dataclass
class _DomainFacts:
    """What the database already knew about a domain."""
    mx_hosts: list[str] = field(default_factory=list)
    mx_fresh: bool = False  # mx_hosts can be reused without a new DNS lookup
    accepts_all: bool | None = None
    accepts_all_fresh: bool = False  # accepts_all is recent enough to skip the catch-all probe


@dataclass
class _DomainOutcome:
    results: dict[str, CheckResult]
    mx: MxResult | None = None  # set when a new DNS lookup was made
    accepts_all: bool | None = None  # set when the catch-all probe gave an answer


def _check_domain(domain: str, addresses: list[str], facts: _DomainFacts, use_smtp: bool,
                  resolve: Resolver | None, smtp_factory: smtp_mod.SmtpFactory | None) -> _DomainOutcome:
    base = {a: CheckResult(a, "unknown", None, is_role(a), is_free(a), is_disposable(a)) for a in addresses}
    outcome = _DomainOutcome(results=base)

    if facts.mx_fresh:
        mx = MxResult("ok", hosts=facts.mx_hosts)
    else:
        mx = lookup_mx(domain, resolve)
        outcome.mx = mx
    if mx.state in ("no_domain", "no_mail"):
        for r in base.values():
            r.status, r.reason, r.mx_found = "invalid", mx.reason, False
        return outcome
    if mx.state != "ok":
        for r in base.values():
            r.reason = mx.reason
        return outcome
    for r in base.values():
        r.mx_found = True

    # Addresses worth asking the mail server about.
    to_ask = [a for a in addresses if not base[a].is_disposable]
    if use_smtp and to_ask and facts.accepts_all_fresh and facts.accepts_all:
        # We recently learned that this server accepts every address: asking again would
        # only give the same answer (and costs a session with the mail server).
        for a in to_ask:
            base[a].status, base[a].reason = "catch_all", REASON_CATCH_ALL
        return outcome
    skip_reason = None
    if not use_smtp:
        skip_reason = REASON_NOT_CHECKED
    elif any(mx.hosts[0].endswith(s) for s in UNCHECKABLE_MX_SUFFIXES):
        skip_reason = REASON_UNCHECKABLE
    elif _is_blocked(mx.hosts[0]):
        skip_reason = smtp_mod.REASON_BLOCKED
    elif _port25_paused():
        skip_reason = REASON_PORT25
    if skip_reason or not to_ask:
        for a in to_ask:
            base[a].reason = skip_reason
        return outcome

    helo = _helo_host()
    # Known (recently) NOT to be catch-all: no need for the made-up address again.
    accepts_all = False if facts.accepts_all_fresh and facts.accepts_all is False else None
    batch_size = smtp_mod.MAX_RCPT_PER_SESSION - 1
    for start in range(0, len(to_ask), batch_size):
        batch = to_ask[start:start + batch_size]
        # Ask about a made-up address once per domain, to detect catch-all servers.
        probe = f"zz-{secrets.token_hex(6)}@{domain}" if accepts_all is None else None
        with _host_lock(mx.hosts[0]):  # one session per mail server at a time
            session = smtp_mod.check_mailboxes(
                mx.hosts, batch, helo_host=helo, mail_from=_mail_from(helo),
                timeout=config.SMTP_TIMEOUT, probe_address=probe, factory=smtp_factory,
            )
        _record_session(session, mx.hosts[0])
        if session.probe is not None:
            category, _ = smtp_mod.classify_reply(session.probe.code, session.probe.message)
            if category == smtp_mod.ACCEPTED:
                accepts_all = True
            elif category == smtp_mod.REJECTED:
                accepts_all = False
        known_all = accepts_all if accepts_all is not None else facts.accepts_all
        for a in batch:
            _apply_reply(base[a], session, accepts_all, known_all)
        if session.failure in ("blocked", "connect"):
            for a in to_ask[start + batch_size:]:
                base[a].reason = session.reason
            break
    outcome.accepts_all = accepts_all
    return outcome


def _any_accepted(session: smtp_mod.SessionResult) -> bool:
    return any(smtp_mod.classify_reply(rep.code, rep.message)[0] == smtp_mod.ACCEPTED
               for rep in session.replies.values())


def _apply_reply(r: CheckResult, session: smtp_mod.SessionResult,
                 probe_accepts_all: bool | None, known_accepts_all: bool | None) -> None:
    """Set status/reason of one address from the session's answers."""
    reply = session.replies.get(r.email)
    if reply is None:
        if probe_accepts_all:  # we skipped asking: the server accepts everything
            r.status, r.reason = "catch_all", REASON_CATCH_ALL
            r.smtp_code = session.probe.code if session.probe else None
        else:
            r.reason = session.reason or "Mail server stopped answering"
        return
    r.smtp_code = reply.code
    category, reason = smtp_mod.classify_reply(reply.code, reply.message)
    if category == smtp_mod.ACCEPTED:
        if known_accepts_all is False:
            r.status, r.reason = "valid", REASON_VALID
        elif known_accepts_all:
            r.status, r.reason = "catch_all", REASON_CATCH_ALL
        else:
            r.reason = REASON_MAYBE_CATCH_ALL
    elif category == smtp_mod.REJECTED:
        r.status, r.reason = "invalid", reason
        if reason == smtp_mod.REASON_MS_NO_USER and not _any_accepted(session):
            # Microsoft said "Access denied" to every address, so we have no proof it would
            # accept a real one: probably doesn't exist, but don't throw the lead away.
            r.status, r.reason = "risky", REASON_MS_UNSURE
    else:
        r.reason = reason


def _final_touches(r: CheckResult) -> CheckResult:
    """Disposable and un-checkable free-mail addresses are 'risky' (unless invalid)."""
    if r.status == "invalid":
        return r
    if r.is_disposable:
        r.status, r.reason = "risky", REASON_DISPOSABLE
    elif r.is_free and r.reason == REASON_UNCHECKABLE:
        r.status = "risky"
    return r


# ----- database cache ---------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    """SQLite gives back naive datetimes; they are UTC."""
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_cached(emails: list[str], use_smtp: bool) -> dict[str, CheckResult]:
    now = datetime.now(timezone.utc)
    max_age = timedelta(days=config.EMAIL_CHECK_MAX_AGE_DAYS)
    found = {}
    with SessionLocal() as session:
        for chunk in _chunks(emails, 500):
            for row in session.scalars(select(EmailCheck).where(EmailCheck.email.in_(chunk))):
                age = now - (_aware(row.checked_at) or now - max_age * 2)
                if age > max_age or (row.status == "unknown" and age > UNKNOWN_MAX_AGE):
                    continue
                if use_smtp and row.reason == REASON_NOT_CHECKED:
                    continue  # SMTP is on now: check the mailbox this time
                if row.status == "unknown" and row.reason in RETRY_REASONS:
                    continue  # our network had a problem last time: try again
                found[row.email] = CheckResult(
                    row.email, row.status, row.reason, bool(row.is_role), bool(row.is_free),
                    bool(row.is_disposable), row.mx_found, row.smtp_code,
                )
    return found


def _load_domain_facts(domains: list[str], force: bool) -> dict[str, _DomainFacts]:
    now = datetime.now(timezone.utc)
    max_age = timedelta(days=config.EMAIL_CHECK_MAX_AGE_DAYS)
    facts = {d: _DomainFacts() for d in domains}
    with SessionLocal() as session:
        for chunk in _chunks(domains, 500):
            for row in session.scalars(select(DomainInfo).where(DomainInfo.domain.in_(chunk))):
                checked = _aware(row.checked_at)
                fresh = checked is not None and now - checked <= max_age
                f = facts[row.domain]
                # Only re-use good news; "no mail server" is looked up again (cheap, and may be fixed).
                f.mx_fresh = bool(fresh and not force and row.has_mx and row.mx_hosts)
                f.mx_hosts = list(row.mx_hosts or [])
                f.accepts_all = row.accepts_all if fresh else None
                f.accepts_all_fresh = bool(fresh and not force and row.accepts_all is not None)
    return facts


def _save(outcomes: dict[str, _DomainOutcome]) -> None:
    for attempt in range(2):  # retry once if another thread inserted the same row meanwhile
        try:
            with SessionLocal() as session:
                now = datetime.now(timezone.utc)
                for domain, outcome in outcomes.items():
                    if outcome.mx is None and outcome.accepts_all is None:
                        pass
                    elif outcome.mx is None or outcome.mx.state != "error":
                        info = session.scalar(select(DomainInfo).where(DomainInfo.domain == domain))
                        if info is None:
                            info = DomainInfo(domain=domain)
                            session.add(info)
                        if outcome.mx is not None:
                            info.mx_hosts = outcome.mx.hosts
                            info.has_mx = outcome.mx.state == "ok"
                            info.checked_at = now
                        if outcome.accepts_all is not None:
                            info.accepts_all = outcome.accepts_all
                    for r in outcome.results.values():
                        row = session.scalar(select(EmailCheck).where(EmailCheck.email == r.email))
                        if row is None:
                            row = EmailCheck(email=r.email)
                            session.add(row)
                        row.status, row.reason = r.status, r.reason
                        row.is_role, row.is_free, row.is_disposable = r.is_role, r.is_free, r.is_disposable
                        row.mx_found, row.smtp_code, row.checked_at = r.mx_found, r.smtp_code, now
                session.commit()
            return
        except IntegrityError:
            if attempt:
                log.warning("Could not save email checks (duplicate rows)", exc_info=True)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ----- main entry point -------------------------------------------------------------

def verify_emails(
    emails: list[str],
    *,
    force: bool = False,
    use_smtp: bool | None = None,
    resolve: Resolver | None = None,
    smtp_factory: smtp_mod.SmtpFactory | None = None,
) -> dict[str, CheckResult]:
    """Check each address. Returns {address as given: CheckResult}; CheckResult.email is the
    cleaned-up (lowercase) address. Never raises for network problems.

    force=True ignores cached results. use_smtp=None means config.SMTP_VERIFY.
    `resolve` / `smtp_factory` replace the real DNS / SMTP (tests)."""
    use_smtp = config.SMTP_VERIFY if use_smtp is None else use_smtp
    results: dict[str, CheckResult] = {}
    wanted: dict[str, list[str]] = defaultdict(list)  # normalized -> inputs that map to it
    for raw in emails:
        if raw in results:
            continue
        normalized, reason = check_syntax(raw)
        if normalized is None:
            results[raw] = CheckResult(str(raw or "").strip().lower(), "invalid", reason)
        else:
            wanted[normalized].append(raw)

    known = {} if force else _load_cached(list(wanted), use_smtp)
    by_domain: dict[str, list[str]] = defaultdict(list)
    for email in wanted:
        if email not in known:
            by_domain[split_email(email)[1]].append(email)

    outcomes: dict[str, _DomainOutcome] = {}
    if by_domain:
        facts = _load_domain_facts(list(by_domain), force)
        workers = min(MAX_PARALLEL_DOMAINS, len(by_domain))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="email-check") as pool:
            futures = {
                d: pool.submit(_check_domain, d, addrs, facts[d], use_smtp, resolve, smtp_factory)
                for d, addrs in by_domain.items()
            }
            for domain, future in futures.items():
                try:
                    outcomes[domain] = future.result()
                except Exception:  # noqa: BLE001 - a bug for one domain must not lose the rest
                    log.exception("Email check failed for domain %s", domain)
                    outcomes[domain] = _DomainOutcome(results={
                        a: CheckResult(a, "unknown", "Check failed unexpectedly", is_role(a), is_free(a),
                                       is_disposable(a))
                        for a in by_domain[domain]
                    })
        for outcome in outcomes.values():
            for r in outcome.results.values():
                _final_touches(r)
        _save(outcomes)
        for outcome in outcomes.values():
            known.update(outcome.results)

    for email, inputs in wanted.items():
        for raw in inputs:
            results[raw] = known[email]
    return results
