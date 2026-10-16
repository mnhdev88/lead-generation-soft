"""Email patterns (name -> address) and find_person_email. No real network."""
import pytest
from sqlalchemy import select

from app import config
from app.db import SessionLocal
from app.emails import candidate_emails, detect_pattern, find_people_emails, find_person_email, learn_pattern
from app.emails import verify as verify_mod
from app.emails.patterns import render, split_name
from app.models import DomainInfo
from tests.test_emails_verify import ACME_DNS, FakeServer, make_resolver


@pytest.fixture(autouse=True)
def fresh_smtp_state(monkeypatch):
    verify_mod.reset_smtp_state()
    monkeypatch.setattr(config, "SMTP_HELO_HOST", "checker.leadgen.test")
    yield
    verify_mod.reset_smtp_state()


# ----- names ------------------------------------------------------------------

@pytest.mark.parametrize("first, last, expected", [
    ("John", "Smith", (["john"], ["smith"])),
    ("Dr. John", "Smith", (["john"], ["smith"])),
    ("Mr John Paul", "Smith Jr.", (["john"], ["smith"])),
    ("José", "García", (["jose"], ["garcia"])),
    ("Jürgen", "Müller-Lüdenscheidt", (["jurgen"], ["muller", "ludenscheidt"])),
    ("Anne-Marie", "O'Brien", (["anne", "marie"], ["obrien"])),
    ("Søren", "Kierkegaard", (["soren"], ["kierkegaard"])),
    ("Dr. Sarah Khan", None, (["sarah"], ["khan"])),  # full name in the first-name field
    ("Ali", "van der Berg", (["ali"], ["van", "der", "berg"])),
    ("Ahmed", "Raza, DDS", (["ahmed"], ["raza"])),
])
def test_split_name(first, last, expected):
    assert split_name(first, last) == expected


def test_render():
    assert render("{first}.{last}", "john", "smith") == "john.smith"
    assert render("{f}{last}", "john", "smith") == "jsmith"
    assert render("{first}{l}", "john", "smith") == "johns"
    assert render("{last}.{first}", "john", "smith") == "smith.john"
    assert render("{first}.{last}", "john", None) is None


# ----- detect_pattern -------------------------------------------------------------

def test_detect_single_example():
    assert detect_pattern("acme.com", [("John", "Smith", "john.smith@acme.com")]) == ("{first}.{last}", 60)


def test_detect_more_examples_more_confidence():
    known = [("John", "Smith", "jsmith@acme.com"), ("Jane", "Doe", "jdoe@acme.com")]
    assert detect_pattern("acme.com", known) == ("{f}{last}", 85)
    known.append(("Bob", "Lee", "blee@acme.com"))
    assert detect_pattern("acme.com", known) == ("{f}{last}", 90)
    known.append(("Amy", "Wong", "awong@acme.com"))
    assert detect_pattern("acme.com", known) == ("{f}{last}", 95)


def test_detect_disagreement_lowers_confidence():
    known = [("John", "Smith", "john.smith@acme.com"), ("Jane", "Doe", "jane.doe@acme.com"),
             ("Bob", "Lee", "blee@acme.com")]
    assert detect_pattern("acme.com", known) == ("{first}.{last}", 70)


def test_detect_ignores_other_domains_and_role_addresses():
    known = [("John", "Smith", "john.smith@gmail.com"), ("Jane", "Doe", "info@acme.com"),
             ("Bad", "Email", "not-an-email")]
    assert detect_pattern("acme.com", known) == (None, 0)


def test_detect_accented_and_hyphenated_names():
    assert detect_pattern("acme.es", [("José", "García", "jose.garcia@acme.es")])[0] == "{first}.{last}"
    assert detect_pattern("acme.de", [("Jürgen", "Müller-Lüdenscheidt", "jmuller@acme.de")])[0] == "{f}{last}"
    assert detect_pattern("acme.de", [("Eva", "Schmidt-Weber", "eva.schmidt-weber@acme.de")])[0] == "{first}.{last}"
    assert detect_pattern("acme.fr", [("Anne-Marie", "Dupont", "annemarie@acme.fr")])[0] == "{first}"
    assert detect_pattern("acme.fr", [("Anne-Marie", "Dupont", "anne.dupont@acme.fr")])[0] == "{first}.{last}"


