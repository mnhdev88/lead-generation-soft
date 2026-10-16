"""Worker tests for "emails" jobs (find + verify emails, Step 4) and the
search -> crawl -> emails chain.

The email engine, Hunter.io, the title classifier and the scorer are replaced by small
fakes on the worker module, so these tests only check how the worker uses them
(and never touch the network, DNS or SMTP)."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import worker
from app.emails import CheckResult, FoundEmail
from app.enrich import PersonFound, WebsiteData
from app.models import Company, Contact, DomainInfo, JobResult, SearchJob
from app.people import Classification
from app.providers.hunter import HunterPerson
from app.sources import SOURCES, CompanyResult, Source
from app.usage import UsageLimitError


class FakeEngine:
    """Stands in for app.emails, app.people.classify, app.scoring.rescore and Hunter."""

    def __init__(self):
        self.statuses: dict[str, str] = {}  # email -> status (default "unknown")
        self.guesses: dict[tuple, FoundEmail] = {}  # (first, last, domain) -> FoundEmail
        self.pattern = ("{first}.{last}", 85)
        self.verify_calls: list[tuple[list[str], bool]] = []
        self.learn_calls: list[tuple[str, list, str]] = []
        self.find_calls: list[tuple] = []
        self.rescored: list[int] = []
        self.broken_domains: set[str] = set()  # verify_emails raises for these
        self.hunter_key = True
        self.hunter_people: dict[str, list[HunterPerson]] = {}
        self.hunter_calls: list[str] = []
        self.hunter_capped = False

    # app.emails
    def verify_emails(self, emails, *, force=False, use_smtp=None):
        self.verify_calls.append((list(emails), force))
        if any(e.split("@")[1] in self.broken_domains for e in emails):
            raise RuntimeError("mail server exploded")
        return {
            e: CheckResult(e, self.statuses.get(e, "unknown"), None, False, False, False, True, 250)
            for e in emails
        }

    def find_people_emails(self, people, domain, *, force=False):
        out = []
        for first, last in people:
            self.find_calls.append((first, last, domain))
            out.append(self.guesses.get((first, last, domain)))
        return out

    def learn_pattern(self, domain, known, source="website"):
        self.learn_calls.append((domain, list(known), source))
        return self.pattern

    # app.people / app.scoring
    @staticmethod
    def classify(title):
        t = (title or "").lower()
        if "owner" in t or "ceo" in t:
            return Classification("owner", "management", True)
        if "manager" in t:
            return Classification("manager", "admin", True)
        if t:
            return Classification("staff", "other", False)
        return Classification(None, None, False)

    def rescore(self, company):
        self.rescored.append(company.id)
        company.score = 10 * len(company.emails or [])

    # app.providers.hunter
    def hunter_module(self):
        def domain_search(domain, limit=10):
            if self.hunter_capped:
                raise UsageLimitError("Monthly Hunter.io limit reached (25/25).")
            self.hunter_calls.append(domain)
            return "{first}", self.hunter_people.get(domain, [])

        return SimpleNamespace(available=lambda: self.hunter_key, domain_search=domain_search)


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(worker, "verify_emails", fake.verify_emails)
    monkeypatch.setattr(worker, "find_people_emails", fake.find_people_emails)
    monkeypatch.setattr(worker, "learn_pattern", fake.learn_pattern)
    monkeypatch.setattr(worker, "classify", fake.classify)
    monkeypatch.setattr(worker, "rescore", fake.rescore)
    monkeypatch.setattr(worker, "hunter", fake.hunter_module())
    monkeypatch.setattr(worker, "get_usage", lambda provider: (3, 25))
    return fake


def company(db, name, *, contacts=(), **kw) -> Company:
    c = Company(name=name, source="osm", source_ref=name, **kw)
    c.contacts = list(contacts)
    db.add(c)
    db.commit()
    return c


def person(full_name, title=None, email=None, source="website", **kw) -> Contact:
    first, last = worker._split_name(full_name)
    return Contact(full_name=full_name, first_name=first, last_name=last, title=title, email=email,
                   email_source="website" if email else None, source=source, **kw)


def run_emails(db, parent=None, **params) -> SearchJob:
    job = SearchJob(kind="emails", query="q", location="l", source="emails",
                    parent_job_id=parent.id if parent else None, params=params)
    db.add(job)
    db.commit()
    worker.run_job(job.id)
    db.expire_all()
    return db.get(SearchJob, job.id)


def contacts_by_name(db, c: Company) -> dict[str, Contact]:
    return {x.full_name: x for x in db.scalars(select(Contact).where(Contact.company_id == c.id))}


# ----- the main flow -----------------------------------------------------------------


def test_emails_job_finds_verifies_and_orders(db, engine):
    acme = company(
        db, "Acme", website="https://www.acme.pk", domain="acme.pk", email="Old@Acme.pk",
        emails=["old@acme.pk", "info@acme.pk", "sales@acme.pk"],
        contacts=[
            person("Jane Doe", "Owner", "jane.doe@acme.pk"),
            person("Bob Smith", "Office Manager"),
            person("Ali", "Receptionist"),
            person("Zed Gmail", "Dentist", "zed@gmail.com"),
        ],
    )
    engine.statuses = {"old@acme.pk": "invalid", "info@acme.pk": "catch_all", "sales@acme.pk": "valid",
                       "jane.doe@acme.pk": "valid", "bob.smith@acme.pk": "catch_all"}
    engine.guesses[("Bob", "Smith", "acme.pk")] = FoundEmail("bob.smith@acme.pk", "catch_all", 85, "pattern")

    job = run_emails(db, company_ids=[acme.id])
    assert job.status == "done" and job.error is None
    assert (job.total, job.processed, job.result_count) == (1, 1, 1)
    assert job.progress_message == (
        "Checked 1 companies: 1 with a deliverable email (1 verified, 0 catch-all), 1 people emails found."
    )

    # b. pattern learned from people with first + last name + an email at the company domain
    assert engine.learn_calls == [("acme.pk", [("Jane", "Doe", "jane.doe@acme.pk")], "website")]
    # d. only people without an email, at the company domain
    assert engine.find_calls == [("Bob", "Smith", "acme.pk"), ("Ali", None, "acme.pk")]
    # e. ONE verify call with every known email of the company and its people (the guessed
    #    bob.smith@ was already checked by the finder, so it isn't checked twice)
    assert len(engine.verify_calls) == 1
    checked, force = engine.verify_calls[0]
    assert set(checked) == {"old@acme.pk", "info@acme.pk", "sales@acme.pk", "jane.doe@acme.pk",
                            "zed@gmail.com"}
    assert force is False

    a = db.get(Company, acme.id)
    assert a.emails == ["sales@acme.pk", "info@acme.pk", "old@acme.pk"]  # valid, catch-all, invalid last
    assert a.email == "sales@acme.pk"
    assert a.email_statuses == {"old@acme.pk": "invalid", "info@acme.pk": "catch_all", "sales@acme.pk": "valid"}
    assert a.emails_checked_at is not None
    assert engine.rescored == [acme.id]

    people = contacts_by_name(db, a)
    jane, bob, ali, zed = people["Jane Doe"], people["Bob Smith"], people["Ali"], people["Zed Gmail"]
    assert (jane.email_status, jane.email_confidence, jane.email_source) == ("valid", 95, "website")
    assert jane.email_checked_at is not None
    assert (bob.email, bob.email_source, bob.email_status, bob.email_confidence) == (
        "bob.smith@acme.pk", "pattern", "catch_all", 85)
    assert ali.email is None and ali.email_status is None
    # unknown, but listed on the company's own website -> 70 instead of 40
    assert (zed.email_status, zed.email_confidence) == ("unknown", 70)
    # f. everyone classified
    assert (jane.seniority, jane.is_decision_maker) == ("owner", True)
    assert (bob.seniority, bob.department, bob.is_decision_maker) == ("manager", "admin", True)
    assert (ali.seniority, ali.is_decision_maker) == ("staff", False)


def test_all_invalid_emails_stay_listed_but_no_best_email(db, engine):
    c = company(db, "Dead", domain="dead.pk", email="a@dead.pk", emails=["a@dead.pk", "b@dead.pk"])
    engine.statuses = {"a@dead.pk": "invalid", "b@dead.pk": "invalid"}
    job = run_emails(db, company_ids=[c.id])
    d = db.get(Company, c.id)
    assert d.email is None
    assert d.emails == ["a@dead.pk", "b@dead.pk"]
    assert d.email_statuses == {"a@dead.pk": "invalid", "b@dead.pk": "invalid"}
    assert job.result_count == 0


def test_targets_come_from_parent_search_and_force_is_passed(db, engine):
    search = SearchJob(kind="search", query="q", location="l", source="osm", status="done")
    db.add(search)
    a = company(db, "A", domain="a.pk", emails=["info@a.pk"])
    b = company(db, "B", domain="b.pk", emails=["info@b.pk"])
    company(db, "Other", domain="o.pk", emails=["info@o.pk"])
    db.add_all([JobResult(job_id=search.id, company_id=a.id), JobResult(job_id=search.id, company_id=b.id)])
    db.commit()
    engine.statuses = {"info@a.pk": "valid", "info@b.pk": "catch_all"}

    job = run_emails(db, parent=search, force=True)
    assert sorted(e for call, _ in engine.verify_calls for e in call) == ["info@a.pk", "info@b.pk"]
    assert all(force for _, force in engine.verify_calls)
    assert job.result_count == 2
    assert "2 with a deliverable email (1 verified, 1 catch-all)" in job.progress_message
    assert db.get(Company, b.id).email == "info@b.pk"


def test_one_bad_company_never_stops_the_job(db, engine):
    bad = company(db, "Bad", domain="bad.pk", emails=["info@bad.pk"])
    good = company(db, "Good", domain="good.pk", emails=["info@good.pk"])
    engine.broken_domains = {"bad.pk"}
    engine.statuses = {"info@good.pk": "valid"}

    job = run_emails(db, company_ids=[bad.id, good.id])
    assert job.status == "done"
    assert job.processed == 2 and job.result_count == 1
    assert job.progress_message.endswith(", 1 failed.")
    assert db.get(Company, good.id).email_statuses == {"info@good.pk": "valid"}
    assert db.get(Company, bad.id).email_statuses == {}  # untouched, still listed
    assert db.get(Company, bad.id).emails == ["info@bad.pk"]


# ----- Hunter.io -----------------------------------------------------------------------


def test_hunter_used_when_company_has_no_emails(db, engine):
    c = company(db, "Hollow", website="https://hollow.pk", domain="hollow.pk",
                contacts=[person("Sara Lee", "Receptionist")])
    engine.hunter_people["hollow.pk"] = [
        HunterPerson("sara.lee@hollow.pk", "Sara", "Lee", "CEO", 92, "https://linkedin.com/in/sara"),
        HunterPerson("omar@hollow.pk", "Omar", "Khan", "Office Manager", 80, None),
    ]
    engine.statuses = {"omar@hollow.pk": "valid"}

    job = run_emails(db, company_ids=[c.id])
    assert engine.hunter_calls == ["hollow.pk"]
    people = contacts_by_name(db, c)
    assert sorted(people) == ["Omar Khan", "Sara Lee"]  # Sara merged by name, Omar added
    sara, omar = people["Sara Lee"], people["Omar Khan"]
    assert (sara.email, sara.email_source, sara.title, sara.source) == (
        "sara.lee@hollow.pk", "hunter", "Receptionist", "website")  # existing title kept
    assert sara.linkedin_url == "https://linkedin.com/in/sara"
    assert (sara.email_status, sara.email_confidence) == ("unknown", 92)  # Hunter's score kept
    assert (omar.source, omar.email_source, omar.email_status, omar.email_confidence) == (
        "hunter", "hunter", "valid", 95)
    assert omar.is_decision_maker is True

    h = db.get(Company, c.id)
    assert h.emails == ["omar@hollow.pk", "sara.lee@hollow.pk"]
    assert h.email == "omar@hollow.pk"
    # the pattern is learned from Hunter's people (source "hunter")
    hunter_learn = [call for call in engine.learn_calls if call[2] == "hunter"]
    assert hunter_learn and hunter_learn[0][0] == "hollow.pk"
    assert ("Omar", "Khan", "omar@hollow.pk") in hunter_learn[0][1]
    assert "2 people emails found" in job.progress_message
    assert "Hunter used 3/25 this month" in job.progress_message

    # Asked once already: a second run doesn't pay again
    run_emails(db, company_ids=[c.id])
    assert engine.hunter_calls == ["hollow.pk"]


def test_hunter_skipped_when_not_needed_or_not_allowed(db, engine):
    has_email = company(db, "HasEmail", domain="a.pk", emails=["info@a.pk"])
    facebook = company(db, "FbOnly", website="https://facebook.com/fb", domain="facebook.com")
    no_domain = company(db, "NoDomain")
    run_emails(db, company_ids=[has_email.id, facebook.id, no_domain.id])
    assert engine.hunter_calls == []

    empty = company(db, "Empty", domain="empty.pk")
    run_emails(db, company_ids=[empty.id], use_hunter=False)
    assert engine.hunter_calls == []
    engine.hunter_key = False
    run_emails(db, company_ids=[empty.id])
    assert engine.hunter_calls == []

    # A failed crawl counts as "nothing found", even with an email from the source
    engine.hunter_key = True
    failed = company(db, "Failed", domain="failed.pk", emails=["x@failed.pk"], enrich_status="failed")
    run_emails(db, company_ids=[failed.id])
    assert engine.hunter_calls == ["failed.pk"]


def test_hunter_monthly_limit_noted_and_job_continues(db, engine):
    a = company(db, "A", domain="a.pk")
    b = company(db, "B", domain="b.pk", emails=["info@b.pk"])
    engine.hunter_capped = True
    engine.statuses = {"info@b.pk": "valid"}
    job = run_emails(db, company_ids=[a.id, b.id])
    assert job.status == "done" and job.processed == 2 and job.result_count == 1
    assert job.progress_message.endswith("Hunter.io skipped: monthly limit reached.")


# ----- "Find decision-makers": crawl first -------------------------------------------


def site(emails=(), people=(), ok=True, error=None) -> WebsiteData:
    return WebsiteData(ok=ok, error=error, final_url=None, emails=list(emails), phones=[], socials={},
                       people=list(people), signals={"https": True, "platform": "WordPress"})


def fake_crawler(monkeypatch, answers: dict) -> list[str]:
    calls: list[str] = []

    async def crawl(url, client=None, max_pages=None):
        calls.append(url)
        return answers[url]

    monkeypatch.setattr(worker, "crawl_website", crawl)
    return calls


def test_crawl_first_then_find_decision_makers(db, engine, monkeypatch):
    fresh = company(db, "Fresh", website="https://fresh.pk", domain="fresh.pk")
    crawled = company(db, "Crawled", website="https://crawled.pk", domain="crawled.pk", enrich_status="done",
                      emails=["info@crawled.pk"])
    calls = fake_crawler(monkeypatch, {"https://fresh.pk": site(
        emails=["info@fresh.pk"],
        people=[PersonFound(full_name="Dr. Nadia Aziz", title="Owner"),
                PersonFound(full_name="Kamran Ali", title="Manager", email="kamran.ali@fresh.pk")],
    )})
    engine.guesses[("Nadia", "Aziz", "fresh.pk")] = FoundEmail("nadia.aziz@fresh.pk", "valid", 95, "pattern")
    engine.statuses = {"nadia.aziz@fresh.pk": "valid", "info@fresh.pk": "catch_all"}

    job = run_emails(db, company_ids=[fresh.id, crawled.id], crawl=True)
    assert calls == ["https://fresh.pk"]  # already-crawled sites are not crawled again
    f = db.get(Company, fresh.id)
    assert f.enrich_status == "done" and f.website_signals["platform"] == "WordPress"
    people = contacts_by_name(db, f)
    nadia = people["Dr. Nadia Aziz"]
    assert (nadia.email, nadia.email_source, nadia.email_status, nadia.email_confidence) == (
        "nadia.aziz@fresh.pk", "pattern", "valid", 95)
    assert (nadia.seniority, nadia.is_decision_maker) == ("owner", True)
    assert ("fresh.pk", [("Kamran", "Ali", "kamran.ali@fresh.pk")], "website") in engine.learn_calls
    assert (job.total, job.processed) == (2, 2)
    assert job.status == "done"


def test_without_crawl_flag_nothing_is_crawled(db, engine, monkeypatch):
    c = company(db, "Fresh", website="https://fresh.pk", domain="fresh.pk", emails=["info@fresh.pk"])
    calls = fake_crawler(monkeypatch, {})
    run_emails(db, company_ids=[c.id])
    assert calls == []


# ----- the chain: search -> crawl -> emails ---------------------------------------------


class FakeSource(Source):
    name, label, description = "fake", "Fake", "For tests"

    def unavailable_reason(self):
        return None

    def search(self, query, location, limit, progress):
        return [CompanyResult(name="Chain Co", source_ref="chain", website="chain.pk")]


def test_search_crawl_emails_chain(db, engine, monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", FakeSource())
    fake_crawler(monkeypatch, {"https://chain.pk": site(emails=["info@chain.pk"])})
    engine.statuses = {"info@chain.pk": "valid"}

    search = SearchJob(kind="search", query="dentist", location="Karachi", source="fake", enrich=True,
                       params={"find_emails": True, "titles": ["owner"]})
    db.add(search)
    db.commit()
    worker.run_job(search.id)

    crawl = db.scalar(select(SearchJob).where(SearchJob.kind == "enrich"))
    assert crawl.parent_job_id == search.id and crawl.params == {"find_emails": True}
    worker.run_job(crawl.id)
    db.expire_all()
    crawl = db.get(SearchJob, crawl.id)
    assert crawl.progress_message.endswith("Email check queued.")

    emails_job = db.scalar(select(SearchJob).where(SearchJob.kind == "emails"))
    assert emails_job.parent_job_id == search.id  # nested under the search in the UI
    assert (emails_job.status, emails_job.source) == ("queued", "emails")
    assert emails_job.params == {"force": False, "use_hunter": True}
    worker.run_job(emails_job.id)
    db.expire_all()
    emails_job = db.get(SearchJob, emails_job.id)
    assert emails_job.status == "done" and emails_job.result_count == 1
    co = db.scalar(select(Company))
    assert co.email_statuses == {"info@chain.pk": "valid"}


def test_crawl_without_find_emails_queues_no_email_check(db, engine, monkeypatch):
    fake_crawler(monkeypatch, {"https://a.pk": site(emails=["info@a.pk"])})
    c = company(db, "A", website="https://a.pk")
    crawl = SearchJob(kind="enrich", query="q", location="l", source="crawler", params={"company_ids": [c.id]})
    db.add(crawl)
    db.commit()
    worker.run_job(crawl.id)
    assert db.scalar(select(SearchJob).where(SearchJob.kind == "emails")) is None

    # selected companies + find_emails: the email check gets the same company ids
    crawl2 = SearchJob(kind="enrich", query="q", location="l", source="crawler",
                       params={"company_ids": [c.id], "find_emails": True, "force": True})
    db.add(crawl2)
    db.commit()
    worker.run_job(crawl2.id)
    emails_job = db.scalar(select(SearchJob).where(SearchJob.kind == "emails"))
    assert emails_job.parent_job_id is None
    assert emails_job.params["company_ids"] == [c.id]


def test_hunter_never_asked_twice_for_a_domain(db, engine):
    # Hunter finds no one: nothing is saved on the company, but the lookup is remembered.
    a = company(db, "Chain A", website="https://chain.pk/a", domain="chain.pk")
    b = company(db, "Chain B", website="https://chain.pk/b", domain="chain.pk")
    run_emails(db, company_ids=[a.id, b.id])
    assert engine.hunter_calls == ["chain.pk"]  # two branches, one domain, one credit
    run_emails(db, company_ids=[a.id])
    run_emails(db, company_ids=[a.id], force=True)  # force = re-verify, not "pay again"
    assert engine.hunter_calls == ["chain.pk"]
    info = db.scalar(select(DomainInfo).where(DomainInfo.domain == "chain.pk"))
    assert info.hunter_checked_at is not None
    assert (info.email_pattern, info.pattern_source) == ("{first}", "hunter")  # Hunter's pattern kept


def test_hunter_generic_emails_are_not_people(db, engine):
    c = company(db, "Generic", website="https://generic.pk", domain="generic.pk")
    engine.hunter_people["generic.pk"] = [
        HunterPerson("info@generic.pk", None, None, None, 90, None, "generic"),
        HunterPerson("sales@generic.pk", "Sales", None, None, 80, None, "generic"),
        HunterPerson("amir.ali@generic.pk", "Amir", "Ali", "Owner", 88, None, "personal"),
    ]
    run_emails(db, company_ids=[c.id])
    assert sorted(contacts_by_name(db, c)) == ["Amir Ali"]
    assert set(db.get(Company, c.id).emails) == {"info@generic.pk", "sales@generic.pk", "amir.ali@generic.pk"}


def test_only_real_emails_teach_the_pattern(db, engine):
    c = company(db, "Learn", domain="learn.pk", contacts=[
        person("Jane Doe", "Owner", "jane.doe@learn.pk"),
        person("Old Guess", "Manager", "old.guess@learn.pk", email_status="catch_all"),
        person("Bad Mail", "Staff", "bad.mail@learn.pk", email_status="invalid"),
    ])
    guessed = contacts_by_name(db, c)["Old Guess"]
    guessed.email_source = "pattern"  # found by the finder in an earlier job
    db.commit()
    run_emails(db, company_ids=[c.id])
    assert engine.learn_calls == [("learn.pk", [("Jane", "Doe", "jane.doe@learn.pk")], "website")]


def test_worker_crawls_with_the_ssrf_safe_client(db, engine, monkeypatch):
    from app.enrich import crawler

    seen = []

    async def crawl(url, client=None, max_pages=None):
        seen.append(client)
        return site()

    monkeypatch.setattr(worker, "crawl_website", crawl)
    c = company(db, "Safe", website="https://safe.pk", domain="safe.pk")
    run_emails(db, company_ids=[c.id], crawl=True)
    assert len(seen) == 1
    assert isinstance(seen[0]._transport, crawler._SafeTransport)
