"""Email engine (Step 4): check addresses and guess people's work emails.

    verify_emails(["info@acme.com"])            -> {"info@acme.com": CheckResult(status="valid", ...)}
    learn_pattern("acme.com", [("John", "Smith", "john.smith@acme.com")])  -> ("{first}.{last}", 60)
    find_person_email("Jane", "Doe", "acme.com") -> FoundEmail("jane.doe@acme.com", "valid", 95, "pattern")
    find_people_emails([("Jane", "Doe"), ("Bob", None)], "acme.com") -> [FoundEmail(...), None]

Modules: syntax (format, role/free/disposable), dns (MX lookup), smtp (mailbox check),
verify (puts it together + cache), patterns (name -> address patterns), finder.
See verify.py for what each status means.
"""
from .finder import FoundEmail, find_people_emails, find_person_email
from .patterns import candidate_emails, detect_pattern, learn_pattern
from .syntax import normalize_email
from .verify import CheckResult, verify_emails

__all__ = [
    "CheckResult",
    "FoundEmail",
    "candidate_emails",
    "detect_pattern",
    "find_people_emails",
    "find_person_email",
    "learn_pattern",
    "normalize_email",
    "verify_emails",
]
