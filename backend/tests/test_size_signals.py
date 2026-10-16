"""Tests for the business-size signals read from a website: stated headcount, founded
year, number of branches and a careers link (app/enrich/extract.py), and how the
crawler merges them across pages (app/enrich/crawler.py).

The sentences come from the research corpus (real business websites and typical
wording). Positives must give exactly the expected number; negatives must give
nothing, because a wrong number is worse than none."""
import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest

from app.enrich import crawl_website, crawler
from app.enrich.crawler import _empty_signals, _merge_size_signals
from app.enrich.extract import analyze_page, detect_signals, detect_size_signals


def size(text: str, page_url: str = "https://acme.com/") -> dict:
    """Size signals of a page that holds just this sentence."""
    return detect_size_signals(f"<html><body><p>{text}</p></body></html>", page_url)


# ---------------------------------------------------------------------------
# Headcount
# ---------------------------------------------------------------------------

EMPLOYEES_POSITIVE = [
    ("Our team of 25 professionals is ready to help with all your tax needs.", 25),
    ("We have over 50 employees serving the greater Tampa Bay area.", 50),
    ("Our friendly front desk is part of a staff of twelve who treat you like family.", 12),
    ("What was once a one truck operation has grown to more than 175 employees.", 175),
    ("We've now grown to 14 clinics with over 850 dedicated team members!", 850),
    ("Our 14 clinics and supporting offices employ over 800 Wisconsinites.", 800),
    ("We now have 5 employees with over 75 years of expertise.", 5),
    ("The company has grown to a multi-million-dollar operation with a work force of 50-60 people "
     "working in two states.", 50),
    ("Today we are a team of 10-15 dedicated technicians.", 10),
    ("We employ 40 full-time staff members across our service area.", 40),
    ("Smith & Sons is a family business with 8 employees.", 8),
    ("Our firm has grown to 30 attorneys and staff.", 30),
    ("More than 200 employees call Acme Heating & Air home.", 200),
    ("Green Valley Landscaping employs approximately 60 people year-round.", 60),
    ("Behind every smile is a dedicated staff of two dozen.", 24),
    ("Our crew of 18 licensed electricians handles jobs big and small.", 18),
    ("With a team of over 100 people, we can handle projects of any size.", 100),
    ("We are a small team of 6 who love what we do.", 6),
    ("Our 35-person team brings creativity to every campaign.", 35),
    ("A 12-member staff keeps our kitchen running seven days a week.", 12),
    ("Number of employees: 45", 45),
    ("Company size: 11-50 employees", 11),
    ("Our staff of nearly 30 is here to serve you.", 30),
    ("Founded in 1998, we have grown to a team of 22.", 22),
    ("We're a team of twenty-five designers, developers and strategists.", 25),
    ("Our practice is supported by a staff of fifteen.", 15),
    ("Today we have over 1,200 employees nationwide.", 1200),
    ("Our team has grown to 40 members.", 40),
    ("Our office is staffed by 9 friendly team members.", 9),
    ("We're a family-run shop of 7 people.", 7),
    ("Our team of a dozen stylists is booked weeks out.", 12),
    ("We have 3 employees and 1 dog.", 3),
]

