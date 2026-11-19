"""Duplicates: one rule for "the same person", people saved twice, a business that only
matches its twin after the website crawl, the Quality page's duplicates tools, exports that
remember what went out, and lists that say a lead is already in another list."""
from sqlalchemy import select

from app import dedupe
from app.enrich import PersonFound
from app.models import Activity, Company, Contact, LeadList, ListItem
from app.people.matching import find_same_person, name_key, same_name
from app.phones import apply_company_phone
from tests.test_worker import crawler, enrich, make_search, site  # noqa: F401  (fixture)

LAT, LON = 30.2700, -97.7400


# ----- the same person ---------------------------------------------------------------


def test_name_key_ignores_titles_middle_names_and_suffixes():
    assert name_key("Dr. John A. Smith, MD") == ("john", "smith")
    assert name_key("john smith") == ("john", "smith")
    assert name_key("Mr. José  García Jr.") == ("jose", "garcia")
    assert name_key("John (Johnny) Smith DDS") == ("john", "smith")
    assert name_key("Dr. Smith") is None  # no first name: never matched on name alone
    assert name_key("") is None


def test_same_name():
    assert same_name("Dr. David J. Weiner", "David Weiner")
    assert not same_name("David Weiner", "Daniel Weiner")
    assert not same_name("J. Smith", "John Smith")  # an initial is not enough
    assert not same_name("Smith", "Smith")


def test_find_same_person_order_and_linkedin_conflict():
    a = Contact(full_name="John Smith", email="john@x.com", linkedin_url="https://www.linkedin.com/in/john-smith-1")
    b = Contact(full_name="Sara Khan", email="sara@x.com")
    people = [a, b]
    assert find_same_person(people, "Johnny", "JOHN@x.com") is a  # same email
    assert find_same_person(people, "Dr. John Q. Smith", None) is a  # same first + last name
    assert find_same_person(people, "Someone", None, "http://linkedin.com/in/john-smith-1/") is a
    # Same name, but a different LinkedIn profile: another person called John Smith.
    assert find_same_person(people, "John Smith", None, "https://www.linkedin.com/in/john-smith-2") is None
    assert find_same_person(people, "Sara Ali", None) is None
    assert find_same_person(people, "John Smith", None, exclude=a) is None


# ----- people saved twice --------------------------------------------------------------


def _company(db, name="Smile Dental", ref="1", **kw) -> Company:
    c = Company(name=name, source="osm", source_ref=ref, lat=LAT, lon=LON, **kw)
    apply_company_phone(c)
    db.add(c)
    db.flush()
    return c


def test_merge_duplicate_people_keeps_the_older_and_moves_lists_notes_sources(db):
    c = _company(db)
    old = Contact(full_name="John Smith", title="Owner", source="website", confirmed_by=["website"])
    new = Contact(full_name="Dr. John A. Smith", email="john@smile.com", source="hunter",
                  lead_status="contacted", is_decision_maker=True)
    other = Contact(full_name="Sara Khan", source="website")
    c.contacts.extend([old, new, other])
    db.flush()
    lst = LeadList(name="Call")
    db.add(lst)
    db.flush()
    db.add(ListItem(list_id=lst.id, contact_id=new.id))
    db.add(Activity(company_id=c.id, contact_id=new.id, kind="note", text="call back"))
    db.commit()

    assert [(k.id, d.id) for k, d in dedupe.duplicate_people(c)] == [(old.id, new.id)]
    assert dedupe.merge_all_duplicate_people(db) == 1

    db.expire_all()
    people = db.scalars(select(Contact).where(Contact.company_id == c.id).order_by(Contact.id)).all()
    assert [p.full_name for p in people] == ["John Smith", "Sara Khan"]
    john = people[0]
    assert john.email == "john@smile.com" and john.title == "Owner"  # filled, not overwritten
    assert john.lead_status == "contacted" and john.is_decision_maker
    assert john.confirmed_by == ["website", "hunter"]  # two sources: confirmed
    assert db.scalar(select(ListItem.contact_id).where(ListItem.list_id == lst.id)) == john.id
    assert db.scalar(select(Activity.contact_id).where(Activity.text == "call back")) == john.id
    assert dedupe.find_all_duplicate_people(db) == []


def test_website_crawl_does_not_add_the_same_person_again(db, crawler):  # noqa: F811
    shop = Company(name="Shop", source="osm", source_ref="shop", website="https://shop.pk")
    shop.contacts.append(Contact(full_name="Ayesha Khan", email="ayesha@shop.pk", source="hunter"))
    search = make_search(db, shop)
    crawler({"https://shop.pk": site(people=[PersonFound(full_name="Dr. Ayesha Khan", title="Dentist")])})
    enrich(db, search)
    people = db.scalars(select(Contact).where(Contact.company_id == shop.id)).all()
    assert len(people) == 1 and people[0].title == "Dentist"
    assert people[0].confirmed_by == ["hunter", "website"]


