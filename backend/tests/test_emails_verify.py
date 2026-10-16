"""Email verification: DNS (MX), SMTP answers, catch-all, batching, cache.
No real network: DNS and SMTP are replaced by the fakes below."""
import smtplib
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import dns.exception
import dns.resolver
import pytest
from sqlalchemy import select

from app import config
from app.emails import smtp as smtp_mod
from app.emails import verify as verify_mod
from app.emails import verify_emails
from app.emails.dns import lookup_mx
from app.models import DomainInfo, EmailCheck

# ----- fakes -------------------------------------------------------------------


def make_resolver(records: dict):
    """records: domain -> {"MX": [(10, "mx.host")], "A": ["1.2.3.4"]} or an exception.
    Unknown domains raise NXDOMAIN; a missing record type raises NoAnswer."""
    calls = []

    def resolve(name, rdtype):
        calls.append((name, rdtype))
        entry = records.get(name)
        if entry is None:
            raise dns.resolver.NXDOMAIN()
        if isinstance(entry, Exception):
            raise entry
        value = entry.get(rdtype)
        if value is None:
            raise dns.resolver.NoAnswer()
        if isinstance(value, Exception):
            raise value
        return value

    resolve.calls = calls
    return resolve


class FakeServer:
    """A pretend mail server. `answers` maps address -> (code, message);
    other addresses get `default`; the made-up catch-all probe (zz-...) gets `probe`."""

    def __init__(self, answers=None, default=(550, "5.1.1 User unknown"), probe=None,
                 mail=(250, "2.1.0 Ok"), ehlo=(250, "mx.test Hello"), connect_error=None):
        self.answers = {k.lower(): v for k, v in (answers or {}).items()}
        self.default = default
        self.probe = probe or default
        self.mail_reply = mail
        self.ehlo_reply = ehlo
        self.connect_error = connect_error
        self.connects = []  # mx host of every connection attempt
        self.sessions = []  # list of RCPT addresses, one list per session
        self.quits = 0
        self.helo_hosts = []

    def factory(self, host, timeout, helo_host):
        self.connects.append(host)
        if self.connect_error is not None:
            raise self.connect_error
        self.helo_hosts.append(helo_host)
        session = []  # each connection records its own RCPTs (domains run in parallel threads)
        self.sessions.append(session)
        return FakeClient(self, session)

    @property
    def rcpts(self):
        return [a for s in self.sessions for a in s]


class FakeClient:
    def __init__(self, server: FakeServer, session: list):
        self.server = server
        self.session = session

    def ehlo(self, name=""):
        code, msg = self.server.ehlo_reply
        return code, msg.encode()

    def helo(self, name=""):
        return self.ehlo(name)

    def mail(self, sender):
        code, msg = self.server.mail_reply
        return code, msg.encode()

    def rcpt(self, address):
        self.session.append(address)
        if address.startswith("zz-"):
            code, msg = self.server.probe
        else:
            code, msg = self.server.answers.get(address, self.server.default)
        if isinstance(code, Exception):
            raise code
        return code, msg.encode()

    def quit(self):
        self.server.quits += 1
        return 221, b"Bye"

    def close(self):
        pass


ACME_DNS = {"acme.com": {"MX": [(20, "mx2.acme.com."), (10, "mx1.acme.com.")]}}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """No remembered blocks and a fixed HELO name for every test.
    Tests that verify addresses also take the `db` fixture (a fresh, empty database)."""
    verify_mod.reset_smtp_state()
    monkeypatch.setattr(config, "SMTP_HELO_HOST", "checker.leadgen.test")
    monkeypatch.setattr(config, "SMTP_MAIL_FROM", "")
    monkeypatch.setattr(config, "SMTP_VERIFY", True)
    yield
    verify_mod.reset_smtp_state()


def run(emails, server=None, records=ACME_DNS, **kwargs):
    resolver = make_resolver(records)
    server = server or FakeServer()
    return verify_emails(emails, resolve=resolver, smtp_factory=server.factory, **kwargs), server, resolver


# ----- DNS -----------------------------------------------------------------------

