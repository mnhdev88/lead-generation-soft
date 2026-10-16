"""Domain checks (DNS): does the domain exist, and which mail servers (MX) receive its email?

lookup_mx("acme.com") -> MxResult(state="ok", hosts=["mx1.acme.com", "mx2.acme.com"])

- Mail servers are sorted best first (lowest MX "preference" number).
- No MX record but the domain has an address (A/AAAA)? Then the domain itself receives
  mail ("implicit MX", RFC 5321 section 5.1).
- MX "0 ." (a "null MX", RFC 7505) means the domain explicitly accepts no email.

The real DNS lookup is a plain function, so tests pass a fake `resolve` instead.
"""
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

import dns.exception
import dns.resolver

log = logging.getLogger("emails.dns")

DNS_TIMEOUT = 5.0  # seconds per lookup

# resolve(name, rdtype) -> records. For "MX": list of (preference, host);
# for "A"/"AAAA": list of IP strings. Raises dnspython errors (NXDOMAIN, NoAnswer, Timeout...).
Resolver = Callable[[str, str], list]

REASON_NO_DOMAIN = "Domain does not exist"
REASON_NO_MAIL = "Domain can't receive email"
REASON_DNS_ERROR = "Couldn't look up the domain (DNS problem); try again later"


@dataclass
class MxResult:
    state: str  # ok | no_domain | no_mail | error
    hosts: list[str] = field(default_factory=list)  # mail servers, best first
    reason: str | None = None  # plain-English explanation when state != ok


_resolver: dns.resolver.Resolver | None = None
_resolver_lock = threading.Lock()


def _get_resolver() -> dns.resolver.Resolver:
    """One shared resolver (reading the system DNS settings is slow on Windows)."""
    global _resolver
    with _resolver_lock:
        if _resolver is None:
            r = dns.resolver.Resolver()
            r.timeout = DNS_TIMEOUT
            r.lifetime = DNS_TIMEOUT
            _resolver = r
        return _resolver


def default_resolve(name: str, rdtype: str) -> list:
    """The real DNS lookup (dnspython)."""
    answer = _get_resolver().resolve(name, rdtype, search=False)
    if rdtype == "MX":
        return [(r.preference, r.exchange.to_text(omit_final_dot=True)) for r in answer]
    return [r.to_text() for r in answer]


def lookup_mx(domain: str, resolve: Resolver | None = None) -> MxResult:
    """Find the mail servers of `domain`. Never raises for DNS problems (state="error")."""
    resolve = resolve or default_resolve
    try:
        records = resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        return MxResult("no_domain", reason=REASON_NO_DOMAIN)
    except dns.resolver.NoAnswer:
        records = []  # domain exists but has no MX -> try the implicit MX below
    except (dns.exception.DNSException, OSError) as e:  # timeout, no nameservers, network down
        log.info("MX lookup failed for %s: %s", domain, e)
        return MxResult("error", reason=REASON_DNS_ERROR)

    hosts = []
    for _pref, host in sorted(records, key=lambda rec: rec[0]):
        host = (host or "").strip().rstrip(".").lower()
        if host and host not in hosts:
            hosts.append(host)
    if records and not hosts:
        return MxResult("no_mail", reason=REASON_NO_MAIL)  # null MX "0 ."
    if hosts:
        return MxResult("ok", hosts=hosts)

    # No MX record: the domain itself can receive mail if it has an address.
    had_error = False
    for rdtype in ("A", "AAAA"):
        try:
            if resolve(domain, rdtype):
                return MxResult("ok", hosts=[domain])
        except dns.resolver.NXDOMAIN:
            return MxResult("no_domain", reason=REASON_NO_DOMAIN)
        except dns.resolver.NoAnswer:
            continue
        except (dns.exception.DNSException, OSError) as e:
            log.info("%s lookup failed for %s: %s", rdtype, domain, e)
            had_error = True
    if had_error:
        return MxResult("error", reason=REASON_DNS_ERROR)
    return MxResult("no_mail", reason=REASON_NO_MAIL)