EMPLOYEES_NEGATIVE = [
    "Our team brings 25 years of experience to every case.",
    "Over 30 years combined experience on our team.",
    "Over 10,000 happy patients and counting!",
    "500+ 5-star reviews from our clients",
    "4.9 stars from 300 reviews",
    "Serving all 50 states with our expert team",
    "Our team of experts is here to help.",
    "Named 1 of 10 best dentists in Austin by our patients",
    "24/7 emergency service: our team is always available.",
    "$99 cleaning special: our staff will take great care of you.",
    "Call our team at 555-123-4567 today!",
    # Role counts (hygienists, doctors, attorneys) are only part of the staff, so they are
    # not a headcount: storing 3 would understate the size.
    "We have 3 hygienists and a state-of-the-art lab.",
    "Our 2 doctors have decades of combined experience.",
    "Our 3 attorneys focus exclusively on family law.",
    "Trusted by 200 businesses across Ohio",
    "Since 1998 our team has served the Denver area.",
    "Our team of 5-star rated technicians",
    "Our team completed over 1,500 projects last year.",
    "Our team installed 2,000+ roofs since 2005.",
    "Join our team! We're hiring 3 new technicians.",
    "We have 2 openings on our team.",
    "Our staff speaks 4 languages.",
    "Our team serves 12 counties in North Texas.",
    "We treat over 100 patients a day.",
    "We maintain a 1:4 staff-to-child ratio.",
    "One teacher for every 6 children in our toddler room",
    "Our 3 locations and friendly staff make scheduling easy.",
    "Our team of 12 volunteers packed meals at the food drive.",
    "Our employees have earned 20+ industry certifications.",
    "We offer a 401(k) match to all employees.",
    "COVID-19: our staff is fully vaccinated.",
    "Suite 200, Staff Entrance at rear",
    "Our private dining room seats 40 people.",
    "Party packages for up to 20 people",
    "Over 40 people attended our open house.",
    "Employee of the Month - March 2024",
    "Named one of the Top 100 Workplaces in Michigan",
    "© 2024 Smith Law Team. All rights reserved.",
    "Meet the Team (12 photos)",
    "Our team of 2 million satisfied customers",
    # Extra traps found while writing the patterns.
    "Our team of 10-15 years of experience",
    "We're hiring 4 new employees this spring.",
    "Our team has trained over 300 people in CPR.",
    # The size of the business's customers, not its own team (payroll, insurance, HR, IT
    # firms and venues write this all the time).
    "We provide payroll services for companies with 5 to 500 employees.",
    "Ideal for companies under 50 employees.",
    "We specialize in small businesses with fewer than 50 employees.",
    "Our clients range from startups to companies with 1,000 employees.",
    "Workers compensation coverage for your 25 employees.",
    "Covering 30 employees or more requires a group plan.",
    "Less than 10 employees? We have a plan for you.",
    "Group health plans for 2-50 employees.",
    "Our facility can host 150 people for weddings.",
    "Team building events for groups of 20 to 200 people at our venue.",
    "We handle IT for offices with 20 to 40 employees.",
    "Ideal for practices with 10-20 staff.",
    "Plans for 10 employees and up.",
]


@pytest.mark.parametrize("text, expected", EMPLOYEES_POSITIVE)
def test_headcount_positive(text, expected):
    signals = size(text)
    assert signals["employees_stated"] == expected
    assert signals["employees_from"] == "text"
    assert signals["employees_snippet"] == text[:200]


@pytest.mark.parametrize("text", EMPLOYEES_NEGATIVE)
def test_headcount_negative(text):
    signals = size(text)
    assert signals["employees_stated"] is None
    assert signals["employees_from"] is None and signals["employees_snippet"] is None


def test_headcount_takes_the_largest_and_drops_conflicts():
    # The same page often states growth twice: we keep the larger number.
    page = "<p>We are a team of 40.</p><p>Our team has grown to 45 members.</p>"
    assert detect_size_signals(page)["employees_stated"] == 45
    page = "<p>We have 40 employees.</p><p>Today we have over 50 employees.</p>"
    assert detect_size_signals(page)["employees_stated"] == 50
    # Numbers more than 3x apart: the page is unclear, so nothing is stored.
    page = "<p>We have 6 employees.</p><p>Today we have over 1,200 employees nationwide.</p>"
    assert detect_size_signals(page)["employees_stated"] is None
    page = "<p>We have 6 employees.</p><p>Today we have over 20 employees.</p>"
    assert detect_size_signals(page)["employees_stated"] is None
    page = "<p>We are a team of 5.</p><p>Our team has grown to 20 members.</p>"
    assert detect_size_signals(page)["employees_stated"] is None


