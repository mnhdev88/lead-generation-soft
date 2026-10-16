"""US PPP loan data: importing the SBA files and finding a company's loan."""
import sqlite3
from datetime import datetime

import httpx
import pytest

from app import config, ppp
from app.models import Company

# The real header of all 14 SBA files (53 columns).
HEADER = (
    "LoanNumber,DateApproved,SBAOfficeCode,ProcessingMethod,BorrowerName,BorrowerAddress,BorrowerCity,"
    "BorrowerState,BorrowerZip,LoanStatusDate,LoanStatus,Term,SBAGuarantyPercentage,InitialApprovalAmount,"
    "CurrentApprovalAmount,UndisbursedAmount,FranchiseName,ServicingLenderLocationID,ServicingLenderName,"
    "ServicingLenderAddress,ServicingLenderCity,ServicingLenderState,ServicingLenderZip,RuralUrbanIndicator,"
    "HubzoneIndicator,LMIIndicator,BusinessAgeDescription,ProjectCity,ProjectCountyName,ProjectState,ProjectZip,"
    "CD,JobsReported,NAICSCode,Race,Ethnicity,UTILITIES_PROCEED,PAYROLL_PROCEED,MORTGAGE_INTEREST_PROCEED,"
    "RENT_PROCEED,REFINANCE_EIDL_PROCEED,HEALTH_CARE_PROCEED,DEBT_INTEREST_PROCEED,BusinessType,"
    "OriginatingLenderLocationID,OriginatingLender,OriginatingLenderCity,OriginatingLenderState,Gender,Veteran,"
    "NonProfit,ForgivenessAmount,ForgivenessDate"
).split(",")
assert len(HEADER) == 53


def loan(number, name, address, city="Austin", state="TX", zip_code="78704", jobs="5", date="04/30/2020",
         method="PPP", amount="20833", business_type="Limited  Liability Company(LLC)", **extra) -> dict:
    row = {
        "LoanNumber": number, "DateApproved": date, "SBAOfficeCode": "0759", "ProcessingMethod": method,
        "BorrowerName": name, "BorrowerAddress": address, "BorrowerCity": city, "BorrowerState": state,
        "BorrowerZip": zip_code, "LoanStatus": "Paid in Full", "Term": "24", "SBAGuarantyPercentage": "100",
        "InitialApprovalAmount": amount, "CurrentApprovalAmount": amount, "UndisbursedAmount": "0",
        "ServicingLenderName": "Bank of America, National Association", "ProjectState": state,
        "ProjectZip": zip_code, "JobsReported": jobs, "NAICSCode": "722511", "BusinessType": business_type,
    }
    row.update(extra)
    return row


ROWS = [
    # Two shops with nearly the same name in one ZIP (dedupe.py has the same example).
    loan("1000000001", "HOME SLICE PIZZA", "1415 S Congress Ave", jobs="38", zip_code="78704-3517"),
    loan("1000000002", "MORE HOME SLICE PIZZA LLC", "1421 S CONGRESS AVE", jobs="12"),
    # A person-named practice.
    loan("1000000003", "JOHN SMITH DDS PC", "2200 S Lamar Blvd Ste 100", jobs="9", business_type="Corporation"),
    # First and second draw of one borrower.
    loan("1000000004", "ACME PLUMBING, LLC", "500 W Oltorf St", jobs="8", date="05/01/2020"),
    loan("1000000005", "ACME PLUMBING LLC", "500 W OLTORF ST", jobs="11", date="02/03/2021", method="PPS",
         amount="25100.5"),
    # Two different borrowers that fit "Lone Star Tacos" equally well.
    loan("1000000006", "LONE STAR TACOS EAST", "N/A"),
    loan("1000000007", "LONE STAR TACOS WEST", ""),
    # Doing business as...
    loan("1000000008", "RIVERA HOLDINGS LLC DBA TACO RIVERA", "77 E Riverside Dr", jobs="0"),
    # Same name and ZIP but another state: not the same business.
    loan("1000000009", "BLUE BONNET BAKERY", "10 Main St", state="NM"),
    # Privacy-redacted and "not available" rows can't be matched and are skipped.
    loan("1000000010", "Exemption 6", "", city="", state="", zip_code=""),
    loan("1000000011", "NOT AVAILABLE", "N/A", city="N/A", state="", zip_code=""),
    # A non-profit whose business type has the files' odd 0xBF byte.
    loan("1000000012", "AUSTIN FOOD PANTRY", "300 E 6th St", zip_code="78701", jobs="",
         business_type="501(c)3 \xbf Non Profit"),
    # Another state, for the --states filter.
    loan("2000000001", "PEACH STATE PAINTING INC", "12 Peachtree St", city="ATLANTA", state="GA", zip_code="30303"),
]