def test_mx_sorted_by_preference():
    result = lookup_mx("acme.com", make_resolver(ACME_DNS))
    assert result.state == "ok"
    assert result.hosts == ["mx1.acme.com", "mx2.acme.com"]


def test_mx_falls_back_to_domain_address():
    result = lookup_mx("tiny.com", make_resolver({"tiny.com": {"A": ["1.2.3.4"]}}))
    assert (result.state, result.hosts) == ("ok", ["tiny.com"])


def test_mx_states():
    resolver = make_resolver({
        "nomail.com": {},  # exists, no MX, no A/AAAA
        "nullmx.com": {"MX": [(0, ".")]},
        "slow.com": dns.exception.Timeout(),
        "broken.com": dns.resolver.NoNameservers(),
    })
    assert lookup_mx("gone.com", resolver).state == "no_domain"
    assert lookup_mx("gone.com", resolver).reason == "Domain does not exist"
    assert lookup_mx("nomail.com", resolver).state == "no_mail"
    assert lookup_mx("nomail.com", resolver).reason == "Domain can't receive email"
    assert lookup_mx("nullmx.com", resolver).state == "no_mail"
    assert lookup_mx("slow.com", resolver).state == "error"
    assert lookup_mx("broken.com", resolver).state == "error"


# ----- SMTP answers -----------------------------------------------------------------

@pytest.mark.parametrize("code, message, category", [
    (250, "2.1.5 Ok", smtp_mod.ACCEPTED),
    (251, "User not local; will forward", smtp_mod.ACCEPTED),
    (252, "Cannot VRFY user", smtp_mod.UNCLEAR),
    # mailbox doesn't exist
    (550, "5.1.1 <x@acme.com>: Recipient address rejected: User unknown in virtual mailbox table",
     smtp_mod.REJECTED),
    (550, "5.1.1 The email account that you tried to reach does not exist. Please try "
          "double-checking the recipient's email address for typos https://support.google.com/mail/?p=NoSuchUser",
     smtp_mod.REJECTED),
    (550, "5.4.1 Recipient address rejected: Access denied. For more information see "
          "https://aka.ms/EXOSmtpErrors [DB5PEPF00014B9A.eurprd02.prod.outlook.com]", smtp_mod.REJECTED),
    (550, "5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup",
     smtp_mod.REJECTED),
    (551, "User not local", smtp_mod.REJECTED),
    (553, "5.1.3 mailbox name not allowed", smtp_mod.REJECTED),
    (553, "sorry, that domain isn't in my list of allowed rcpthosts", smtp_mod.UNCLEAR),
    (550, "No such user here", smtp_mod.REJECTED),
    (550, "Mailbox unavailable", smtp_mod.REJECTED),
    (550, "User not listed in public Name & Address Book", smtp_mod.REJECTED),
    (554, "delivery error: dd This user doesn't have a yahoo.com account", smtp_mod.REJECTED),
    (550, "5.2.1 The email account that you tried to reach is disabled", smtp_mod.REJECTED),
    (552, "5.2.2 Mailbox full", smtp_mod.UNCLEAR),
    (554, "5.7.1 <x@acme.com>: Relay access denied", smtp_mod.UNCLEAR),
    # greylisting / try later
    (450, "4.2.0 <x@acme.com>: Recipient address rejected: Greylisted, see http://postgrey", smtp_mod.TEMPORARY),
    (451, "4.7.1 Please try again later", smtp_mod.TEMPORARY),
    (452, "4.5.3 Too many recipients", smtp_mod.TEMPORARY),
    (421, "4.3.2 Service not available", smtp_mod.TEMPORARY),
    # the server refuses US (our IP / host), says nothing about the mailbox
    (554, "5.7.1 Service unavailable; Client host [203.0.113.5] blocked using zen.spamhaus.org; "
          "https://www.spamhaus.org/query/ip/203.0.113.5", smtp_mod.BLOCKED),
    (550, "5.7.1 [203.0.113.5] The IP you're using to send mail is not authorized to send email "
          "directly to our servers. Please use the SMTP relay at your service provider instead.", smtp_mod.BLOCKED),
    (550, "5.7.606 Access denied, banned sending IP [203.0.113.5]. To request removal from this list "
          "please visit https://sender.office.com/", smtp_mod.BLOCKED),
    (550, "5.7.1 Unfortunately, messages from [203.0.113.5] weren't sent. Please contact your Internet "
          "service provider since part of their network is on our block list (S3150).", smtp_mod.BLOCKED),
    (421, "4.7.0 [203.0.113.5] Our system has detected an unusual rate of unsolicited mail "
          "originating from your IP address.", smtp_mod.BLOCKED),
    (550, "5.7.1 Client host rejected: cannot find your reverse hostname, [203.0.113.5]", smtp_mod.BLOCKED),
    (554, "5.7.1 Your IP 203.0.113.5 is listed in the PBL", smtp_mod.BLOCKED),
    (550, "5.7.1 Helo command rejected: need fully-qualified hostname", smtp_mod.BLOCKED),
    (550, "5.7.1 Message rejected due to policy", smtp_mod.UNCLEAR),
    (None, "", smtp_mod.UNCLEAR),
])
def test_classify_reply(code, message, category):
    assert smtp_mod.classify_reply(code, message)[0] == category