def test_clear_self_description_beats_a_bare_employee_count():
    # "employ over 800" describes the business; a bare "850 team members" is used
    # only when nothing clearer is on the page.
    page = ("<p>We've now grown to 14 clinics with over 850 dedicated team members!</p>"
            "<p>Our 14 clinics and supporting offices employ over 800 Wisconsinites.</p>")
    assert detect_size_signals(page)["employees_stated"] == 800
    # A small firm that mentions its clients' size keeps its own team size.
    page = ("<p>Our small team of 4 helps local businesses.</p>"
            "<p>We look after 30 employees at each client site.</p>")
    signals = detect_size_signals(page)
    assert signals["employees_stated"] == 4
    assert signals["employees_snippet"] == "Our small team of 4 helps local businesses."
    page = ("<p>Our small team of 4 helps local businesses.</p>"
            "<p>We handle IT for offices with 20 to 40 employees.</p>")
    assert detect_size_signals(page)["employees_stated"] == 4
    page = "<p>We are a team of 5.</p><p>Ideal for practices with 10-20 staff.</p>"
    assert detect_size_signals(page)["employees_stated"] == 5


def test_snippet_is_capped():
    text = "We have 8 employees " + "and many happy customers " * 30 + "."
    assert size(text)["employees_snippet"] == text[:200]


# ---------------------------------------------------------------------------
# Founded year
# ---------------------------------------------------------------------------

FOUNDED_POSITIVE = [
    ("Proudly serving Denver since 1998.", 1998),
    ("Established in 1987, Miller Insurance has protected local families for generations.", 1987),
    ("Family-owned since 1952.", 1952),
    ("Founded in 2010 by two friends with a passion for coffee.", 2010),
    ("EST. 1998", 1998),
    ("The Manos family has guided Dental Associates since its founding in 1974.", 1974),
    ("T.P. Howard's Plumbing is a family owned, and operated business established in 1983.", 1983),
    ("Bradley Plumbing & Heating, Inc. was founded in January 1952 by Walter J. Bradley.", 1952),
    ("Blaylock Plumbing is a family-owned business established in 1938.", 1938),
    ("Serving Northern Michigan since 2000.", 2000),
    ("In business since 1965.", 1965),
    ("We opened our doors in 2004 on Main Street.", 2004),
    ("Dr. Smith started the practice in 1991.", 1991),
    ("Founded: 2015", 2015),
    ("Since 1975 - Celebrating 50 Years!", 1975),
    ("A family tradition since 1952", 1952),
    ("Our very first clinic opened in 1974 on South 16th Street in Milwaukee.", 1974),
    ("Founded in 1998, we have grown to a team of 22.", 1998),
]

FOUNDED_NEGATIVE = [
    "© 1998-2024 Smith Dental. All rights reserved.",
    "Copyright 2023 Acme Roofing",
    "Page last updated 2023",
    "With over 25 years of experience, we know roofs.",
    "1998 Main St, Suite 4, Austin, TX 78701",
    "Call 1-800-555-1987 for a free quote",
    "Est. 2,000 sq ft showroom",
    "Est. wait time: 15 minutes",
    "Dr. Lee graduated from UCLA in 1995.",
    "Dr. Patel has been practicing dentistry since 1990.",
    "BBB Accredited Business - Member since 2012",
    "Built in 1920, our historic building has character.",
    "Since 2020, we have offered telehealth visits.",
    "Founded in 1776, Philadelphia is rich in history.",
    "Established patients can book online.",
    "Voted Best of 2019 by readers",
    "License #19875-B",
    "Established in 2031",
    "Serving customers since last year",
    # Extra traps found while writing the patterns.
    "© 2015 Acme Plumbing, established in 2015. All rights reserved.",
    "Dr. Lee started her career in 1995.",
    "We opened our second office in 2010.",
    "Dr. Patel has been serving patients since 1990.",
    # When a service or product started, not the business.
    "We began offering Invisalign in 2008.",
    "We began offering drain cleaning in 2005.",
    "We started accepting insurance in 2012.",
]


