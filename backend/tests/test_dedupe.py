"""Spotting and merging the same business found twice."""
from sqlalchemy import select

from app import dedupe, worker
from app.dedupe import find_all_duplicates, merge_all_duplicates, merge_companies, name_similarity, same_business
from app.models import Activity, Company, CompanySource, Contact, JobResult, LeadList, ListItem, SearchJob
from app.phones import apply_company_phone
from app.sources import SOURCES, CompanyResult, Source

LAT, LON = 30.2700, -97.7400


def company(name="Smile Dental", source="osm", ref="1", phone=None, lat=LAT, lon=LON, **kw) -> Company:
    c = Company(name=name, source=source, source_ref=ref, phone=phone, lat=lat, lon=lon, **kw)
    apply_company_phone(c)
    return c


def test_name_similarity():
    assert name_similarity("Smile Dental Clinic", "Smile Dental Clinic, LLC") == 1.0
    assert name_similarity("Smile Dental", "Smile Dental - Dr. Khan") >= 0.85
    assert name_similarity("Smile Dental", "Bright Dental") < 0.85
    assert name_similarity("Dental", "Smile Dental") < 0.85  # one shared word isn't enough


def test_same_phone_and_name():
    a = company(phone="+1 512-351-8010", ref="1")
    b = company(name="Smile Dental Clinic", source="google", ref="g1", phone="(512) 351-8010", lat=LAT + 0.005)
    assert same_business(a, b) == "same phone and name"


def test_same_phone_but_different_name_is_not_enough():
    a = company(name="Dr Ali Khan", phone="+1 512-351-8010")
    b = company(name="City Hospital", source="google", ref="g1", phone="+1 512-351-8010")
    assert same_business(a, b) is None


def test_same_domain_needs_the_same_place():
    a = company(name="McDonald's", domain="mcdonalds.com")
    near = company(name="McDonalds Restaurant", source="google", ref="g1", domain="mcdonalds.com", lat=LAT + 0.001)
    far = company(name="McDonald's", source="google", ref="g2", domain="mcdonalds.com", lat=LAT + 0.05)
    assert same_business(a, near) == "same website, same place"
    assert same_business(a, far) is None


def test_shared_domain_never_matches():
    a = company(name="A Bakery", domain="facebook.com")
    b = company(name="B Salon", source="google", ref="g1", domain="facebook.com")
    assert same_business(a, b) is None


def test_same_name_same_place():
    a = company(name="Smile Dental", ref="node/1")
    b = company(name="Smile Dental", ref="way/2", lat=LAT + 0.0005)
    assert same_business(a, b) == "same name, same place"
    far = company(name="Smile Dental", ref="way/3", lat=LAT + 0.01)
    assert same_business(a, far) is None


def test_two_google_places_need_to_be_very_close():
    a = company(source="google", ref="g1", phone="+1 512-351-8010")
    b = company(source="google", ref="g2", phone="+1 512-351-8010", lat=LAT + 0.005)
    assert same_business(a, b) is None


def test_merge_moves_everything_and_google_facts_win(db):
    old = company(name="Smile Dental", ref="node/1", phone="+1 512-351-8010", email="info@smile.com",
                  emails=["info@smile.com"], lead_status="contacted", opening_hours="Mo-Fr 09:00-17:00")
    old.contacts.append(Contact(full_name="Ali Khan", title="Owner"))
    new = company(name="Smile Dental Clinic", source="google", ref="g1", phone="(512) 351-8010",
                  rating=4.8, reviews_count=120, emails=["sales@smile.com"])
    new.contacts.append(Contact(full_name="ali khan", email="ali@smile.com"))
    new.contacts.append(Contact(full_name="Sara Ahmed"))
    lst = LeadList(name="Hot")
    job = SearchJob(kind="search", query="dentist", location="Austin", source="google")
    db.add_all([old, new, lst, job])
    db.flush()
    db.add_all([ListItem(list_id=lst.id, company_id=old.id), ListItem(list_id=lst.id, company_id=new.id),
                JobResult(job_id=job.id, company_id=new.id), Activity(company_id=new.id, kind="note", text="Called")])
    db.commit()
    new_id = new.id

    kept = merge_companies(db, old, new, "same phone and name")
    db.commit()

    assert kept.id == old.id and db.get(Company, new_id) is None
    assert kept.name == "Smile Dental Clinic" and kept.rating == 4.8  # Google's listing wins
    assert kept.opening_hours == "Mo-Fr 09:00-17:00"  # ...and OpenStreetMap fills the gaps
    assert (kept.source, kept.source_ref) == ("google", "g1")
    aliases = db.scalars(select(CompanySource)).all()
    assert [(a.company_id, a.source, a.source_ref) for a in aliases] == [(old.id, "osm", "node/1")]
    assert kept.emails == ["info@smile.com", "sales@smile.com"] and kept.lead_status == "contacted"
    people = sorted(c.full_name for c in kept.contacts)
    assert people == ["Ali Khan", "Sara Ahmed"]  # the two Ali Khans became one
    assert next(c for c in kept.contacts if c.full_name == "Ali Khan").email == "ali@smile.com"
    assert db.scalars(select(ListItem.company_id)).all() == [old.id]  # one entry, not two
    assert db.scalars(select(JobResult.company_id)).all() == [old.id]
    kinds = sorted(a.kind for a in db.scalars(select(Activity).where(Activity.company_id == old.id)))
    assert kinds == ["merge", "note"]


