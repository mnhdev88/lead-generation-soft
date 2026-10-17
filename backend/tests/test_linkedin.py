"""Reading LinkedIn web search results (app/people/linkedin.py). Precision first: every
doubtful result must be rejected, so most tests here are "must NOT accept" cases."""
from types import SimpleNamespace

import pytest

from app.people import linkedin
from app.people.linkedin import FoundPerson, parse_company_page, parse_profiles

SMILE = SimpleNamespace(name="Smile Dental", city="Austin")
SMILE_NO_CITY = SimpleNamespace(name="Smile Dental", city=None)


def r(title, snippet="", url="https://www.linkedin.com/in/janedoe"):
    return {"title": title, "url": url, "snippet": snippet}


def one(result, company=SMILE):
    people = parse_profiles([result], company)
    assert len(people) <= 1
    return people[0] if people else None


# ----- queries ----------------------------------------------------------------------


def test_people_query_quotes_name_drops_legal_suffix_and_adds_city():
    q = linkedin.people_query(SimpleNamespace(name="Smile Dental, PLLC", city="Austin"))
    assert q == ('site:linkedin.com/in ("owner" OR "founder" OR "president" OR "CEO" OR "managing partner" '
                 'OR "principal") "Smile Dental" Austin')


def test_people_query_without_city_and_branch_name():
    q = linkedin.people_query(SimpleNamespace(name="Acme Plumbing Inc. - North Branch", city=None))
    assert q.endswith('"Acme Plumbing"')
    q = linkedin.people_query(SimpleNamespace(name='Joe "The Plumber" LLC', city=""))
    assert q.endswith('"Joe The Plumber"')


def test_company_page_query():
    assert linkedin.company_page_query(SMILE) == 'site:linkedin.com/company "Smile Dental" Austin'


# ----- accepted ---------------------------------------------------------------------


def test_google_three_part_title():
    p = one(r("Jane Doe - Owner - Smile Dental | LinkedIn",
              "Owner at Smile Dental · Experience: Smile Dental · Location: Austin, Texas, United States"))
    assert p == FoundPerson("Jane Doe", "Owner", "https://www.linkedin.com/in/janedoe",
                            "https://www.linkedin.com/in/janedoe")


def test_dash_title_with_at():
    p = one(r("Jane Doe – Founder & CEO at Smile Dental", "Austin, Texas, United States · 500+ connections"))
    assert (p.full_name, p.title) == ("Jane Doe", "Founder & CEO")


def test_name_only_title_with_snippet_headline():
    p = one(r("Jane Doe | LinkedIn", "Owner at Smile Dental · Experience: Smile Dental · Location: Greater Austin Area"))
    assert (p.full_name, p.title) == ("Jane Doe", "Owner")


def test_founder_and_ceo_role_is_accepted():
    p = one(r("Jane Doe | LinkedIn", "Founder and CEO at Smile Dental · Location: Austin"))
    assert p.title == "Founder and CEO"


def test_two_part_title_company_with_snippet_role():
    p = one(r("Jane Doe - Smile Dental | LinkedIn", "Practice Owner at Smile Dental · Location: Austin, Texas"))
    assert p.title == "Practice Owner"


def test_headline_plus_current_experience():
    p = one(r("Jane Doe - Owner | LinkedIn", "Experience: Smile Dental · Education: UT Austin · Location: Austin"))
    assert p.title == "Owner"


def test_legal_suffix_and_case_and_ampersand_differences():
    company = SimpleNamespace(name="Smith & Sons Plumbing, LLC", city="Toronto")
    p = one(r("Bob Smith - President - Smith and Sons Plumbing | LinkedIn"), company)
    assert p.title == "President"
    p = one(r("Bob Smith - President - SMITH & SONS PLUMBING INC. | LinkedIn"), company)
    assert p is not None