@pytest.mark.parametrize("text, expected", FOUNDED_POSITIVE)
def test_founded_positive(text, expected):
    signals = size(text)
    assert signals["founded_year"] == expected
    assert signals["founded_snippet"] == text[:200]


@pytest.mark.parametrize("text", FOUNDED_NEGATIVE)
def test_founded_negative(text):
    signals = size(text)
    assert signals["founded_year"] is None and signals["founded_snippet"] is None


def test_founded_beats_opened_and_gerund_names_still_count():
    # "founded" is clearer than "opened": an original store's opening is not the founding.
    signals = size("The original store opened in 1924; our company was founded in 2010.")
    assert signals["founded_year"] == 2010
    # Without a "founded" sentence, "opened" still counts.
    assert size("We opened our doors in 2004 on Main Street.")["founded_year"] == 2004
    # A real "founded" sentence wins over a later service date.
    assert size("Smith Dental was founded in 1995. We began offering Invisalign in 2008.")["founded_year"] == 1995
    # A business name that ends in "-ing" is not a service ("Sterling" is capitalised).
    assert size("John started Sterling Plumbing in 1990.")["founded_year"] == 1990
    assert size("The company was founded during the war in 1942.")["founded_year"] == 1942


def test_future_year_is_never_a_founded_year():
    next_year = datetime.now(timezone.utc).year + 1
    assert size(f"Established in {next_year}")["founded_year"] is None


def test_copyright_lines_never_give_a_founded_year():
    page = ("<footer><p>© 1998 Acme Roofing</p><p>Copyright 2001 - founded in 2001</p>"
            "<p>(c) 1999 All rights reserved. Since 1999</p></footer>")
    assert detect_size_signals(page)["founded_year"] is None


# ---------------------------------------------------------------------------
# Number of locations
# ---------------------------------------------------------------------------

LOCATIONS_POSITIVE = [
    ("Visit one of our 5 convenient locations in the Phoenix area.", 5),
    ("With offices in 3 cities across New Jersey", 3),
    ("We've now grown to 14 clinics with over 850 dedicated team members!", 14),
    ("With 4 offices across Northern Virginia, help is never far away.", 4),
    ("Two locations to serve you: Plano and Frisco.", 2),
    ("Visit any of our 7 locations for same-day service.", 7),
    ("We now operate 12 stores throughout Texas.", 12),
    ("Three convenient offices in Orange County", 3),
    ("Now with 9 branches statewide", 9),
    ("Our 3 restaurants in Austin each have their own menu.", 3),
    ("Book at any of our 20 salons.", 20),
    ("Now open: our second location in Plano!", 2),
    ("Our 14 clinics and supporting offices employ over 800 Wisconsinites.", 14),
]

LOCATIONS_NEGATIVE = [
    "Proudly serving 5 counties in Northern California",
    "Getting started is as easy as 3 easy steps.",
    "Find locations near you",
    "Serving 12 cities in the metro area",
    "We service over 30 zip codes.",
    "Our office features 4 treatment rooms.",
    "Free parking: 20 spaces at our location",
    "Accepted at over 50 locations nationwide",
    "Find our sauces in 200+ retail locations.",
    "Delivering to 3 states",
    "Our location has 6 styling chairs.",
    "Just 15 minutes from our location",
    # "both locations" would mean 2, but we only take stated numbers.
    "Open 7 days a week at both locations",
    "Suite 5, Location B",
    "All locations closed July 4",
    "Our 1 location is in Austin.",  # 1 is not "several branches"
]


@pytest.mark.parametrize("text, expected", LOCATIONS_POSITIVE)
def test_locations_positive(text, expected):
    assert size(text)["locations_stated"] == expected


@pytest.mark.parametrize("text", LOCATIONS_NEGATIVE)
def test_locations_negative(text):
    assert size(text)["locations_stated"] is None


def test_locations_takes_the_largest():
    page = "<p>Now open: our second location in Plano!</p><p>Visit any of our 7 locations.</p>"
    assert detect_size_signals(page)["locations_stated"] == 7


