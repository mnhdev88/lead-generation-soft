"""Ask a domain's mail server whether mailboxes exist - WITHOUT sending any email.

One short conversation ("session") per domain, on port 25:

    EHLO <our host name>
    MAIL FROM:<verify@our host>
    RCPT TO:<zz-8f3a1c0b9d2e@acme.com>   <- made-up address: is this a "catch-all" server?
    RCPT TO:<john@acme.com>              <- the addresses we want to check (max ~20)
    RCPT TO:<jane@acme.com>
    QUIT                                 <- we never send DATA, so nothing is delivered

The server answers each RCPT with a code: 250 = "I'll take it", 550 = "no such user",
4xx = "try later" (greylisting) and so on. classify_reply() turns code + message into a
simple category. Servers also refuse to talk to IP addresses on spam block lists (home
internet connections usually are) - that is "blocked", not "mailbox doesn't exist".

The connection is made by `factory(host, timeout, helo_host)`, which tests replace with a fake.
"""
import logging
import re
import smtplib
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..enrich.crawler import is_public_ip

log = logging.getLogger("emails.smtp")

SMTP_PORT = 25
MAX_RCPT_PER_SESSION = 20  # including the catch-all probe
MAX_MX_TRIED = 2  # try the next mail server only when the first can't be reached
# A whole session (connect + all questions) may take at most this many times the timeout.
SESSION_TIME_LIMIT_FACTOR = 6

# Categories returned by classify_reply()
ACCEPTED = "accepted"  # the server would deliver to this address
REJECTED = "rejected"  # the mailbox does not exist (or is disabled)
TEMPORARY = "temporary"  # try later (greylisting, too busy)
BLOCKED = "blocked"  # the server refuses to talk to US (our IP / host name), says nothing about the mailbox
UNCLEAR = "unclear"  # anything else

REASON_BLOCKED = "Mail server refused the check (our IP is blocked)"
REASON_MS_NO_USER = "Mailbox does not exist (Microsoft 365)"
REASON_CONNECT = "Couldn't connect to the mail server (port 25 may be blocked on this network)"
REASON_PRIVATE = "Mail server is on a private network"

# factory(mx_host, timeout_seconds, helo_host) -> a connected smtplib.SMTP-like object
SmtpFactory = Callable[[str, float, str], Any]


class PrivateMailServer(OSError):
    """The mail server's address is private or local (127.0.0.1, 10.x...): never contacted."""


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)  # wakes up a read that is waiting on this socket
    except OSError:
        pass  # already closed