def test_find_and_merge_all(db):
    db.add_all([
        company(name="Smile Dental", ref="node/1", phone="+1 512-351-8010"),
        company(name="Smile Dental", source="google", ref="g1", phone="+1 512-351-8010"),
        company(name="Smile Dental", ref="way/2"),  # same name, same spot
        company(name="Bright Smiles", ref="node/9", lat=LAT + 0.02),
    ])
    db.commit()
    groups = find_all_duplicates(db)
    assert [[c.source_ref for c, _ in g] for g in groups] == [["node/1", "g1", "way/2"]]

    assert merge_all_duplicates(db) == 2
    assert sorted(c.name for c in db.scalars(select(Company))) == ["Bright Smiles", "Smile Dental"]
    assert find_all_duplicates(db) == []


# ----- during a search ------------------------------------------------------------------


class NamedSource(Source):
    label, description = "Fake", "For tests"

    def __init__(self, name, results):
        self.name, self.results = name, results

    def search(self, query, location, limit, progress):
        return self.results


def run_search(db, monkeypatch, source, results) -> SearchJob:
    monkeypatch.setitem(SOURCES, source, NamedSource(source, results))
    job = SearchJob(kind="search", query="dentist", location="Austin", source=source)
    db.add(job)
    db.commit()
    worker.run_job(job.id)
    db.expire_all()
    return db.get(SearchJob, job.id)


def test_search_merges_with_a_business_from_another_source(db, monkeypatch):
    osm = CompanyResult(name="Smile Dental", source_ref="node/1", phone="+1 512-351-8010", lat=LAT, lon=LON,
                        opening_hours="Mo-Fr 09:00-17:00")
    run_search(db, monkeypatch, "osm", [osm])
    google = CompanyResult(name="Smile Dental Clinic", source_ref="g1", phone="(512) 351-8010", country="United States",
                           lat=LAT, lon=LON, rating=4.7)
    job = run_search(db, monkeypatch, "google", [google])

    companies = db.scalars(select(Company)).all()
    assert len(companies) == 1 and job.result_count == 1
    assert "Merged 1 duplicate" in job.progress_message
    c = companies[0]
    assert (c.source, c.rating, c.opening_hours) == ("google", 4.7, "Mo-Fr 09:00-17:00")

    # Searching OpenStreetMap again updates the merged company (via the alias) without
    # overwriting Google's facts or making a new copy.
    osm.name, osm.rating = "Smile Dental (old name)", None
    job = run_search(db, monkeypatch, "osm", [osm])
    companies = db.scalars(select(Company)).all()
    assert len(companies) == 1 and job.result_count == 1
    assert companies[0].name == "Smile Dental Clinic" and companies[0].rating == 4.7
    assert db.scalar(select(JobResult.company_id).where(JobResult.job_id == job.id)) == c.id


def test_search_merges_two_copies_in_the_same_results(db, monkeypatch):
    results = [
        CompanyResult(name="Smile Dental", source_ref="node/1", lat=LAT, lon=LON),
        CompanyResult(name="Smile Dental", source_ref="way/2", lat=LAT + 0.0003, lon=LON, phone="+1 512-351-8010"),
    ]
    job = run_search(db, monkeypatch, "osm", results)
    companies = db.scalars(select(Company)).all()
    assert len(companies) == 1 and job.result_count == 1
    assert companies[0].phone_e164 == "+15123518010"
    assert db.scalars(select(JobResult.company_id).where(JobResult.job_id == job.id)).all() == [companies[0].id]


def test_cli_preview_changes_nothing(db, capsys):
    db.add_all([company(ref="node/1"), company(ref="way/2")])
    db.commit()
    dedupe.main([])
    assert "1 duplicate(s)" in capsys.readouterr().out
    db.expire_all()
    assert len(db.scalars(select(Company)).all()) == 2


def test_similar_names_next_door_are_different_shops():
    a = company(name="Home Slice Pizza", ref="node/1")
    b = company(name="More Home Slice Pizza", ref="node/2", lat=LAT + 0.0002)
    assert same_business(a, b) is None


def test_same_name_with_different_phones_is_not_merged():
    a = company(name="Brickell Dental", source="google", ref="g1", phone="+1 305-614-4423")
    b = company(name="Brickell Dental", source="google", ref="g2", phone="+1 305-377-8004", lat=LAT + 0.0003)
    assert same_business(a, b) is None