# ---------------------------------------------------------------------------
# Careers link
# ---------------------------------------------------------------------------

CAREERS_POSITIVE = [
    ("Careers", "/careers"),
    ("Career Opportunities", "/about/career-opportunities/"),
    ("Jobs", "/jobs"),
    ("Job Openings", "/job-openings"),
    ("Current Openings", "/current-openings"),
    ("Current Job Openings", "/current-job-openings"),
    ("Open Positions", "/open-positions"),
    ("Employment", "/employment"),
    ("Employment Opportunities", "/employment-opportunities/"),
    ("Join Our Team", "/join-our-team"),
    ("Join the Team", "/join-the-team"),
    # "Work With Us" can also mean "become a client" on agency sites; the corpus lists it
    # both ways. We count it (a careers link is only a weak size hint).
    ("Work With Us", "/work-with-us"),
    ("Work for Us", "/work-for-us"),
    ("We're Hiring", "/hiring"),
    ("We Are Hiring", "/now-hiring"),
    ("Now Hiring", "/now-hiring"),
    ("Hiring", "/hiring"),
    # "Apply Now" only counts when it points into a careers section.
    ("Apply Now", "/careers/apply"),
    # A bare "Opportunities" only counts when it links to /opportunities.
    ("Opportunities", "/opportunities"),
    ("Team Openings", "/team-openings"),
    # The path alone is enough, whatever the link text says.
    ("Click here", "/careers/"),
    ("Join Us", "/careers"),
    ("See what's open", "https://acme.com/were-hiring"),
    # Links to job sites (applicant tracking systems).
    ("Apply", "https://boards.greenhouse.io/acme"),
    ("Apply", "https://jobs.lever.co/acme"),
    ("Apply", "https://acme.applytojob.com/apply"),
    ("Apply", "https://acme.bamboohr.com/careers"),
    ("Apply", "https://apply.workable.com/acme/"),
    ("Apply", "https://recruiting.paylocity.com/recruiting/jobs/All/abc"),
    ("Apply", "https://acme.recruitee.com/"),
    ("Apply", "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"),
    ("Apply", "https://acme.breezy.hr/"),
    ("Apply", "https://www.indeed.com/cmp/Acme-Plumbing/jobs"),
    ("Apply", "https://www.dentalpost.net/dental-jobs/acme"),
    ("Apply", "https://poachedjobs.com/acme"),
    ("Apply", "https://harri.com/acme"),
]

CAREERS_NEGATIVE = [
    ("Career Advice", "/blog/career-advice"),
    ("Career Tips", "/career-tips"),
    ("Career Services", "/career-services"),
    ("Our Work", "/our-work"),
    ("Work Samples", "/work-samples"),
    ("Recent Work", "/recent-work"),
    ("Jobs We Completed", "/jobs-we-completed"),
    ("Recent Jobs", "/recent-jobs"),
    ("Job Gallery", "/job-gallery"),
    ("Job Photos", "/job-photos"),
    ("Jobs", "/gallery/jobs"),  # a contractor's "jobs" are projects
    ("Job Site Safety", "/job-site-safety"),
    ("Jobsite Cleanup", "/jobsite-cleanup"),
    ("Job Estimate", "/estimate"),
    ("Request a Job Quote", "/quote"),
    ("Find a Job", "/find-a-job"),
    ("Job Seekers", "/job-seekers"),
    ("Post a Job", "/post-a-job"),
    ("Glassdoor", "https://www.glassdoor.com/Reviews/Acme-Reviews-E123.htm"),
    ("Indeed", "https://www.indeed.com/"),
    ("Join Our Mailing List", "/newsletter"),
    ("Join Our Newsletter", "/newsletter"),
    ("Join Our Loyalty Club", "/loyalty"),
    ("Join Our Team of Satisfied Customers", "/reviews"),
    ("Steve Jobs on design", "/steve-jobs-quote"),
    ("The Jobs Act explained", "/jobs-act"),
    ("Employment Law", "/practice-areas/employment-law"),
    ("Employment Litigation", "/employment-litigation"),
    ("Employee Benefits", "/employee-benefits"),
    ("Franchise Opportunities", "/franchise-opportunities"),
    ("Opportunities", "/invest"),
    ("Hiring a Contractor: 5 Tips", "/blog/hiring-a-contractor"),
    ("Careers", "https://someotherbusiness.com/careers"),  # another website
    ("Apply Now", "/contact"),
    # Gyms, churches and clubs use "Join Us" for membership.
    ("Join Us", "/membership"),
    ("Join Us!", "/membership"),
    ("Join Today", "/join-us"),
    ("See what's open", "https://acme.com/join-us"),
    # Not web addresses, in any letter case.
    ("Careers", "JavaScript:alert(document.cookie)"),
    ("Careers", "JAVASCRIPT:alert(1)"),
    ("Careers", "javascript:void(0)"),
    ("Careers", "vbscript:x"),
    ("Careers", "data:text/html,x"),
    ("Careers", "MAILTO:jobs@acme.com"),
    ("Careers", "ftp://acme.com/careers"),
]


