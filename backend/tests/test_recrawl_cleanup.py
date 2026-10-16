"""A good re-crawl replaces what earlier crawls found on the website (stale/junk emails and
untouched junk people), but never touches emails from the business listing or Hunter.io,
nor people the user has worked with."""
from sqlalchemy import select

from app import worker
from app.enrich import PersonFound
from app.models import Activity, Company, Contact, LeadList, ListItem
from tests.test_worker import company, crawler, enrich, make_search, site  # noqa: F401  (fixture)


def test_good_recrawl_drops_stale_website_emails_keeps_source_and_hunter(db, crawler):  # noqa: F811
    shop = company(
        "Shop", website="https://shop.pk", email="20info@shop.pk",
        emails=["20info@shop.pk", "owner@shop.pk", "boss@shop.pk", "old@shop.pk"],
        email_statuses={"20info@shop.pk": "unknown", "owner@shop.pk": "valid", "boss@shop.pk": "valid"},
        email_sources={"boss@shop.pk": "hunter"},
        raw={"email": "Owner@Shop.pk"},  # older row: no origin recorded, but the listing gives it
    )
    search = make_search(db, shop)
    crawler({"https://shop.pk": site(emails=["info@shop.pk"])})
    enrich(db, search, force=True)

    s = db.get(Company, shop.id)
    assert s.emails == ["owner@shop.pk", "boss@shop.pk", "info@shop.pk"]
    assert s.email == "owner@shop.pk"  # the old best email was dropped, so a new one is picked
    assert s.email_statuses == {"owner@shop.pk": "valid", "boss@shop.pk": "valid"}
    assert s.email_sources == {"boss@shop.pk": "hunter", "info@shop.pk": "website"}


def test_failed_recrawl_keeps_everything(db, crawler):  # noqa: F811
    shop = company("Shop", website="https://shop.pk", email="info@shop.pk", emails=["info@shop.pk"])
    search = make_search(db, shop)
    crawler({"https://shop.pk": site(ok=False, error="Website timed out")})
    enrich(db, search, force=True)
    s = db.get(Company, shop.id)
    assert s.emails == ["info@shop.pk"] and s.email == "info@shop.pk"


def test_good_recrawl_removes_only_untouched_junk_people(db, crawler):  # noqa: F811
    shop = company("Shop", website="https://shop.pk")
    search = make_search(db, shop)
    first = [
        PersonFound(full_name="Oral Surgery", title="Emergency Dentist"),  # junk, untouched
        PersonFound(full_name="David Weiner", title="Dr."),  # duplicate of the fuller name below
        PersonFound(full_name="Listed Person", title="Manager"),  # user put in a list
        PersonFound(full_name="Noted Person", title="Manager"),  # user wrote a note
        PersonFound(full_name="Contacted Person", title="Manager"),  # user changed status
        PersonFound(full_name="Mailed Person", title="Manager", email="mailed@shop.pk"),  # has email
    ]
    crawler({"https://shop.pk": site(people=first)})
    enrich(db, search)

    by_name = {c.full_name: c for c in db.scalars(select(Contact).where(Contact.company_id == shop.id))}
    lst = LeadList(name="Keep")
    db.add(lst)
    db.flush()
    db.add(ListItem(list_id=lst.id, contact_id=by_name["Listed Person"].id))
    db.add(Activity(company_id=shop.id, contact_id=by_name["Noted Person"].id, kind="note", text="call back"))
    by_name["Contacted Person"].lead_status = "contacted"
    db.commit()

    crawler({"https://shop.pk": site(people=[PersonFound(full_name="David J. Weiner", title="General Dentist")])})
    enrich(db, search, force=True)

    names = sorted(c.full_name for c in db.scalars(select(Contact).where(Contact.company_id == shop.id)))
    assert names == ["Contacted Person", "David J. Weiner", "Listed Person", "Mailed Person", "Noted Person"]


def test_failed_recrawl_keeps_people(db, crawler):  # noqa: F811
    shop = company("Shop", website="https://shop.pk")
    search = make_search(db, shop)
    crawler({"https://shop.pk": site(people=[PersonFound(full_name="Bilal Ahmed", title="Manager")])})
    enrich(db, search)
    crawler({"https://shop.pk": site(ok=False, error="Blocked (HTTP 403)")})
    enrich(db, search, force=True)
    assert [c.full_name for c in db.scalars(select(Contact).where(Contact.company_id == shop.id))] == ["Bilal Ahmed"]


def test_search_records_source_email_origin(db, monkeypatch):
    from app.sources import CompanyResult

    c = Company(name="X", source="osm", source_ref="n/1")
    db.add(c)
    db.commit()
    worker._upsert_company(db, "osm", CompanyResult(name="X", source_ref="n/1", email="A@x.pk; b@x.pk"))
    db.commit()
    assert db.get(Company, c.id).email_sources == {"a@x.pk": "source", "b@x.pk": "source"}