def csv_bytes(rows=ROWS) -> bytes:
    """The rows as the SBA writes them: Windows-1252 text, CRLF, quotes only where needed."""
    import csv
    import io

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for row in rows:
        writer.writerow([row.get(column, "") for column in HEADER])
    return out.getvalue().encode("cp1252")


@pytest.fixture
def ppp_path(tmp_path, monkeypatch):
    path = tmp_path / "ppp.db"
    monkeypatch.setattr(config, "PPP_DB_PATH", path)
    return path


@pytest.fixture
def ppp_db(ppp_path, tmp_path):
    """A small PPP database made from ROWS."""
    csv_file = tmp_path / "public_up_to_150k_1_240930.csv"
    csv_file.write_bytes(csv_bytes())
    ppp.import_files([str(csv_file)])
    return ppp_path


def loans_in(path) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT loan_number, name, address, zip5, jobs, date_approved, draw, business_type "
                            "FROM loans ORDER BY loan_number").fetchall()


def google_company(name, address, **kw) -> Company:
    """A company as the Google source saves it (raw = the Google place)."""
    zip_code = address.split(", TX ")[-1][:5] if ", TX " in address else None
    raw = {"addressComponents": [
        {"longText": "Texas", "shortText": "TX", "types": ["administrative_area_level_1", "political"]},
        {"longText": "United States", "shortText": "US", "types": ["country", "political"]},
    ] + ([{"longText": zip_code, "shortText": zip_code, "types": ["postal_code"]}] if zip_code else [])}
    return Company(name=name, address=address, country="United States", source="google",
                   source_ref=name, raw=raw, **kw)


# ----- importing ---------------------------------------------------------------------------


def test_import_keeps_what_we_need(ppp_db):
    rows = {r[0]: r for r in loans_in(ppp_db)}
    assert "1000000010" not in rows and "1000000011" not in rows  # redacted names skipped
    assert rows["1000000001"][3] == "78704"  # ZIP+4 -> 5 digits
    assert rows["1000000005"][4:7] == (11, "2021-02-03", "second")  # PPS = second draw
    assert rows["1000000004"][6] == "first"
    assert rows["1000000006"][2] is None  # 'N/A' address
    assert rows["1000000008"][4] is None  # 0 jobs = unknown
    assert rows["1000000012"][7] == "501(c)3 \xbf Non Profit"  # Latin-1 byte read fine
    assert ppp.available()


def test_import_states_filter(ppp_path, tmp_path):
    csv_file = tmp_path / "public_150k_plus_240930.csv"
    csv_file.write_bytes(csv_bytes())
    ppp.import_files([str(csv_file)], states={"GA"})
    assert [r[0] for r in loans_in(ppp_path)] == ["2000000001"]