def careers(text: str, href: str) -> dict:
    return detect_size_signals(f'<nav><a href="{href}">{text}</a></nav><p>Hello</p>', "https://acme.com/")


@pytest.mark.parametrize("text, href", CAREERS_POSITIVE)
def test_careers_link_positive(text, href):
    signals = careers(text, href)
    assert signals["has_careers"] is True
    assert signals["careers_url"].startswith("https://")


@pytest.mark.parametrize("text, href", CAREERS_NEGATIVE)
def test_careers_link_negative(text, href):
    signals = careers(text, href)
    assert signals["has_careers"] is False and signals["careers_url"] is None


def test_careers_url_is_resolved_and_first_wins():
    signals = detect_size_signals('<a href="/about">About</a><a href="careers/">Careers</a>'
                                  '<a href="/jobs">Jobs</a>', "https://acme.com/")
    assert signals["careers_url"] == "https://acme.com/careers/"


def test_hiring_heading_counts_without_a_link():
    signals = detect_size_signals("<h2>We're hiring!</h2><p>Send us your resume.</p>", "https://acme.com/")
    assert signals["has_careers"] is True and signals["careers_url"] is None
    # but not a sentence that merely mentions hiring
    signals = detect_size_signals("<p>Hiring a plumber is easy with us.</p>", "https://acme.com/")
    assert signals["has_careers"] is False


# ---------------------------------------------------------------------------
# JSON-LD (schema.org)
# ---------------------------------------------------------------------------

def jsonld(data: str) -> dict:
    return detect_size_signals(f'<script type="application/ld+json">{data}</script><p>Hi</p>')


@pytest.mark.parametrize("value, expected", [
    ('{"@type": "QuantitativeValue", "value": 25}', 25),
    ('{"@type": "QuantitativeValue", "minValue": 11, "maxValue": 50}', 11),
    ("12", 12),
    ('"12"', 12),
    ('"11-50"', 11),
    ('"50+"', 50),
    ('"10 to 20"', 10),
    ('"about 40"', 40),
    ('{"@type": "QuantitativeValue", "value": "25"}', 25),
    ('[{"@type": "QuantitativeValue", "value": 30}]', 30),
    # Missing or site-builder defaults
    ("0", None),
    ('""', None),
    ("1", None),
    ('{"@type": "QuantitativeValue"}', None),
    ("null", None),
    ('"lots"', None),
    ("1000000", None),
])
def test_jsonld_number_of_employees_shapes(value, expected):
    signals = jsonld('{"@type": "Dentist", "name": "Bright Smiles", "numberOfEmployees": %s}' % value)
    assert signals["employees_stated"] == expected
    assert signals["employees_from"] == ("jsonld" if expected else None)