def test_detect_first_name_only():
    assert detect_pattern("clinic.pk", [("Dr. Sara", None, "sara@clinic.pk")]) == ("{first}", 60)


def test_detect_tie_prefers_common_pattern():
    # "j@" style can't happen (one-letter guesses are skipped), but john@ matches only {first}
    known = [("John", "Smith", "john@acme.com"), ("Jane", "Doe", "jane.doe@acme.com")]
    assert detect_pattern("acme.com", known) == ("{first}", 45)


# ----- candidate_emails -----------------------------------------------------------

def test_candidates_known_pattern_first():
    out = candidate_emails("John", "Smith", "acme.com", "{last}.{first}")
    assert out[0] == "smith.john@acme.com"
    assert len(out) == 8
    assert len(set(out)) == 8


def test_candidates_popular_order_without_pattern():
    out = candidate_emails("John", "Smith", "www.Acme.com")
    assert out[:5] == ["john@acme.com", "john.smith@acme.com", "jsmith@acme.com",
                       "johnsmith@acme.com", "johns@acme.com"]


def test_candidates_accents_and_missing_last_name():
    assert candidate_emails("Zoë", "Ångström", "acme.se")[1] == "zoe.angstrom@acme.se"
    assert candidate_emails("Ann", None, "acme.com", "{first}.{last}") == ["ann@acme.com"]
    assert candidate_emails("", "Smith", "acme.com") == []


# ----- learn_pattern ----------------------------------------------------------------

def _domain(name):
    with SessionLocal() as s:
        return s.scalar(select(DomainInfo).where(DomainInfo.domain == name))


def test_learn_pattern_stores_and_keeps_the_better_one(db):
    assert learn_pattern("acme.com", [("John", "Smith", "john.smith@acme.com"),
                                      ("Jane", "Doe", "jane.doe@acme.com")]) == ("{first}.{last}", 85)
    info = _domain("acme.com")
    assert (info.email_pattern, info.pattern_confidence, info.pattern_source) == ("{first}.{last}", 85, "website")

    # A weaker, different guess doesn't replace it...
    assert learn_pattern("acme.com", [("Bob", "Lee", "blee@acme.com")], source="hunter") == ("{first}.{last}", 85)
    # ...nothing detected returns what is stored.
    assert learn_pattern("acme.com", []) == ("{first}.{last}", 85)
    assert learn_pattern("unknown.com", []) == (None, 0)


def test_learn_pattern_replaces_guess(db):
    db.add(DomainInfo(domain="acme.com", email_pattern="{first}", pattern_confidence=90, pattern_source="guess"))
    db.commit()
    assert learn_pattern("acme.com", [("Bob", "Lee", "blee@acme.com")]) == ("{f}{last}", 60)


# ----- find_person_email ------------------------------------------------------------

def _set_pattern(db, pattern, confidence=85, accepts_all=None):
    db.add(DomainInfo(domain="acme.com", email_pattern=pattern, pattern_confidence=confidence,
                      pattern_source="website", accepts_all=accepts_all))
    db.commit()


def _find(first, last, server, records=ACME_DNS, **kwargs):
    return find_person_email(first, last, "acme.com", resolve=make_resolver(records),
                             smtp_factory=server.factory, **kwargs)


def test_find_with_known_pattern_verified(db):
    _set_pattern(db, "{first}.{last}")
    server = FakeServer(answers={"jane.doe@acme.com": (250, "OK")})
    found = _find("Jane", "Doe", server)
    assert (found.email, found.status, found.confidence, found.source) == ("jane.doe@acme.com", "valid", 95, "pattern")
    assert server.rcpts[1:] == ["jane.doe@acme.com"]  # only the pattern guess was asked