def test_finished_files_are_skipped_and_interrupted_ones_redone(ppp_path, tmp_path, capsys):
    csv_file = tmp_path / "part.csv"
    csv_file.write_bytes(csv_bytes())
    ppp.import_files([str(csv_file)])
    ppp.import_files([str(csv_file)])
    assert "already imported" in capsys.readouterr().out
    assert len(loans_in(ppp_path)) == 11  # not twice

    # A download that breaks off halfway leaves the file unfinished...
    conn = ppp._connect_rw()
    data = csv_bytes()
    with pytest.raises(IOError):
        ppp.import_stream(conn, [data[: len(data) // 2]], "part.csv", total_bytes=len(data), progress=lambda _: None)
    assert not ppp.is_imported(conn, "part.csv", None)
    conn.close()
    # ...and the next run imports it again from the start, without doubled loans.
    ppp.import_files([str(csv_file)])
    assert len(loans_in(ppp_path)) == 11

    # More states later: the file is imported again with both.
    csv2 = tmp_path / "states.csv"
    csv2.write_bytes(csv_bytes())
    ppp.import_files([str(csv2)], states={"GA"})
    ppp.import_files([str(csv2)], states={"GA"})  # skipped
    ppp.import_files([str(csv2)], states={"NM"})
    with sqlite3.connect(ppp_path) as conn:
        assert conn.execute("SELECT states FROM files WHERE name = 'states.csv'").fetchone()[0] == "GA,NM"


def test_not_available_without_database(ppp_path, capsys):
    assert not ppp.available()
    assert ppp.match_company(google_company("Home Slice Pizza", "1415 S Congress Ave, Austin, TX 78704, USA")) is None
    ppp.main(["stats"])
    assert "No PPP database yet" in capsys.readouterr().out
    assert not ppp_path.exists()  # looking never creates the file


def test_stats(ppp_db, capsys):
    ppp.main(["stats"])
    out = capsys.readouterr().out
    assert "11 loans from 1 of 1 file(s)" in out and "Ready for matching: yes" in out


# ----- where a company is ------------------------------------------------------------------


def test_us_location_from_google_data():
    c = google_company("Home Slice Pizza", "1415 S Congress Ave, Austin, TX 78704, USA")
    assert ppp.us_location(c) == ("TX", "78704")


def test_us_location_from_openstreetmap_tags():
    c = Company(name="Joe's", source="osm", source_ref="n/1", address="12 Main St, Austin, 78701",
                raw={"addr:housenumber": "12", "addr:street": "Main St", "addr:postcode": "78701-2201",
                     "addr:state": "Texas"})
    assert ppp.us_location(c) == ("TX", "78701")


def test_us_location_from_address_text():
    c = Company(name="X", source="osm", source_ref="n/2", address="99 Elm St, Dallas, TX 75201")
    assert ppp.us_location(c) == ("TX", "75201")
    c.address = "99 Elm St, Dallas"  # no state + ZIP and no country: can't tell
    assert ppp.us_location(c) == (None, None)
    c.country = "US"
    c.raw = {"addr:postcode": "75201"}
    assert ppp.us_location(c) == (None, "75201")


def test_us_location_outside_the_us():
    pk = Company(name="X", source="google", source_ref="g", country="Pakistan",
                 address="Shop 5, Block 2, Karachi 75300", raw={})
    assert ppp.us_location(pk) == (None, None)
    ca = Company(name="X", source="google", source_ref="g", country="CA", address="1 King St W, Toronto, ON M5H 1A1")
    assert ppp.us_location(ca) == (None, None)


def test_street_parts():
    assert ppp.street_parts("1415 S Congress Ave, Austin, TX 78704, USA") == ("1415", "congress")
    assert ppp.street_parts("1415 SOUTH CONGRESS AVENUE STE 100") == ("1415", "congress")
    assert ppp.street_parts("PO BOX 12") == (None, None)
    assert ppp.street_parts("N/A") == (None, None)


def test_address_parts_keep_the_unit():
    assert ppp.address_parts("1200 Main St Suite 150, Austin, TX 78757") == ("1200", "main", "150")
    assert ppp.address_parts("1200 MAIN ST STE 150") == ("1200", "main", "150")
    assert ppp.address_parts("1200 Main St, Ste. 150, Austin") == ("1200", "main", "150")
    assert ppp.address_parts("1200 Main St #B") == ("1200", "main", "b")
    assert ppp.address_parts("1200 Main St, Austin, TX 78757") == ("1200", "main", None)
    # ", FL 33101" is a state and ZIP, not a floor
    assert ppp.address_parts("10 Ocean Dr, FL 33101") == ("10", "ocean", None)


# ----- matching ----------------------------------------------------------------------------


def test_same_name_wins_over_a_similar_name(ppp_db):
    found = ppp.match_company(google_company("Home Slice Pizza", "1415 S Congress Ave, Austin, TX 78704, USA"))
    assert found["name"] == "HOME SLICE PIZZA" and found["jobs"] == 38
    assert found["match"] == "same name, street address and ZIP" and found["confidence"] == "high"
    assert found["zip"] == "78704" and found["state"] == "TX" and found["loans"] == 1

    more = ppp.match_company(google_company("More Home Slice Pizza", "1421 S Congress Ave, Austin, TX 78704, USA"))
    assert more["name"] == "MORE HOME SLICE PIZZA LLC" and more["jobs"] == 12


def test_name_only_match_needs_exactly_the_same_words(ppp_db):
    # "Services" is filler, so "Acme Plumbing Services" is "ACME PLUMBING LLC"...
    no_number = google_company("Acme Plumbing Services", "Austin, TX 78704, USA")
    found = ppp.match_company(no_number)
    assert found["match"] == "same name and ZIP" and found["confidence"] == "medium"
    # ...also further along the same street (moved), but never on another street.
    elsewhere = google_company("Acme Plumbing Services", "999 W Oltorf St, Austin, TX 78704, USA")
    assert ppp.match_company(elsewhere)["match"] == "same name and ZIP (different house number or suite)"
    other_street = google_company("Acme Plumbing Services", "500 Barton Springs Rd, Austin, TX 78704, USA")
    assert ppp.match_company(other_street) is None
    # A similar name without the street address isn't enough.
    assert ppp.match_company(google_company("Acme Plumbing Repair", "Austin, TX 78704, USA")) is None


def test_first_and_second_draw(ppp_db):
    found = ppp.match_company(google_company("Acme Plumbing", "500 W Oltorf St, Austin, TX 78704, USA"))
    assert found["loans"] == 2
    assert (found["jobs"], found["date_approved"], found["draw"]) == (11, "2021-02-03", "second")
    assert found["amount"] == 25100.5 and found["loan_number"] == "1000000005"


def test_person_named_practice(ppp_db):
    # The same practice under the dentist's name matches ("PC" is only the legal form)...
    found = ppp.match_company(google_company("John Smith, D.D.S.", "2200 S Lamar Blvd, Austin, TX 78704, USA"))
    assert found["name"] == "JOHN SMITH DDS PC" and found["jobs"] == 9
    # ...a brand name sharing only the surname doesn't, even at the same address (could be
    # another dentist in the building), and neither does another John Smith business.
    assert ppp.match_company(google_company("Smith Family Dentistry", "2200 S Lamar Blvd, Austin, TX 78704, USA")) is None
    assert ppp.match_company(google_company("John Smith CPA", "4000 S 1st St, Austin, TX 78704, USA")) is None


def test_same_street_address_and_somewhat_similar_name(ppp_db):
    found = ppp.match_company(google_company("Home Slice", "1415 S Congress Ave, Austin, TX 78704, USA"))
    assert found["name"] == "HOME SLICE PIZZA"
    found = ppp.match_company(google_company("Acme Plumbers", "500 W Oltorf St, Austin, TX 78704, USA"))
    assert found["match"] == "same street address and similar name" and found["similarity"] == 0.77
    # The same name without the street address isn't enough.
    assert ppp.match_company(google_company("Acme Plumbers", "Austin, TX 78704, USA")) is None


def test_two_equally_good_borrowers_is_no_match(ppp_db):
    assert ppp.match_company(google_company("Lone Star Tacos", "Austin, TX 78704, USA")) is None


def test_doing_business_as_name(ppp_db):
    found = ppp.match_company(google_company("Taco Rivera", "77 E Riverside Dr, Austin, TX 78704, USA"))
    assert found["name"] == "RIVERA HOLDINGS LLC DBA TACO RIVERA"
    assert found["jobs"] is None  # reported 0 = unknown


def test_state_must_agree(ppp_db):
    assert ppp.match_company(google_company("Blue Bonnet Bakery", "10 Main St, Austin, TX 78704, USA")) is None


def test_unknown_name_in_zip(ppp_db):
    assert ppp.match_company(google_company("Bright Smile Dental", "1 Main St, Austin, TX 78704, USA")) is None


# ----- saving the result on companies ------------------------------------------------------


def test_apply_ppp(ppp_db):
    c = google_company("Home Slice Pizza", "1415 S Congress Ave, Austin, TX 78704, USA")
    assert ppp.apply_ppp(c) is True
    assert c.ppp["jobs"] == 38 and isinstance(c.ppp_checked_at, datetime)

    nobody = google_company("Bright Smile Dental", "1 Main St, Austin, TX 78704, USA")
    assert ppp.apply_ppp(nobody) is False
    assert nobody.ppp == {} and nobody.ppp_checked_at is not None

    # Outside the US: marked "not applicable" (with the time), so it isn't looked at again.
    abroad = Company(name="Home Slice Pizza", source="google", source_ref="x", country="Pakistan", raw={})
    assert ppp.apply_ppp(abroad) is False
    assert abroad.ppp == {"not_applicable": True} and abroad.ppp_checked_at is not None


def test_apply_ppp_without_database_does_nothing(ppp_path):
    c = google_company("Home Slice Pizza", "1415 S Congress Ave, Austin, TX 78704, USA", ppp={"jobs": 3})
    assert ppp.apply_ppp(c) is False
    assert c.ppp == {"jobs": 3} and c.ppp_checked_at is None


def test_match_saved_companies(db, ppp_db):
    db.add_all([
        google_company("Home Slice Pizza", "1415 S Congress Ave, Austin, TX 78704, USA"),
        google_company("Bright Smile Dental", "1 Main St, Austin, TX 78704, USA"),
        Company(name="Karachi Dental", source="google", source_ref="pk", country="Pakistan", raw={}),
    ])
    db.commit()
    assert ppp.match_saved_companies(db) == (2, 1)
    pizza = db.query(Company).filter_by(name="Home Slice Pizza").one()
    assert pizza.ppp["jobs"] == 38 and pizza.ppp_checked_at is not None
    assert pizza.score is not None  # rescored

    assert ppp.match_saved_companies(db) == (0, 0)  # everything checked already
    assert ppp.match_saved_companies(db, only_unchecked=False) == (2, 1)


# ----- downloading -------------------------------------------------------------------------


def test_download_streams_files_and_resumes(ppp_path, monkeypatch, capsys):
    monkeypatch.setattr(ppp, "RETRY_WAIT", 0)
    base = "https://data.sba.gov/sites/default/files/distribution/SBA-OCA-2022-07-001/"
    catalog = {"dataset": [
        {"identifier": "OTHER", "distribution": [{"downloadURL": "https://example.com/other.csv"}]},
        {"identifier": "SBA-OCA-2022-07-001", "distribution": [
            {"downloadURL": base + "ppp-data-dictionary.xlsx"},
            {"downloadURL": base + "public_150k_plus_250101.csv"},
            {"downloadURL": base + "public_up_to_150k_1_250101.csv"},
        ]},
    ]}
    big = csv_bytes(ROWS[:3])
    small = csv_bytes(ROWS[3:])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        calls.append(name)
        if name == "data.json":
            return httpx.Response(200, json=catalog)
        if name == "public_150k_plus_250101.csv":
            return httpx.Response(200, content=big)
        if name == "public_up_to_150k_1_250101.csv":
            if calls.count(name) == 1:
                return httpx.Response(500)  # fails once, works on the retry
            return httpx.Response(200, content=small)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert ppp.download(client=client) == 11
    assert calls == ["data.json", "public_150k_plus_250101.csv", "public_up_to_150k_1_250101.csv",
                     "public_up_to_150k_1_250101.csv"]
    assert ppp.available()

    calls.clear()
    assert ppp.download(client=client) == 0  # both finished: nothing downloaded again
    assert calls == ["data.json"]
    assert "already imported" in capsys.readouterr().out


def test_download_gives_up_after_retries(ppp_path, monkeypatch, capsys):
    monkeypatch.setattr(ppp, "RETRY_WAIT", 0)
    monkeypatch.setattr(ppp, "KNOWN_URLS", ["https://data.sba.gov/x/public_150k_plus_240930.csv"])
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    assert ppp.download(client=client) == 0
    out = capsys.readouterr().out
    assert "using the known file list" in out and "These files failed: public_150k_plus_240930.csv" in out
    assert not ppp.available()


# ----- strict matching: the cases that used to give another business's headcount ----------

TRAP_ROWS = [
    # One office building, four tenants (suites differ).
    loan("3000000001", "MAIN STREET ORTHODONTICS PLLC", "1200 Main St Ste 300", zip_code="78757", jobs="25"),
    loan("3000000002", "AUSTIN FAMILY MEDICINE PA", "1200 Main St Ste 100", zip_code="78757", jobs="45"),
    loan("3000000003", "BRIGHT SMILES PEDIATRIC DENTISTRY", "1200 Main St Ste 400", zip_code="78757", jobs="20"),
    loan("3000000004", "LAKESIDE HOLDINGS LLC", "1200 Main St Ste 210", zip_code="78757", jobs="80"),
    # Name contained in a longer one, PO box; a sole proprietor; another franchise area.
    loan("3000000005", "AUSTIN DENTAL SPECIALISTS GROUP", "PO BOX 4411", zip_code="78757", jobs="60"),
    loan("3000000006", "JOHN SMITH", "", zip_code="78757", jobs="1", business_type="Sole Proprietorship"),
    loan("3000000007", "SERVPRO OF NORTH AUSTIN", "8000 Research Blvd", zip_code="78757", jobs="30"),
    # One letter apart, other house number; a chain store at another address.
    loan("3000000008", "ACME PLUMBING LLC", "900 Burnet Rd", zip_code="78757", jobs="40"),
    loan("3000000009", "SUBWAY", "2000 Lamar Blvd", zip_code="78757", jobs="9"),
    # A franchise loan (FranchiseName set).
    loan("3000000010", "PEARL BAKERY CAFE LLC", "10 Oak St", zip_code="78757", jobs="14",
         FranchiseName="Pearl Bakery Cafe"),
    # Two stores with the same (non-chain) name: two borrowers. The first store has 2 draws.
    loan("3000000011", "TEXAS ROOFING PROS", "1 Anderson Ln", zip_code="78757", jobs="7"),
    loan("3000000012", "TEXAS ROOFING PROS", "900 Anderson Ln", zip_code="78757", jobs="70"),
    loan("3000000013", "TEXAS ROOFING PROS", "1 ANDERSON LANE", zip_code="78757", jobs="8",
         date="02/03/2021", method="PPS"),
    # A real match next to the building's other tenants.
    loan("3000000014", "JOES PIZZA & GRILL LLC", "1200 Main St Ste 120", zip_code="78757", jobs="12"),
]


@pytest.fixture
def trap_db(ppp_path, tmp_path):
    csv_file = tmp_path / "public_up_to_150k_2_240930.csv"
    csv_file.write_bytes(csv_bytes(TRAP_ROWS))
    ppp.import_files([str(csv_file)])
    return ppp_path


@pytest.mark.parametrize("name, address", [
    # Another tenant of the same building (suites used to be ignored).
    ("Main Street Dental", "1200 Main St Suite 150, Austin, TX 78757"),
    ("Austin Family Dentistry", "1200 Main St Suite 120, Austin, TX 78757"),
    ("Bright Smiles Dental", "1200 Main St Ste 250, Austin, TX 78757"),
    ("Lakeside Bistro", "1200 Main St, Austin, TX 78757"),
    # The shorter name inside a longer one, with no address to confirm it.
    ("Austin Dental", "5000 Burnet Rd, Austin, TX 78757"),
    ("John Smith, CPA", "Austin, TX 78757"),
    ("SERVPRO of South Austin", "Austin, TX 78757"),
    # Near-identical names at another house number; a chain store at another address.
    ("Ace Plumbing", "123 Burnet Rd, Austin, TX 78757"),
    ("Subway", "3100 Lamar Blvd, Austin, TX 78757"),
    ("Subway", "Austin, TX 78757"),
    # A franchise loan only matches at the same street address.
    ("Pearl Bakery Cafe", "Austin, TX 78757"),
    ("Pearl Bakery Cafe", "12 Oak St, Austin, TX 78757"),
    # Two stores with this name and no address to tell which: don't guess.
    ("Texas Roofing Pros", "Austin, TX 78757"),
])
def test_wrong_business_is_not_matched(trap_db, name, address):
    assert ppp.match_company(google_company(name, address)) is None


def test_chain_and_franchise_match_at_the_same_street_address(trap_db):
    subway = ppp.match_company(google_company("Subway", "2000 Lamar Blvd, Austin, TX 78757"))
    assert subway["jobs"] == 9 and subway["confidence"] == "high"
    pearl = ppp.match_company(google_company("Pearl Bakery Cafe", "10 Oak St, Austin, TX 78757"))
    assert pearl["jobs"] == 14 and pearl["confidence"] == "high"


def test_each_store_is_its_own_borrower(trap_db):
    store = ppp.match_company(google_company("Texas Roofing Pros", "1 Anderson Ln, Austin, TX 78757"))
    assert store["loans"] == 2 and store["jobs"] == 8  # its two draws, not the other store's loan
    other = ppp.match_company(google_company("Texas Roofing Pros", "900 Anderson Ln, Austin, TX 78757"))
    assert other["loans"] == 1 and other["jobs"] == 70


def test_real_match_in_a_shared_building(trap_db):
    found = ppp.match_company(google_company("Joe's Pizza", "1200 Main St Ste 120, Austin, TX 78757"))
    assert found["name"] == "JOES PIZZA & GRILL LLC" and found["confidence"] == "medium"
    assert found["match"] == "same street address and similar name"
    # Without a unit on the company's side the building address still agrees.
    assert ppp.match_company(google_company("Joe's Pizza", "1200 Main St, Austin, TX 78757"))["jobs"] == 12


# ----- importing odd data, and running alongside the app -----------------------------------


def test_bad_rows_are_skipped_not_fatal(ppp_path, tmp_path):
    rows = [
        loan("4000000001", "INFINITE JOBS LLC", "1 A St", jobs="inf"),
        loan("4000000002", "HUGE JOBS LLC", "2 A St", jobs="1e30", amount="1e400"),
        loan("4000000003", "GOOD BAKERY", "3 A St", jobs="6"),
    ]
    # A row with a field over Python's default csv limit (131,072 characters).
    data = csv_bytes(rows) + b'4000000004,05/01/2020,,PPP,"' + b"x" * 200_000 + b'",1 B St\r\n'
    csv_file = tmp_path / "odd.csv"
    csv_file.write_bytes(data)
    assert ppp.import_files([str(csv_file)]) == 3
    jobs = {r[1]: r[4] for r in loans_in(ppp_path)}
    assert jobs == {"INFINITE JOBS LLC": None, "HUGE JOBS LLC": None, "GOOD BAKERY": 6}
    assert ppp.available()


def test_one_broken_file_does_not_stop_the_others(ppp_path, tmp_path, capsys):
    good = tmp_path / "good.csv"
    good.write_bytes(csv_bytes())
    broken = tmp_path / "broken.csv"
    broken.write_bytes(b"not,a,ppp,file\r\n1,2,3,4\r\n")
    ppp.import_files([str(broken), str(good)])
    out = capsys.readouterr().out
    assert "broken.csv: failed" in out and "good.csv: 11 loans saved" in out and "paused" in out
    # broken.csv is in the files table without having finished: matching waits for it.
    assert not ppp.available()


def test_matching_waits_while_a_file_is_unfinished(ppp_db):
    assert ppp.available()
    conn = ppp._connect_rw()
    data = csv_bytes()
    with pytest.raises(IOError):  # an import that broke off halfway
        ppp.import_stream(conn, [data[: len(data) // 2]], "other.csv", total_bytes=len(data), progress=lambda _: None)
    conn.close()
    assert not ppp.available()
    c = google_company("Bright Smile Dental", "1 Main St, Austin, TX 78704, USA")
    assert ppp.apply_ppp(c) is False and c.ppp_checked_at is None  # not saved as "no loan"


def test_import_rebuilds_the_zip_index(ppp_db, tmp_path, monkeypatch):
    with sqlite3.connect(ppp_db) as conn:
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"loans_zip", "loans_file"} <= indexes
    seen = []
    real_start = ppp._start_import

    def spy(conn):
        real_start(conn)
        seen.append(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'loans_zip'").fetchone())

    monkeypatch.setattr(ppp, "_start_import", spy)
    csv_file = str(tmp_path / "public_up_to_150k_1_240930.csv")
    ppp.import_files([csv_file])  # nothing to import: the index isn't touched
    assert seen == [] and ppp.available()
    ppp.import_files([csv_file], force=True)  # dropped first, built again at the end
    assert seen == [None] and ppp.available()


def test_finish_import_survives_a_reader(ppp_db, monkeypatch, capsys):
    monkeypatch.setattr(ppp, "JOURNAL_WAIT", 0)
    conn = ppp._connect_rw()

    class Busy:
        """A connection whose journal switch always fails, as when the app is reading."""
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA journal_mode"):
                raise sqlite3.OperationalError("database is locked")
            return conn.execute(sql, *args)

        def commit(self):
            conn.commit()

    ppp._finish_import(Busy())  # no exception
    conn.close()
    assert "Stop the app" in capsys.readouterr().out


# ----- saving: companies outside the US, and a few at a time -------------------------------


def test_match_saved_companies_marks_companies_outside_the_us(db, ppp_db):
    db.add_all([
        Company(name="Karachi Dental", source="google", source_ref="pk", country="Pakistan", raw={}),
        Company(name="Lahore Bakery", source="osm", source_ref="pk2", address="Mall Road, Lahore", raw={}),
    ])
    db.commit()
    assert ppp.match_saved_companies(db) == (0, 0)
    db.expire_all()
    for company in db.query(Company):
        assert company.ppp == {"not_applicable": True} and company.ppp_checked_at is not None
    # The next start has nothing left to look at.
    assert db.query(Company).filter(Company.ppp_checked_at.is_(None)).count() == 0


def test_match_saved_companies_a_few_at_a_time(db, ppp_db):
    db.add_all([
        google_company("Home Slice Pizza", "1415 S Congress Ave, Austin, TX 78704, USA"),
        google_company("Bright Smile Dental", "1 Main St, Austin, TX 78704, USA"),
        google_company("Acme Plumbing", "500 W Oltorf St, Austin, TX 78704, USA"),
    ])
    db.commit()
    assert ppp.match_saved_companies(db, limit=2) == (2, 1)
    assert db.query(Company).filter(Company.ppp_checked_at.is_(None)).count() == 1
    assert ppp.match_saved_companies(db, limit=2) == (1, 1)
    assert ppp.match_saved_companies(db, limit=2) == (0, 0)