def test_company_written_together_or_with_city():
    assert one(r("Jane Doe - Owner - SmileDental | LinkedIn")) is not None
    assert one(r("Jane Doe - Owner - Smile Dental Austin | LinkedIn")) is not None
    branch = SimpleNamespace(name="Smile Dental - South Austin", city="Austin")
    assert one(r("Jane Doe - Owner - Smile Dental | LinkedIn"), branch) is not None


def test_credentials_go_to_title_and_emojis_are_removed():
    p = one(r("Dr. Jane Doe, DDS 🦷 - Owner - Smile Dental | LinkedIn"))
    assert (p.full_name, p.title) == ("Jane Doe", "Owner, DDS")
    p = one(r("JANE DOE DMD - Owner - Smile Dental | LinkedIn"))
    assert (p.full_name, p.title) == ("Jane Doe", "Owner, DMD")
    p = one(r("✨ Jane (JJ) Doe ✨ - Owner - Smile Dental | LinkedIn"))
    assert p.full_name == "Jane Doe"


def test_surname_that_looks_like_a_credential_is_kept():
    p = one(r("Minh Do - Owner - Smile Dental | LinkedIn"))
    assert p.full_name == "Minh Do"


def test_regional_url_is_normalised_and_source_kept():
    url = "https://pk.linkedin.com/in/Ali-Khan-123/?originalSubdomain=pk"
    company = SimpleNamespace(name="Khan Dental Care", city="Lahore")
    p = one(r("Ali Khan - CEO - Khan Dental Care | LinkedIn", "Location: Lahore, Punjab, Pakistan", url=url), company)
    assert p.linkedin_url == "https://www.linkedin.com/in/ali-khan-123"
    assert p.source_url == url


def test_only_state_or_country_location_is_not_another_city():
    assert one(r("Jane Doe - Owner - Smile Dental | LinkedIn", "Location: Texas, United States")) is not None
    assert one(r("Jane Doe - Owner - Smile Dental | LinkedIn", "Location: United States")) is not None


def test_html_entities_in_results():
    p = one(r("Bob Smith - Owner - Smith &amp; Sons | LinkedIn"), SimpleNamespace(name="Smith & Sons", city=None))
    assert p is not None


def test_managing_partner_and_principal_and_office_manager():
    assert one(r("Jane Doe - Managing Partner - Smile Dental | LinkedIn")) is not None
    assert one(r("Jane Doe - Principal Dentist - Smile Dental | LinkedIn")) is not None
    assert one(r("Jane Doe - Office Manager - Smile Dental | LinkedIn")) is not None


def test_at_most_three_people_and_no_duplicates():
    results = [r(f"Person {n} Doe - Owner - Smile Dental | LinkedIn", url=f"https://www.linkedin.com/in/p{n}")
               for n in "ABCDE"]
    results.insert(1, r("Person A Doe - Owner - Smile Dental | LinkedIn", url="https://www.linkedin.com/in/pa"))
    results.insert(1, r("Someone Else - Owner - Smile Dental | LinkedIn", url="https://uk.linkedin.com/in/pA/"))
    people = parse_profiles(results, SMILE)
    assert [p.full_name for p in people] == ["Person A Doe", "Person B Doe", "Person C Doe"]


# ----- rejected: wrong company ------------------------------------------------------------


@pytest.mark.parametrize("employer", [
    "Smile Dental Studio",  # a different business with a longer name
    "Smile Dental Care",
    "Bright Smile Dental",
    "Smiles Dental",
    "Smile",
    "Dental",
    "Smile Dental Stu...",  # cut off by the search engine: can't tell
    "Smile Dent…",
])
def test_other_company_is_rejected(employer):
    assert one(r(f"Jane Doe - Owner - {employer} | LinkedIn")) is None
    assert one(r("Jane Doe | LinkedIn", f"Owner at {employer} · Location: Austin")) is None


def test_owner_at_smile_dental_studio_vs_smile_dental():
    assert one(r("Jane Doe | LinkedIn", "Owner at Smile Dental Studio · Experience: Smile Dental Studio")) is None
    studio = SimpleNamespace(name="Smile Dental Studio", city="Austin")
    assert one(r("Jane Doe | LinkedIn", "Owner at Smile Dental · Experience: Smile Dental"), studio) is None