class _TimedSMTP(smtplib.SMTP):
    """smtplib.SMTP with a deadline for the WHOLE conversation.

    smtplib's own timeout starts again with every byte received, so a server that sends a
    line every few seconds ("220-still here...") could keep us waiting forever. When the
    deadline passes, the socket is shut down and the next read fails."""

    def __init__(self, ip: str, helo_host: str, timeout: float, time_limit: float):
        self._time_limit = time_limit
        self._timer: threading.Timer | None = None
        super().__init__(ip, SMTP_PORT, local_hostname=helo_host, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        sock = super()._get_socket(host, port, timeout)
        self._timer = threading.Timer(self._time_limit, _shutdown, args=(sock,))
        self._timer.daemon = True
        self._timer.start()
        return sock

    def close(self):
        if self._timer is not None:
            self._timer.cancel()
        super().close()


def _public_address(host: str) -> str:
    """The mail server's IP address. Raises PrivateMailServer when it points to a private or
    local network (same rule as the website crawler), OSError when it can't be found."""
    ips = [info[4][0] for info in socket.getaddrinfo(host, SMTP_PORT, type=socket.SOCK_STREAM)]
    if not ips:
        raise OSError(f"No address for {host}")
    if not all(is_public_ip(ip) for ip in ips):
        raise PrivateMailServer(f"{host} points to a private network")
    return next((ip for ip in ips if ":" not in ip), ips[0])  # IPv4 first: not every network has IPv6


def default_factory(host: str, timeout: float, helo_host: str) -> smtplib.SMTP:
    # We connect to the exact address we just checked, so DNS can't switch it to a private one.
    return _TimedSMTP(_public_address(host), helo_host, timeout, time_limit=timeout * SESSION_TIME_LIMIT_FACTOR)


# ----- understanding the server's answers -------------------------------------

# The server refuses US: block lists, bad reputation, home IP, missing reverse DNS, bad HELO.
_BLOCK_RE = re.compile(
    r"spamhaus|spamcop|barracuda|sorbs|dnsbl|\brbl\b|black ?list|block ?list|blocked using|"
    r"client host|\bpbl\b|banned sending ip|banned sender|"
    r"5\.7\.(?:5[01]\d|6\d\d|7\d\d)|"  # Microsoft: 5.7.511 banned sender, 5.7.6xx/7xx banned IP / tenant
    r"not allowed to send|not authori[sz]ed to send|reputation|unsolicited|"
    r"your ip|sending ip|ip address .{0,40}(?:blocked|listed|banned)|"
    r"\b(?:is|are|been|currently) (?:black)?listed\b|"
    r"dynamic ip|dynamic address|residential|dial-?up|"
    r"reverse dns|\brdns\b|ptr record|no ptr|reverse lookup|"
    r"helo command rejected|invalid helo|bad helo|fully[- ]qualified|"
    r"rejected by policy|policy rejection|access denied, (?:banned|traffic)"
)
# Microsoft 365 with "Directory-Based Edge Blocking": unknown recipients get
# "550 5.4.1 Recipient address rejected: Access denied. AS(201806281)".
_MS_NO_USER_RE = re.compile(r"5\.4\.1 .*recipient address rejected: access denied|as\(201806281\)")
# Bad destination mailbox (5.1.x; but 5.1.7 / 5.1.8 are about OUR sender address).
_NO_USER_CODE_RE = re.compile(r"\b5\.1\.(?:[0-6]|9|10)\b")
_NO_USER_TEXT_RE = re.compile(
    r"user unknown|unknown user|no such user|no such (?:mailbox|recipient|address|account)|"
    r"does ?n[o']t exist|not exist|doesn't have an? .{0,30}account|mailbox (?:is )?unavailable|"
    r"mailbox not found|mailbox does not|recipient not found|unknown recipient|invalid recipient|"
    r"recipient (?:address )?(?:is )?invalid|recipient unknown|address unknown|user not found|"
    r"no mailbox|not a valid (?:mailbox|recipient|user)|unrouteable address|undeliverable address|"
    r"account (?:is )?(?:disabled|inactive|suspended|closed)|mailbox (?:is )?(?:disabled|inactive)|"
    r"no longer (?:active|available|with)|not listed in|invalid mailbox|bad destination mailbox|"
    r"recip(?:ient)?notfound|not found in (?:the )?directory"
)
_DISABLED_RE = re.compile(r"\b5\.2\.1\b|(?:account|mailbox|user) (?:has been |is )?(?:disabled|inactive|suspended)"
                          r"|is disabled")
_FULL_RE = re.compile(r"\b5\.2\.2\b|mailbox (?:is )?full|over ?quota|quota exceeded|insufficient storage")
_RELAY_RE = re.compile(r"relay(?:ing)? (?:access )?(?:denied|not permitted|prohibited)|not permitted to relay|"
                       r"allowed rcpthosts")
_POLICY_RE = re.compile(r"\b5\.7\.\d+\b|access denied")
_GREYLIST_RE = re.compile(r"greylist|graylist|try (?:again )?later|temporar")


def classify_reply(code: int | None, message: str | None) -> tuple[str, str]:
    """Turn one SMTP answer into (category, short plain-English reason)."""
    msg = (message or "").lower()
    if code is None:
        return UNCLEAR, "No answer from the mail server"
    if code in (250, 251):
        return ACCEPTED, "Mailbox exists"
    if code == 252:
        return UNCLEAR, "Mail server won't say if the mailbox exists"
    if _BLOCK_RE.search(msg):
        return BLOCKED, REASON_BLOCKED
    if 400 <= code < 500:
        if code == 452 and "too many" in msg:
            return TEMPORARY, "Mail server is limiting checks; try later"
        if code == 452 or _FULL_RE.search(msg):
            return TEMPORARY, "Mailbox is full or the server is busy; try later"
        if code in (450, 451) or _GREYLIST_RE.search(msg):
            return TEMPORARY, "Mail server asked us to try later (greylisting)"
        return TEMPORARY, "Mail server is busy; try later"
    if 500 <= code < 600:
        if _MS_NO_USER_RE.search(msg):
            return REJECTED, REASON_MS_NO_USER
        if _FULL_RE.search(msg):
            return UNCLEAR, "Mailbox is full"
        if _RELAY_RE.search(msg):
            return UNCLEAR, "This mail server doesn't handle the domain"
        if _DISABLED_RE.search(msg):
            return REJECTED, "Mailbox is disabled"
        if _NO_USER_CODE_RE.search(msg) or _NO_USER_TEXT_RE.search(msg):
            return REJECTED, "Mailbox does not exist"
        if _POLICY_RE.search(msg):
            return UNCLEAR, "Mail server refused the check (policy)"
        if code in (550, 551, 553):
            return REJECTED, "Mailbox does not exist"
        return UNCLEAR, "Mail server gave an unclear answer"
    return UNCLEAR, "Mail server gave an unclear answer"


# ----- one session ------------------------------------------------------------

@dataclass
class Reply:
    code: int | None
    message: str


@dataclass
class SessionResult:
    mx_host: str | None = None  # the server we talked to
    probe: Reply | None = None  # answer for the made-up address (catch-all test)
    replies: dict[str, Reply] = field(default_factory=dict)  # email -> answer to RCPT TO
    # None = the conversation worked; otherwise: connect | blocked | refused | error
    failure: str | None = None
    reason: str | None = None  # plain-English reason when failure is set


def _text(msg: Any) -> str:
    """smtplib gives bytes, possibly several lines -> one plain string."""
    if isinstance(msg, bytes):
        msg = msg.decode("utf-8", "replace")
    return " ".join(str(msg or "").split())


def _quit(client: Any) -> None:
    try:
        client.quit()
    except Exception:  # noqa: BLE001 - the server may already have hung up
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def check_mailboxes(
    mx_hosts: list[str],
    emails: list[str],
    *,
    helo_host: str,
    mail_from: str,
    timeout: float,
    probe_address: str | None = None,
    factory: SmtpFactory | None = None,
) -> SessionResult:
    """Ask the first reachable mail server about `emails` (all of the same domain) in ONE
    session. With `probe_address`, it is asked first to detect a catch-all server.
    Never raises for network problems."""
    factory = factory or default_factory
    emails = emails[: MAX_RCPT_PER_SESSION - (1 if probe_address else 0)]
    failure, reason = "connect", REASON_CONNECT
    for host in mx_hosts[:MAX_MX_TRIED]:
        try:
            client = factory(host, timeout, helo_host)
        except PrivateMailServer:
            log.warning("Not checking %s: its mail server %s is on a private network", emails[:1], host)
            failure, reason = "refused", REASON_PRIVATE
            continue
        except smtplib.SMTPConnectError as e:  # the server answered, but with an error greeting
            category, _ = classify_reply(e.smtp_code, _text(e.smtp_error))
            log.info("SMTP %s refused connection: %s %s", host, e.smtp_code, _text(e.smtp_error))
            if category == BLOCKED:
                return SessionResult(mx_host=host, failure="blocked", reason=REASON_BLOCKED)
            failure, reason = "refused", "Mail server refused the connection"
            continue
        except (OSError, smtplib.SMTPException) as e:  # timeout, refused, port 25 blocked
            log.info("SMTP connect to %s failed: %s", host, e)
            continue
        try:
            return _run_session(client, host, emails, helo_host, mail_from, probe_address)
        finally:
            _quit(client)
    return SessionResult(failure=failure, reason=reason)


def _run_session(client, host, emails, helo_host, mail_from, probe_address) -> SessionResult:
    result = SessionResult(mx_host=host)
    try:
        code, msg = client.ehlo(helo_host)
        if not 200 <= code < 300:
            code, msg = client.helo(helo_host)
        if not 200 <= code < 300:
            return _refused(result, code, msg, "Mail server refused our greeting (set SMTP_HELO_HOST)")

        code, msg = client.mail(mail_from)
        if not 200 <= code < 300:
            return _refused(result, code, msg, "Mail server refused our sender address (set SMTP_MAIL_FROM)")

        if probe_address:
            code, msg = client.rcpt(probe_address)
            result.probe = Reply(code, _text(msg))
            category, _ = classify_reply(code, result.probe.message)
            if category == BLOCKED:
                result.failure, result.reason = "blocked", REASON_BLOCKED
                return result
            if category == ACCEPTED:
                return result  # catch-all: every address would be accepted, no need to ask more

        for email in emails:
            code, msg = client.rcpt(email)
            reply = Reply(code, _text(msg))
            result.replies[email] = reply
            category, _ = classify_reply(code, reply.message)
            if category == BLOCKED:
                result.failure, result.reason = "blocked", REASON_BLOCKED
                break
            if code == 421 or (code == 452 and "too many" in reply.message.lower()):
                break  # server is closing / wants fewer recipients: stop asking
    except (OSError, smtplib.SMTPException) as e:  # hung up or timed out half-way
        log.info("SMTP session with %s ended early: %s", host, e)
        if not result.replies and result.probe is None:
            result.failure, result.reason = "error", "Mail server stopped answering"
    return result


def _refused(result: SessionResult, code: int, msg: Any, reason: str) -> SessionResult:
    text = _text(msg)
    log.info("SMTP %s refused: %s %s", result.mx_host, code, text)
    category, _ = classify_reply(code, text)
    if category == BLOCKED:
        result.failure, result.reason = "blocked", REASON_BLOCKED
    else:
        result.failure, result.reason = "refused", reason
    return result