def test_find_catch_all_returns_pattern_guess(db):
    _set_pattern(db, "{f}{last}", confidence=85)
    server = FakeServer(default=(250, "OK"), probe=(250, "OK"))
    found = _find("Jane", "Doe", server)
    assert (found.email, found.status, found.confidence) == ("jdoe@acme.com", "catch_all", 85)


def test_find_without_smtp_returns_pattern_guess_only_when_pattern_known(db):
    server = FakeServer()
    assert _find("Jane", "Doe", server, use_smtp=False) is None
    _set_pattern(db, "{first}", confidence=60)
    found = _find("Jane", "Doe", server, use_smtp=False)
    assert (found.email, found.status, found.confidence) == ("jane@acme.com", "unknown", 60)
    assert server.connects == []


def test_find_tries_candidates_in_one_session_and_learns_pattern(db):
    server = FakeServer(answers={"jdoe@acme.com": (250, "OK")})
    found = _find("Jane", "Doe", server)
    assert (found.email, found.status, found.confidence) == ("jdoe@acme.com", "valid", 90)
    assert len(server.sessions) == 1
    assert server.sessions[0][1:4] == ["jane@acme.com", "jane.doe@acme.com", "jdoe@acme.com"]
    assert _domain("acme.com").email_pattern == "{f}{last}"
    assert _domain("acme.com").pattern_source == "smtp"


def test_find_pattern_rejected_falls_back_to_other_candidates(db):
    _set_pattern(db, "{first}.{last}")
    server = FakeServer(answers={"jane@acme.com": (250, "OK")})
    found = _find("Jane", "Doe", server)
    assert (found.email, found.status) == ("jane@acme.com", "valid")
    assert len(server.sessions) == 2  # the pattern guess, then the other candidates together


def test_find_nothing_accepted(db):
    assert _find("Jane", "Doe", FakeServer()) is None  # every address rejected
    assert _find("Jane", "Doe", FakeServer(), records={}) is None  # domain doesn't exist


def test_find_port_25_blocked_without_pattern(db):
    server = FakeServer(connect_error=TimeoutError("timed out"))
    assert _find("Jane", "Doe", server) is None


def test_find_microsoft_access_denied_tries_other_candidates(db):
    _set_pattern(db, "{first}.{last}")
    denied = (550, "5.4.1 Recipient address rejected: Access denied. AS(201806281)")
    server = FakeServer(answers={"jdoe@acme.com": (250, "2.1.5 Recipient OK")}, default=denied)
    records = {"acme.com": {"MX": [(0, "acme-com.mail.protection.outlook.com")]}}
    found = _find("Jane", "Doe", server, records)
    assert (found.email, found.status) == ("jdoe@acme.com", "valid")


def test_find_pattern_needing_last_name_is_not_used_without_one(db):
    # "{first}.{last}" can't be filled for "Maria" (no last name): "maria@" would be a blind
    # guess, not the pattern, so it must not get the pattern's confidence.
    _set_pattern(db, "{first}.{last}", confidence=95)
    assert _find("Maria", None, FakeServer(), use_smtp=False) is None
    catch_all = FakeServer(default=(250, "OK"), probe=(250, "OK"))
    assert _find("Maria", None, catch_all) is None  # a catch-all server can't confirm it either


def test_find_people_checks_everyone_together(db):
    _set_pattern(db, "{first}.{last}")
    names = ["ann", "bob", "cat", "dan", "eve", "fay"]
    server = FakeServer(answers={f"{n}.doe@acme.com": (250, "OK") for n in names})
    found = find_people_emails([(n.title(), "Doe") for n in names] + [("Gus", None)], "acme.com",
                               resolve=make_resolver(ACME_DNS), smtp_factory=server.factory)
    assert [f.email for f in found[:6]] == [f"{n}.doe@acme.com" for n in names]
    assert all(f.status == "valid" and f.confidence == 95 for f in found[:6])
    # one session for the six pattern guesses (+ one for Gus, whose name doesn't fit the pattern)
    assert len(server.sessions) == 2
    assert found[6] is None