# ----- full verification ----------------------------------------------------------

def test_valid_when_server_checks_mailboxes(db):
    server = FakeServer(answers={"john@acme.com": (250, "2.1.5 Ok")}, probe=(550, "5.1.1 User unknown"))
    results, server, _ = run(["John@Acme.com", "nobody@acme.com"], server)
    john = results["John@Acme.com"]  # keyed by the address as given
    assert john.email == "john@acme.com"
    assert (john.status, john.reason, john.smtp_code, john.mx_found) == ("valid", "Mailbox exists", 250, True)
    assert results["nobody@acme.com"].status == "invalid"
    assert results["nobody@acme.com"].reason == "Mailbox does not exist"
    assert server.connects == ["mx1.acme.com"]  # best MX first
    assert server.helo_hosts == ["checker.leadgen.test"]
    # one session: probe first, then both addresses
    assert len(server.sessions) == 1
    assert server.sessions[0][0].startswith("zz-") and server.sessions[0][0].endswith("@acme.com")
    assert server.sessions[0][1:] == ["john@acme.com", "nobody@acme.com"]
    assert server.quits == 1


def test_catch_all_domain(db):
    server = FakeServer(default=(250, "OK"), probe=(250, "OK"))
    results, server, _ = run(["john@acme.com", "info@acme.com"], server)
    assert {r.status for r in results.values()} == {"catch_all"}
    assert results["info@acme.com"].is_role is True
    assert server.sessions[0][0].startswith("zz-")
    assert len(server.sessions[0]) == 1  # no need to ask about each address
    with verify_mod.SessionLocal() as s:
        info = s.scalar(select(DomainInfo).where(DomainInfo.domain == "acme.com"))
    assert info.accepts_all is True
    assert info.has_mx is True
    assert info.mx_hosts == ["mx1.acme.com", "mx2.acme.com"]


def test_accepted_but_catch_all_unknown_is_unknown(db):
    server = FakeServer(answers={"john@acme.com": (250, "OK")}, probe=(451, "4.7.1 Greylisted, try later"))
    results, _, _ = run(["john@acme.com"], server)
    assert results["john@acme.com"].status == "unknown"
    assert "may accept every address" in results["john@acme.com"].reason


def test_microsoft_365_unknown_recipient_is_invalid(db):
    rejected = (550, "5.4.1 Recipient address rejected: Access denied. AS(201806281)")
    server = FakeServer(answers={"jane@acme.com": (250, "2.1.5 Recipient OK")}, default=rejected)
    records = {"acme.com": {"MX": [(0, "acme-com.mail.protection.outlook.com")]}}
    results, _, _ = run(["jane@acme.com", "ghost@acme.com"], server, records)
    assert results["jane@acme.com"].status == "valid"
    assert results["ghost@acme.com"].status == "invalid"
    assert results["ghost@acme.com"].reason == "Mailbox does not exist (Microsoft 365)"


