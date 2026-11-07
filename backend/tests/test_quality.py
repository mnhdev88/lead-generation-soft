"""Lead quality: the do-not-contact list and bounced emails (app/blocklist.py)."""
from app import blocklist, worker
from app.emails import verify_emails
from app.models import Company, Contact, DomainInfo, SearchJob


def companies(db):
    acme = Company(name="Acme Dental", source="osm", source_ref="a", website="https://acme.com", domain="acme.com",
                   email="info@acme.com", emails=["info@acme.com", "jane@acme.com"])
    beta = Company(name="Beta Gym", source="osm", source_ref="b", website="https://beta.pk", domain="beta.pk")
    gamma = Company(name="Gamma Cafe", source="osm", source_ref="c", email="gamma@gmail.com")
    db.add_all([acme, beta, gamma])
    db.flush()
    db.add_all([
        Contact(company_id=acme.id, full_name="Jane Doe", email="jane@acme.com", email_status="catch_all",
                email_source="pattern", email_confidence=70),
        Contact(company_id=acme.id, full_name="Bob Roe", email="bob@acme.com", email_status="catch_all",
                email_source="pattern", email_confidence=70),
        Contact(company_id=beta.id, full_name="Ali Khan", email="ali@beta.pk", email_status="valid"),
    ])
    db.add(DomainInfo(domain="acme.com", email_pattern="{first}", pattern_confidence=60, pattern_source="website"))
    db.commit()
    return acme, beta, gamma


def names(client, path="/api/companies", **params):
    return sorted(c.get("name") or c.get("full_name") for c in client.get(path, params=params).json()["items"])


def test_parse_pasted_emails_and_domains():
    parsed = blocklist.parse_entries("Info@Acme.com, https://www.Beta.pk/contact\nbeta.pk;  not-a-thing  x@y")
    assert parsed.entries == [("email", "info@acme.com"), ("domain", "beta.pk")]
    assert parsed.invalid == ["not-a-thing", "x@y"]


def test_domain_hides_its_businesses_and_people_everywhere(client, db):
    companies(db)
    r = client.post("/api/quality/do-not-contact", json={"text": "acme.com\nacme.com\nnonsense", "reason": "client"})
    assert r.status_code == 201
    assert r.json() == {"added": 1, "already": 0, "invalid": ["nonsense"]}
    assert names(client) == ["Beta Gym", "Gamma Cafe"]
    assert names(client, "/api/people") == ["Ali Khan"]
    assert "Acme" not in client.get("/api/companies/export.csv").text
    # Shown on request, with the reason.
    shown = {c["name"]: c for c in client.get("/api/companies", params={"include_blocked": True}).json()["items"]}
    assert shown["Acme Dental"]["do_not_contact"] == "acme.com (Current client)"
    assert shown["Acme Dental"]["contacts"][0]["do_not_contact"] == "acme.com (Current client)"
    assert shown["Beta Gym"]["do_not_contact"] is None
    listed = client.get("/api/quality/do-not-contact").json()
    assert [(e["kind"], e["value"], e["matches"]) for e in listed] == [("domain", "acme.com", 1)]
    client.delete(f"/api/quality/do-not-contact/{listed[0]['id']}")
    assert len(names(client)) == 3


def test_email_and_email_domain_entries(client, db):
    companies(db)
    client.post("/api/quality/do-not-contact", json={"text": "ali@beta.pk gmail.com"})
    assert names(client) == ["Acme Dental", "Beta Gym"]  # Gamma's email is @gmail.com
    assert names(client, "/api/people") == ["Bob Roe", "Jane Doe"]


def test_one_business_by_its_button(client, db):
    _, beta, _ = companies(db)
    r = client.post(f"/api/quality/do-not-contact/company/{beta.id}", json={"reason": "said_no"})
    assert r.status_code == 201 and r.json()["label"] == "Beta Gym" and r.json()["matches"] == 1
    assert names(client) == ["Acme Dental", "Gamma Cafe"]
    assert client.get(f"/api/companies/{beta.id}").json()["do_not_contact"] == "Beta Gym (Said no)"
    assert client.delete(f"/api/quality/do-not-contact/company/{beta.id}").status_code == 204
    assert len(names(client)) == 3


def test_jobs_skip_blocked_businesses(client, db):
    acme, beta, _ = companies(db)
    client.post("/api/quality/do-not-contact", json={"text": "acme.com"})
    job = SearchJob(kind="enrich", query="q", location="", source="crawler",
                    params={"company_ids": [acme.id, beta.id]})
    db.add(job)
    db.commit()
    assert [c.name for c in worker._job_targets(db, job)] == ["Beta Gym"]


def test_bounced_email_is_invalid_everywhere_and_weakens_the_pattern(client, db):
    acme, _, _ = companies(db)
    r = client.post("/api/quality/bounced", json={"text": "JANE@acme.com, info@acme.com\nnope"})
    assert r.json() == {"emails": 2, "companies": 1, "people": 1, "patterns_weakened": 1, "invalid": ["nope"]}
    db.expire_all()
    c = db.get(Company, acme.id)
    assert c.email_statuses == {"info@acme.com": "invalid", "jane@acme.com": "invalid"}
    assert c.email is None  # every email of it bounced
    people = {p.full_name: p for p in c.contacts}
    assert (people["Jane Doe"].email_status, people["Jane Doe"].email_confidence) == ("invalid", 0)
    assert people["Bob Roe"].email_confidence == 30  # made with the same pattern: less trusted
    info = db.query(DomainInfo).filter_by(domain="acme.com").one()
    assert (info.pattern_confidence, info.pattern_source) == (20, "guess")
    assert all("Verified email" not in r and "Catch-all email" not in r for r in c.score_reasons)


def test_bounced_email_stays_invalid_when_checked_again(client, db):
    client.post("/api/quality/bounced", json={"text": "jane@acme.com"})
    result = verify_emails(["jane@acme.com"], force=True, use_smtp=False)["jane@acme.com"]
    assert (result.status, result.reason) == ("invalid", "Bounced when you emailed it")


def test_mail_provider_from_saved_mail_servers(client, db):
    from app.emails.hosting import mail_provider

    assert mail_provider(["acme-com.mail.protection.outlook.com"]) == "Microsoft 365"
    assert mail_provider(["mx.acme.co.uk"]) == "Own or web-host mail server"
    assert mail_provider([]) is None
    acme, beta, gamma = companies(db)
    db.add(DomainInfo(domain="beta.pk", mx_hosts=["aspmx.l.google.com"], has_mx=True))
    db.commit()
    got = {c["name"]: c["mail_provider"] for c in client.get("/api/companies").json()["items"]}
    assert got == {"Acme Dental": None, "Beta Gym": "Google Workspace",
                   "Gamma Cafe": "Free email address (Gmail, Yahoo…)"}