def test_same_name_person_at_other_company():
    assert one(r("Jane Doe - Owner - Acme Corp | LinkedIn", "Experience: Smile Dental · Location: Austin")) is None


def test_company_only_mentioned_not_employer():
    # Smile Dental only appears in a description, not as where they work.
    assert one(r("Jane Doe - Owner - Doe Consulting | LinkedIn",
                 "Helping practices like Smile Dental grow · Location: Austin")) is None
    assert one(r("John Roe | LinkedIn",
                 "I work with Jane Doe, owner at Smile Dental · Location: Austin")) is None
    assert one(r("John Roe | LinkedIn", "Proud to support our owner at Smile Dental")) is None


def test_headline_without_any_employer_is_rejected():
    assert one(r("Jane Doe - Owner | LinkedIn", "Location: Austin")) is None
    assert one(r("Jane Doe - Smile Dental | LinkedIn", "Experience: Smile Dental · Location: Austin")) is None


def test_headline_with_experience_but_another_employer_named():
    assert one(r("Jane Doe - Owner | LinkedIn", "Owner at Acme Dental · Experience: Smile Dental")) is None


# ----- rejected: not working there now -----------------------------------------------------


@pytest.mark.parametrize("result", [
    r("Jane Doe - Former Owner - Smile Dental | LinkedIn"),
    r("Jane Doe | LinkedIn", "Former Owner at Smile Dental · Location: Austin"),
    r("Jane Doe | LinkedIn", "Ex-Owner at Smile Dental · Location: Austin"),
    r("Jane Doe | LinkedIn", "Retired Owner at Smile Dental"),
    r("Jane Doe | LinkedIn", "Past President at Smile Dental"),
    r("Jane Doe - Owner - Smile Dental | LinkedIn", "Previously: Smile Dental · Owner at Bright Smiles"),
    r("Jane Doe - Owner - Bright Smiles | LinkedIn", "Owner at Bright Smiles · Former owner of Smile Dental"),
    r("Jane Doe - Owner - Smile Dental | LinkedIn", "Former owner at Smile Dental · Location: Austin"),
    r("Jane Doe - CEO - Bright Smiles | LinkedIn", "CEO at Bright Smiles (ex-Smile Dental)"),
])
def test_former_employees_are_rejected(result):
    assert one(result) is None


# ----- rejected: not a decision-maker ------------------------------------------------------


@pytest.mark.parametrize("title", [
    "Jane Doe - Dental Hygienist - Smile Dental | LinkedIn",
    "Jane Doe - Receptionist - Smile Dental | LinkedIn",
    "Jane Doe - Dental Assistant - Smile Dental | LinkedIn",
    "Jane Doe - Executive Assistant to the CEO - Smile Dental | LinkedIn",
    "Jane Doe - Associate Dentist - Smile Dental | LinkedIn",
    "Jane Doe - Marketing Coordinator - Smile Dental | LinkedIn",
    "Jane Doe - Product Owner - Smile Dental | LinkedIn",
])
def test_non_decision_makers_are_rejected(title):
    assert one(r(title)) is None


def test_students_and_recruiters_are_rejected():
    assert one(r("Jane Doe - Owner - Smile Dental | LinkedIn", "Dental student at UT · Location: Austin")) is None
    assert one(r("Jane Doe - Student - Smile Dental | LinkedIn")) is None
    assert one(r("Jane Doe - Founder - Smile Dental | LinkedIn", "Dental recruiter · Location: Austin")) is None
    assert one(r("Jane Doe - Owner - Smile Dental | LinkedIn", "Talent Acquisition · Hiring for Smile Dental")) is None


# ----- rejected: another city --------------------------------------------------------------