def test_microsoft_365_rejecting_everything_is_only_risky(db):
    # No address was accepted, so "Access denied" might mean "we refuse you", not "no such user".
    rejected = (550, "5.4.1 Recipient address rejected: Access denied. For more information see "
                     "https://aka.ms/EXOSmtpErrors [MN7PEPF00000150.namprd04.prod.outlook.com]")
    records = {"acme.com": {"MX": [(0, "acme-com.mail.protection.outlook.com")]}}
    results, _, _ = run(["info@acme.com"], FakeServer(default=rejected), records)
    r = results["info@acme.com"]
    assert (r.status, r.smtp_code) == ("risky", 550)
    assert r.reason == "Microsoft 365 rejected it; the mailbox probably doesn't exist"


def test_greylisting_is_unknown(db):
    server = FakeServer(default=(450, "4.2.0 Greylisted, please try again later"))
    results, _, _ = run(["john@acme.com"], server)
    r = results["john@acme.com"]
    assert (r.status, r.smtp_code) == ("unknown", 450)
    assert "greylisting" in r.reason


def test_blocked_ip_is_unknown_and_provider_is_left_alone(db):
    blocked = (554, "5.7.1 Service unavailable; Client host [203.0.113.5] blocked using zen.spamhaus.org")
    server = FakeServer(default=blocked, probe=blocked)
    results, server, _ = run(["john@acme.com"], server)
    assert results["john@acme.com"].status == "unknown"
    assert results["john@acme.com"].reason == "Mail server refused the check (our IP is blocked)"
    assert len(server.sessions) == 1

    # Another domain on the same provider (acme.com) is not contacted again for a while.
    records = {"other.com": {"MX": [(10, "mx9.acme.com")]}}
    results2, server2, _ = run(["amy@other.com"], FakeServer(), records)
    assert results2["amy@other.com"].reason == "Mail server refused the check (our IP is blocked)"
    assert server2.connects == []


def test_blocked_at_greeting(db):
    error = smtplib.SMTPConnectError(554, b"5.7.1 Client host [203.0.113.5] blocked using Spamhaus")
    server = FakeServer(connect_error=error)
    results, server, _ = run(["john@acme.com"], server)
    assert results["john@acme.com"].status == "unknown"
    assert "blocked" in results["john@acme.com"].reason
    assert server.connects == ["mx1.acme.com"]  # no point trying the backup server


def test_connection_failure_tries_next_mx_then_pauses_smtp(db):
    server = FakeServer(connect_error=TimeoutError("timed out"))
    records = {f"d{i}.com": {"MX": [(10, f"mx1.d{i}.com"), (20, f"mx2.d{i}.com")]} for i in range(5)}
    results, server, _ = run(["a@d0.com"], server, records)
    assert server.connects == ["mx1.d0.com", "mx2.d0.com"]
    assert results["a@d0.com"].status == "unknown"
    assert "port 25" in results["a@d0.com"].reason

    run(["a@d1.com"], server, records)
    run(["a@d2.com"], server, records)  # third domain in a row -> SMTP paused
    before = len(server.connects)
    results, server, _ = run(["a@d3.com"], server, records)
    assert len(server.connects) == before
    assert results["a@d3.com"].reason == verify_mod.REASON_PORT25


def test_smtp_off_gives_domain_only_result(db):
    results, server, _ = run(["john@acme.com", "x@gone.com"], use_smtp=False)
    assert results["john@acme.com"].status == "unknown"
    assert results["john@acme.com"].reason == "Domain OK; mailbox not checked"
    assert results["john@acme.com"].mx_found is True
    assert results["x@gone.com"].status == "invalid"
    assert results["x@gone.com"].reason == "Domain does not exist"
    assert results["x@gone.com"].mx_found is False
    assert server.connects == []


def test_smtp_verify_setting_is_the_default(db, monkeypatch):
    monkeypatch.setattr(config, "SMTP_VERIFY", False)
    results, server, _ = run(["john@acme.com"])
    assert results["john@acme.com"].reason == "Domain OK; mailbox not checked"
    assert server.connects == []


