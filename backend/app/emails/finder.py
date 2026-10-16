"""Find a person's work email from their name + company domain.

find_person_email("Jane", "Doe", "acme.com") -> FoundEmail("jane.doe@acme.com", "valid", 95, "pattern")

1. Known pattern for the domain (learned from the website or Hunter)? Check that guess first.
   - accepted              -> valid (confidence 95)
   - server accepts all    -> catch_all, confidence = how sure we are of the pattern
   - couldn't check        -> unknown, confidence = pattern confidence
   - rejected              -> the pattern doesn't fit this person; go on to step 2
2. Ask the mail server about the most likely addresses in one session and return the first
   one it accepts (valid, confidence 90). The pattern is remembered for the domain.
3. Nothing confirmed and no known pattern -> None (we don't return blind guesses).
   The same when the pattern doesn't fit the person ("{first}.{last}" but no last name).

find_people_emails() does the same for several people of one company in one go.
"""
import logging
from dataclasses import dataclass

from sqlalchemy import select

from .. import config
from ..db import SessionLocal
from ..models import DomainInfo
from . import smtp as smtp_mod
from .dns import Resolver
from .patterns import _clean_domain, candidate_emails, learn_pattern, pattern_email
from .verify import REASON_MS_UNSURE, verify_emails

log = logging.getLogger("emails.finder")


@dataclass
class FoundEmail:
    email: str
    status: str  # valid | catch_all | unknown (never invalid)
    confidence: int  # 0-100
    source: str  # "pattern"


def _stored_pattern(domain: str) -> tuple[str | None, int]:
    """(pattern, confidence) known for the domain, e.g. ("{first}.{last}", 85)."""
    with SessionLocal() as session:
        info = session.scalar(select(DomainInfo).where(DomainInfo.domain == domain))
    if info is None or not info.email_pattern:
        return None, 0
    return info.email_pattern, info.pattern_confidence or 0


def find_person_email(
    first: str,
    last: str | None,
    domain: str,
    *,
    force: bool = False,
    use_smtp: bool | None = None,
    resolve: Resolver | None = None,
    smtp_factory: smtp_mod.SmtpFactory | None = None,
) -> FoundEmail | None:
    """Best email for a person at `domain`, or None. Never raises for network problems."""
    return find_people_emails([(first, last)], domain, force=force, use_smtp=use_smtp,
                              resolve=resolve, smtp_factory=smtp_factory)[0]


def find_people_emails(
    people: list[tuple[str, str | None]],
    domain: str,
    *,
    force: bool = False,
    use_smtp: bool | None = None,
    resolve: Resolver | None = None,
    smtp_factory: smtp_mod.SmtpFactory | None = None,
) -> list[FoundEmail | None]:
    """find_person_email for several people of ONE company at once: all their guesses are
    checked together (one mail-server session per ~19 addresses instead of one per person).
    `people` = [(first, last), ...]; returns one FoundEmail or None per person, same order."""
    domain = _clean_domain(domain or "")
    use_smtp = config.SMTP_VERIFY if use_smtp is None else use_smtp
    pattern, pattern_conf = _stored_pattern(domain)
    found: list[FoundEmail | None] = [None] * len(people)
    candidates = [candidate_emails(first, last, domain, pattern) if domain else [] for first, last in people]

    def check(emails: list[str]):
        return verify_emails(emails, force=force, use_smtp=use_smtp, resolve=resolve, smtp_factory=smtp_factory)

    # 1. The domain's known pattern: one guess per person, all checked together. Only when the
    #    pattern really fits this person (e.g. "{first}.{last}" needs a last name).
    tops = {i: pattern_email(first, last, domain, pattern) for i, (first, last) in enumerate(people)}
    tops = {i: top for i, top in tops.items() if top and candidates[i] and candidates[i][0] == top}
    done: set[int] = set()  # people we have an answer for (an email, or "give up")
    if tops:
        results = check(list(dict.fromkeys(tops.values())))
        for i, top in tops.items():
            result = results[top]
            if result.mx_found is False:  # the domain can't receive email at all
                return found
            if result.status == "valid":
                found[i] = FoundEmail(top, "valid", 95, "pattern")
            elif result.status == "catch_all":
                found[i] = FoundEmail(top, "catch_all", pattern_conf, "pattern")
            elif result.status == "unknown" or (result.status == "risky" and result.reason != REASON_MS_UNSURE):
                found[i] = FoundEmail(top, "unknown", pattern_conf, "pattern")
            # invalid (or Microsoft said "Access denied"): this person doesn't follow the usual
            # pattern - try the other candidates in step 2
            if found[i] is not None:
                done.add(i)

    # 2. Ask the mail server about the other likely addresses (needs SMTP).
    if not use_smtp:
        return found
    rest = {i: [e for e in candidates[i] if e != tops.get(i)] for i in range(len(people)) if i not in done}
    rest = {i: emails for i, emails in rest.items() if emails}
    if not rest:
        return found
    results = check(list(dict.fromkeys(e for emails in rest.values() for e in emails)))
    for i, emails in rest.items():
        for email in emails:  # best guess first
            if results[email].status == "valid":
                first, last = people[i]
                learn_pattern(domain, [(first, last or "", email)], source="smtp")
                found[i] = FoundEmail(email, "valid", 90, "pattern")
                break
    return found