def test_jsonld_example_dentist():
    signals = jsonld('{"@context":"https://schema.org","@type":"Dentist","name":"Bright Smiles Family '
                     'Dentistry","foundingDate":"1998","numberOfEmployees":{"@type":"QuantitativeValue",'
                     '"value":14},"address":{"@type":"PostalAddress","streetAddress":"1998 Main St",'
                     '"addressLocality":"Austin","addressRegion":"TX"}}')
    assert signals["employees_stated"] == 14 and signals["employees_from"] == "jsonld"
    assert signals["founded_year"] == 1998
    assert "numberOfEmployees" in signals["employees_snippet"]


def test_jsonld_graph_with_departments():
    signals = jsonld(
        '{"@context":"https://schema.org","@graph":[{"@type":"Organization","@id":"#org","name":"Acme HVAC",'
        '"foundingDate":"1987-01-01","numberOfEmployees":{"@type":"QuantitativeValue","minValue":51,'
        '"maxValue":200},"department":[{"@type":"HVACBusiness","name":"Acme HVAC Dallas","address":'
        '{"@type":"PostalAddress","streetAddress":"100 Elm St","postalCode":"75201"}},{"@type":'
        '"HVACBusiness","name":"Acme HVAC Fort Worth","address":{"@type":"PostalAddress",'
        '"streetAddress":"200 Oak Ave","postalCode":"76102"}}]}]}')
    assert signals["employees_stated"] == 51
    assert signals["founded_year"] == 1987
    assert signals["locations_stated"] == 2


def test_jsonld_text_band_and_area_served_ignored():
    signals = jsonld('{"@type":"LegalService","name":"Doe Law","numberOfEmployees":"11-50",'
                     '"foundingDate":"Est. 2005","areaServed":["Cook County","DuPage County"]}')
    assert signals["employees_stated"] == 11
    assert signals["founded_year"] == 2005
    assert signals["locations_stated"] is None


@pytest.mark.parametrize("value, expected", [
    ('"1998"', 1998), ('"1998-01-01"', 1998), ('"1998-05"', 1998), ('"May 1998"', 1998),
    ('"05/1998"', 1998), ("1998", 1998), ('"1776"', None), ('"2999"', None), ('""', None),
])
def test_jsonld_founding_date_shapes(value, expected):
    assert jsonld('{"@type": "Organization", "foundingDate": %s}' % value)["founded_year"] == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_jsonld_non_finite_numbers_are_ignored(value):
    # Python's JSON reader accepts these; they used to crash the whole page crawl.
    data = '{"@type": "Organization", "numberOfEmployees": %s, "foundingDate": %s}' % (value, value)
    signals = jsonld(data)
    assert signals["employees_stated"] is None and signals["founded_year"] is None
    page = analyze_page('<script type="application/ld+json">%s</script>'
                        '<a href="mailto:info@acme.com">Email</a>' % data, "https://acme.com/")
    assert page.emails and page.signals["employees_stated"] is None


def test_jsonld_other_founded_keys_and_broken_json():
    assert jsonld('{"@type": "Organization", "foundingYear": 2001}')["founded_year"] == 2001
    # A trailing comma is a common mistake; we still read the block.
    assert jsonld('{"@type": "Organization", "yearFounded": "2003",}')["founded_year"] == 2003
    assert jsonld("{not json")["founded_year"] is None


def test_text_headcount_beats_jsonld():
    page = ('<script type="application/ld+json">{"@type":"Organization","numberOfEmployees":100}</script>'
            "<p>We are a small team of 6 who love what we do.</p>")
    signals = detect_size_signals(page)
    assert signals["employees_stated"] == 6 and signals["employees_from"] == "text"


# ---------------------------------------------------------------------------
# Page analysis and crawler merge
# ---------------------------------------------------------------------------

SIZE_KEYS = {"employees_stated", "employees_from", "employees_snippet", "founded_year",
             "founded_snippet", "locations_stated", "has_careers", "careers_url"}


def test_analyze_page_includes_size_signals_but_detect_signals_does_not():
    info = analyze_page("<p>Family-owned since 1952.</p>", "https://acme.com/")
    assert SIZE_KEYS <= set(info.signals) and info.signals["founded_year"] == 1952
    assert info.signals["platform"] is None  # the old signals are still there
    assert not SIZE_KEYS & set(detect_signals("<p>Family-owned since 1952.</p>"))