def test_other_city_is_rejected_when_city_known():
    res = r("Jane Doe - Owner - Smile Dental | LinkedIn", "Owner at Smile Dental · Location: Dallas, Texas, United States")
    assert one(res) is None
    assert one(res, SMILE_NO_CITY) is not None  # city unknown: can't tell
    res = r("Jane Doe | LinkedIn", "Owner at Smile Dental. Toronto, Ontario, Canada. 500 connections")
    assert one(res) is None
    assert one(r("Jane Doe - Owner - Smile Dental | LinkedIn", "Location: Greater Dallas Area")) is None
    assert one(r("Jane Doe - Owner - Smile Dental | LinkedIn", "Location: Austin, Texas Metropolitan Area")) is not None


def test_other_city_helper():
    assert linkedin.other_city("Saint Louis, Missouri", "St. Louis") is False
    assert linkedin.other_city("New York, New York, United States", "New York City") is False
    assert linkedin.other_city("Brooklyn, New York", "New York") is True
    assert linkedin.other_city("Punjab, Pakistan", "Lahore") is False
    assert linkedin.other_city(None, "Lahore") is False


# ----- rejected: not a person's profile page ----------------------------------------------


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/company/smile-dental",
    "https://www.linkedin.com/posts/janedoe_owner-smile-dental-activity-123",
    "https://www.linkedin.com/pulse/why-i-opened-smile-dental-jane-doe",
    "https://www.linkedin.com/in/janedoe/detail/recent-activity/",
    "https://www.linkedin.com/pub/jane-doe/1/2/3",
    "https://www.linkedin.com/jobs/view/123",
    "http://www.linkedin.com/in/janedoe",  # not https
    "https://linkedin.com.evil.example/in/janedoe",
    "https://www.facebook.com/in/janedoe",
    "https://www.linkedin.com/in/",
    "",
])
def test_non_profile_urls_are_rejected(url):
    assert one(r("Jane Doe - Owner - Smile Dental | LinkedIn", url=url)) is None


def test_company_page_posing_as_person_is_rejected():
    assert one(r("Smile Dental - Owner - Smile Dental | LinkedIn")) is None


@pytest.mark.parametrize("name", ["Jane", "LinkedIn Member", "123 456", "A B C D E F G", "Jobs at Smile Dental"])
def test_names_that_are_not_people(name):
    assert one(r(f"{name} - Owner - Smile Dental | LinkedIn")) is None


def test_bad_inputs_are_ignored():
    assert parse_profiles(None, SMILE) == []
    assert parse_profiles([None, "x", {}, {"url": None}], SMILE) == []


# ----- company page -----------------------------------------------------------------------


def test_parse_company_page():
    results = [
        r("Jane Doe - Owner - Smile Dental | LinkedIn"),  # a person: skipped
        r("Smile Dental Studio | LinkedIn", url="https://www.linkedin.com/company/smile-dental-studio"),
        r("Smile Dental | LinkedIn", url="https://www.linkedin.com/company/Smile-Dental-ATX/?trk=x"),
    ]
    assert parse_company_page(results, SMILE) == "https://www.linkedin.com/company/smile-dental-atx"


def test_parse_company_page_title_variants():
    for title in ("Smile Dental: Overview | LinkedIn", "Smile Dental, PLLC - LinkedIn", "Smile Dental - Austin | LinkedIn"):
        res = [r(title, url="https://ca.linkedin.com/company/smiledental")]
        assert parse_company_page(res, SMILE) == "https://www.linkedin.com/company/smiledental", title


def test_parse_company_page_rejects():
    assert parse_company_page([r("Smiles Dental | LinkedIn", url="https://www.linkedin.com/company/x")], SMILE) is None
    assert parse_company_page([r("Smile Dental | LinkedIn", url="https://www.linkedin.com/company/x/jobs")], SMILE) is None
    assert parse_company_page([r("Smile Dental | LinkedIn", url="https://www.linkedin.com/school/x")], SMILE) is None
    assert parse_company_page([], SMILE) is None