def test_dns_timeout_is_unknown(db):
    results, server, _ = run(["john@slow.com"], records={"slow.com": dns.exception.Timeout()})
    r = results["john@slow.com"]
    assert (r.status, r.mx_found) == ("unknown", None)
    assert "DNS" in r.reason
    assert server.connects == []


def test_bad_syntax_disposable_and_yahoo(db):
    records = {
        "mailinator.com": {"MX": [(10, "mail.mailinator.com")]},
        "yahoo.com": {"MX": [(1, "mta5.am0.yahoodns.net")]},
    }
    results, server, _ = run(["not an email", "x@mailinator.com", "someone@yahoo.com"], records=records)
    assert results["not an email"].status == "invalid"
    assert results["not an email"].reason == "Not a valid email address"
    assert results["x@mailinator.com"].status == "risky"
    assert results["x@mailinator.com"].is_disposable is True
    assert results["someone@yahoo.com"].status == "risky"
    assert results["someone@yahoo.com"].is_free is True
    assert "Yahoo" in results["someone@yahoo.com"].reason
    assert server.connects == []  # neither is worth an SMTP session


def test_batches_one_session_per_domain_and_max_20_per_session(db):
    emails = [f"user{i}@acme.com" for i in range(25)]
    records = {**ACME_DNS, "beta.com": {"MX": [(10, "mx.beta.com")]}}
    server = FakeServer(default=(250, "OK"), probe=(550, "5.1.1 no such user"))
    results, server, resolver = run(emails + ["amy@beta.com"], server, records)
    assert all(r.status == "valid" for r in results.values())
    acme_sessions = [s for s in server.sessions if s[-1].endswith("@acme.com")]
    assert [len(s) for s in acme_sessions] == [20, 6]  # probe + 19, then the 6 left (no second probe)
    assert len([s for s in server.sessions if s[-1].endswith("@beta.com")]) == 1
    assert len(server.sessions) == 3
    assert [c for c in resolver.calls if c[0] == "acme.com"] == [("acme.com", "MX")]  # one DNS lookup


def test_results_are_cached(db):
    server = FakeServer(answers={"john@acme.com": (250, "OK")})
    run(["john@acme.com"], server)
    with verify_mod.SessionLocal() as s:
        row = s.scalar(select(EmailCheck).where(EmailCheck.email == "john@acme.com"))
    assert (row.status, row.smtp_code) == ("valid", 250)

    results, server2, resolver2 = run(["john@acme.com"])
    assert results["john@acme.com"].status == "valid"
    assert server2.connects == [] and resolver2.calls == []  # straight from the cache


def test_force_and_expired_cache_check_again(db):
    server = FakeServer(answers={"john@acme.com": (250, "OK")})
    run(["john@acme.com"], server)

    _, server2, _ = run(["john@acme.com"], FakeServer(answers={"john@acme.com": (250, "OK")}), force=True)
    assert len(server2.sessions) == 1

    old = datetime.now(timezone.utc) - timedelta(days=config.EMAIL_CHECK_MAX_AGE_DAYS + 1)
    with verify_mod.SessionLocal() as s:
        s.scalar(select(EmailCheck).where(EmailCheck.email == "john@acme.com")).checked_at = old
        s.commit()
    _, server3, resolver3 = run(["john@acme.com"], FakeServer(answers={"john@acme.com": (250, "OK")}))
    assert len(server3.sessions) == 1
    assert resolver3.calls == []  # the domain's mail servers are still fresh in the cache


def test_unchecked_result_is_rechecked_once_smtp_is_on(db):
    run(["john@acme.com"], use_smtp=False)
    results, server, _ = run(["john@acme.com"], FakeServer(answers={"john@acme.com": (250, "OK")}))
    assert results["john@acme.com"].status == "valid"
    assert len(server.sessions) == 1


def test_unknown_results_expire_after_a_day(db):
    run(["john@acme.com"], FakeServer(default=(451, "4.7.1 try later")))
    with verify_mod.SessionLocal() as s:
        s.scalar(select(EmailCheck).where(EmailCheck.email == "john@acme.com")).checked_at = (
            datetime.now(timezone.utc) - timedelta(hours=25))
        s.commit()
    results, server, _ = run(["john@acme.com"], FakeServer(answers={"john@acme.com": (250, "OK")}))
    assert results["john@acme.com"].status == "valid"