def test_empty_signals_have_the_size_keys():
    empty = _empty_signals()
    assert SIZE_KEYS <= set(empty)
    assert empty["has_careers"] is False
    assert all(empty[k] is None for k in SIZE_KEYS - {"has_careers"})


def test_merge_size_signals_across_pages():
    pages = [
        # homepage: JSON-LD headcount (often template garbage), a careers link
        {"employees_stated": 100, "employees_from": "jsonld", "employees_snippet": "numberOfEmployees: 100",
         "founded_year": None, "founded_snippet": None, "locations_stated": 2,
         "has_careers": True, "careers_url": "https://acme.com/careers"},
        # about page: headcount in the text, founded year
        {"employees_stated": 12, "employees_from": "text", "employees_snippet": "Our team of 12.",
         "founded_year": 1987, "founded_snippet": "Founded in 1987.", "locations_stated": 5,
         "has_careers": False, "careers_url": None},
        # team page: another text headcount and founded year (first found wins)
        {"employees_stated": 14, "employees_from": "text", "employees_snippet": "14 employees",
         "founded_year": 1990, "founded_snippet": "Since 1990", "locations_stated": 3,
         "has_careers": True, "careers_url": "https://acme.com/jobs"},
    ]
    signals = _empty_signals()
    _merge_size_signals(signals, pages)
    assert (signals["employees_stated"], signals["employees_from"]) == (12, "text")
    assert signals["employees_snippet"] == "Our team of 12."
    assert (signals["founded_year"], signals["founded_snippet"]) == (1987, "Founded in 1987.")
    assert signals["locations_stated"] == 5
    assert signals["has_careers"] is True and signals["careers_url"] == "https://acme.com/careers"

    # Only JSON-LD anywhere: it is used. Nothing at all: everything stays empty.
    signals = _empty_signals()
    _merge_size_signals(signals, pages[:1] + [{}])
    assert (signals["employees_stated"], signals["employees_from"]) == (100, "jsonld")
    signals = _empty_signals()
    _merge_size_signals(signals, [{}, {}])
    assert signals == _empty_signals()


def test_crawl_collects_size_signals(monkeypatch):
    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(crawler, "_resolve_host", resolve)
    pages = {
        "https://acme.com/": ('<html><body><nav><a href="/about">About us</a><a href="/careers">Careers</a>'
                              '<a href="/locations">Locations</a></nav><p>EST. 1999</p></body></html>'),
        "https://acme.com/about": "<p>Family-owned since 1952. We are a small team of 6 who love what we do.</p>",
        "https://acme.com/locations": "<p>Visit any of our 7 locations for same-day service.</p>",
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await crawl_website("acme.com", client=client)

    data = asyncio.run(run())
    s = data.signals
    assert s["founded_year"] == 1999  # the homepage comes first
    assert s["employees_stated"] == 6 and s["employees_from"] == "text"
    assert s["locations_stated"] == 7  # the locations page was crawled
    assert s["has_careers"] is True and s["careers_url"] == "https://acme.com/careers"
    assert "https://acme.com/careers" not in requested  # careers pages are not crawled


def test_hostile_text_is_fast():
    hostile = [
        "team of " * 50_000,
        "team of 1 " * 30_000,
        "1 " * 200_000,
        "founded " * 50_000 + "in 1999",
        "serving " * 50_000 + "since 1999",
        "our 1 2 3 4 " * 30_000,
        "employ " * 50_000,
        ("x" * 399 + ". ") * 5000,
        "<a href='/careers-blog'>Careers advice</a>" * 20_000,
        '<script type="application/ld+json">' + "[" * 50_000 + "]" * 50_000 + "</script>",
    ]
    for text in hostile:
        started = time.perf_counter()
        detect_size_signals(text[:2_000_000])
        assert time.perf_counter() - started < 5, text[:30]