# ----- a business that matches its twin only after the crawl ----------------------------


def test_crawl_that_finds_the_phone_merges_the_business_with_its_twin(db, crawler):  # noqa: F811
    google = Company(name="Smile Dental", source="google", source_ref="g1", phone="+1 512-351-8010",
                     lat=LAT, lon=LON, country="US")
    apply_company_phone(google)
    db.add(google)
    db.commit()
    # ~550 m away and no phone: not the same business when it was saved.
    osm = Company(name="Smile Dental Clinic", source="osm", source_ref="o1", website="https://smile.example",
                  lat=LAT + 0.005, lon=LON, country="US")
    assert dedupe.find_duplicate(db, osm) is None
    search = make_search(db, osm)
    osm_id = osm.id

    crawler({"https://smile.example": site(phones=["+1 512-351-8010"])})
    job = enrich(db, search)

    assert db.get(Company, osm_id) is None  # merged into the older one
    kept = db.get(Company, google.id)
    assert kept.website == "https://smile.example"
    assert "merged 1 duplicate" in job.progress_message


# ----- Quality page: duplicates ----------------------------------------------------------


def test_duplicates_endpoints_preview_then_merge(client, db):
    a = _company(db, "Smile Dental", ref="1", phone="+1 512-351-8010")
    _company(db, "Smile Dental Clinic", ref="2", phone="(512) 351-8010")
    a.contacts.extend([Contact(full_name="John Smith"), Contact(full_name="Dr. John Smith")])
    db.commit()

    r = client.get("/api/quality/duplicates")
    assert r.status_code == 200
    body = r.json()
    assert body["business_copies"] == 1 and body["people_copies"] == 1
    assert [b["name"] for b in body["businesses"][0]] == ["Smile Dental", "Smile Dental Clinic"]
    assert body["businesses"][0][1]["reason"] == "same phone and name"
    assert [p["name"] for p in body["people"][0]["pairs"][0]] == ["John Smith", "Dr. John Smith"]
    assert db.scalar(select(Company.id).where(Company.source_ref == "2")) is not None  # preview changed nothing

    r = client.post("/api/quality/duplicates/merge", headers={"X-Requested-With": "leadgen"})
    assert r.status_code == 200 and r.json() == {"businesses": 1, "people": 1}
    assert client.get("/api/quality/duplicates").json()["business_copies"] == 0


# ----- exports and lists -----------------------------------------------------------------


def test_export_remembers_what_went_out_and_can_skip_it(client, db):
    a = _company(db, "A", ref="a")
    _company(db, "B", ref="b")
    db.commit()
    r = client.get("/api/companies/export.csv", params={"q": "A"})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Company, a.id).exported_at is not None

    page = client.get("/api/companies", params={"exported": "true"}).json()
    assert [c["name"] for c in page["items"]] == ["A"]
    r = client.get("/api/companies/export.csv", params={"exported": "false"})
    lines = r.text.strip().splitlines()
    assert len(lines) == 2 and lines[1].startswith("B,")  # header + B only


def test_people_export_has_one_row_per_email(client, db):
    a = _company(db, "A", ref="a")
    b = _company(db, "A Branch", ref="b")
    a.contacts.append(Contact(full_name="Ali Khan", email="ali@a.com"))
    b.contacts.append(Contact(full_name="Ali Khan", email="ALI@a.com"))
    b.contacts.append(Contact(full_name="No Email"))
    db.commit()
    r = client.get("/api/people/export.csv")
    rows = r.text.strip().splitlines()[1:]
    assert len(rows) == 2 and sum("ali@a.com" in row.lower() for row in rows) == 1
    people = client.get("/api/people", params={"exported": "false"}).json()["items"]
    assert [p["full_name"] for p in people] == ["Ali Khan"]  # the left-out copy was not exported


def test_adding_to_a_list_says_how_many_are_in_another_list(client, db):
    a = _company(db, "A", ref="a")
    b = _company(db, "B", ref="b")
    db.commit()
    first = client.post("/api/lists", json={"name": "First"}).json()
    second = client.post("/api/lists", json={"name": "Second"}).json()
    client.post(f"/api/lists/{first['id']}/items", json={"company_ids": [a.id]})
    r = client.post(f"/api/lists/{second['id']}/items", json={"company_ids": [a.id, b.id]})
    assert r.json() == {"changed": 2, "in_other_lists": 1}