def test_server_hangs_up_midway(db):
    server = FakeServer(answers={"a@acme.com": (250, "OK"),
                                 "b@acme.com": (smtplib.SMTPServerDisconnected("bye"), "")})
    results, _, _ = run(["a@acme.com", "b@acme.com", "c@acme.com"], server)
    assert results["a@acme.com"].status == "valid"
    assert results["b@acme.com"].status == "unknown"
    assert results["c@acme.com"].status == "unknown"


def test_sender_refused(db):
    server = FakeServer(mail=(553, "5.1.8 Sender address rejected: Domain not found"))
    results, _, _ = run(["john@acme.com"], server)
    assert results["john@acme.com"].status == "unknown"
    assert "SMTP_MAIL_FROM" in results["john@acme.com"].reason


def test_provider_key():
    assert verify_mod.provider_key("acme-com.mail.protection.outlook.com") == "outlook.com"
    assert verify_mod.provider_key("aspmx.l.google.com") == "google.com"
    assert verify_mod.provider_key("mx.acme.co.uk") == "acme.co.uk"


def test_known_catch_all_domain_is_not_asked_again(db):
    run(["a@acme.com"], FakeServer(default=(250, "OK"), probe=(250, "OK")))
    results, server, _ = run(["b@acme.com"], FakeServer())
    assert results["b@acme.com"].status == "catch_all"
    assert server.connects == []  # we already know the answer
    _, server2, _ = run(["c@acme.com"], FakeServer(default=(250, "OK"), probe=(250, "OK")), force=True)
    assert len(server2.sessions) == 1  # force asks again


def test_known_normal_domain_skips_the_probe(db):
    run(["a@acme.com"], FakeServer(answers={"a@acme.com": (250, "OK")}))
    results, server, _ = run(["b@acme.com"], FakeServer(answers={"b@acme.com": (250, "OK")}))
    assert results["b@acme.com"].status == "valid"
    assert server.sessions == [["b@acme.com"]]  # no made-up zz-... address this time


def test_problems_on_our_side_are_not_cached(db):
    first, _, _ = run(["a@acme.com"], FakeServer(connect_error=TimeoutError("timed out")))
    assert first["a@acme.com"].status == "unknown"
    results, server, _ = run(["a@acme.com"], FakeServer(answers={"a@acme.com": (250, "OK")}))
    assert results["a@acme.com"].status == "valid"
    assert len(server.sessions) == 1


def test_one_session_at_a_time_per_mail_provider():
    a = verify_mod._host_lock("acme-com.mail.protection.outlook.com")
    b = verify_mod._host_lock("beta-com.mail.protection.outlook.com")
    assert a is b  # both Microsoft 365
    assert verify_mod._host_lock("mx.acme.com") is not a


def test_private_mail_server_is_never_contacted(monkeypatch):
    monkeypatch.setattr(smtp_mod.socket, "getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 25))])
    result = smtp_mod.check_mailboxes(["mx.evil.test"], ["a@evil.test"], helo_host="h.test",
                                      mail_from="v@h.test", timeout=1)
    assert (result.failure, result.reason) == ("refused", smtp_mod.REASON_PRIVATE)


def test_smtp_session_has_a_total_time_limit(monkeypatch):
    # A local "tarpit" that keeps sending greeting lines and never finishes (loopback only).
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def drip():
        conn, _ = server.accept()
        try:
            while not stop.is_set():
                conn.sendall(b"220-still greeting you\r\n")
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=drip, daemon=True).start()
    monkeypatch.setattr(smtp_mod, "SMTP_PORT", server.getsockname()[1])
    start = time.monotonic()
    try:
        with pytest.raises((OSError, smtplib.SMTPException)):
            smtp_mod._TimedSMTP("127.0.0.1", "h.test", timeout=1.0, time_limit=1.5)
        assert time.monotonic() - start < 6
    finally:
        stop.set()
        server.close()
