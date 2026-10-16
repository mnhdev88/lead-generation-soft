"""Unit tests for app/enrich/extract.py (pure functions, no network, no database)."""
import time
from datetime import datetime, timezone

import pytest

from app.enrich.extract import (
    PersonFound,
    analyze_page,
    clean_email,
    decode_cfemail,
    detect_signals,
    extract_emails,
    extract_people,
    extract_phones,
    extract_socials,
    find_candidate_links,
    looks_like_name,
    merge_people,
    rank_emails,
    registered_domain,
    same_person,
    social_profile,
)

# A typical WordPress "Meet the team" page.
WORDPRESS_TEAM_PAGE = """
<!doctype html><html><head>
<title>Our Team | Bright Smile Dental</title>
<meta name="description" content="Family dentistry in Austin, TX.">
<meta name="generator" content="WordPress 6.5.2">
<link rel="stylesheet" href="https://brightsmile.com/wp-content/themes/astra/style.css">
</head><body>
<nav><ul><li><a href="/">Home</a></li><li><a href="/about-us/">About Us</a></li>
<li><a href="/our-team/">Our Team</a></li><li><a href="/contact/">Contact</a></li></ul></nav>
<h1>Meet Our Team</h1>
<div class="team-grid">
  <div class="team-member">
    <img src="/wp-content/uploads/sarah@2x.jpg" alt="">
    <h3>Dr. Sarah Mitchell</h3>
    <p class="role">Owner &amp; Lead Dentist</p>
    <a href="mailto:sarah.mitchell@brightsmile.com">Email Sarah</a>
    <a href="https://www.linkedin.com/in/sarah-mitchell-dds/">LinkedIn</a>
  </div>
  <div class="team-member">
    <h3>James O'Connor</h3>
    <p class="role">Office Manager</p>
  </div>
  <div class="team-member">
    <h3>Priya Raman, DDS</h3>
    <span>Associate Dentist</span>
  </div>
</div>
<section><h2>Our Services</h2><p>Teeth Whitening</p><p>Dental Implants</p></section>
<footer>
  <p>Questions? <a href="mailto:info@brightsmile.com">info@brightsmile.com</a></p>
  <p>&copy; 2016 - 2024 Bright Smile Dental. All Rights Reserved.</p>
</footer>
</body></html>
"""

# A Shopify store footer.
SHOPIFY_FOOTER = """
<html><head><title>Karachi Kurtas</title>
<link rel="preconnect" href="https://cdn.shopify.com">
<script src="//cdn.shopify.com/s/files/1/0001/theme.js"></script>
<script>var Shopify = Shopify || {}; Shopify.theme = {"name":"Dawn"};
window.sentry = {"dsn":"https://9f2e8c1b7a6d4e3f9a8b7c6d5e4f3a2b@o12345.ingest.sentry.io/123"};</script>
</head><body>
<footer class="site-footer">
  <ul class="social">
    <li><a href="https://www.facebook.com/sharer/sharer.php?u=https://karachikurtas.pk">Share</a></li>
    <li><a href="https://twitter.com/intent/tweet?url=x">Tweet</a></li>
    <li><a href="https://pinterest.com/pin/create/button/">Pin</a></li>
    <li><a href="https://www.facebook.com/KarachiKurtas/">Facebook</a></li>
    <li><a href="https://instagram.com/karachikurtas?igshid=abc">Instagram</a></li>
    <li><a href="https://www.instagram.com/p/C1234xyz/">Latest post</a></li>
    <li><a href="https://x.com/karachikurtas">X</a></li>
    <li><a href="https://www.youtube.com/@karachikurtas">YouTube</a></li>
    <li><a href="https://www.youtube.com/watch?v=abc123">Video</a></li>
    <li><a href="https://www.tiktok.com/@karachikurtas">TikTok</a></li>
    <li><a href="https://wa.me/923001234567?text=Hi">WhatsApp</a></li>
  </ul>
  <p>Customer care: <a href="tel:+923001234567">+92 300 1234567</a> | orders@karachikurtas.pk</p>
  <p>Order #10023451 placed 2024-03-15 for Rs. 4,500</p>
  <p>&copy; 2023, Karachi Kurtas Powered by Shopify</p>
</footer></body></html>
"""


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

def test_emails_from_mailto_text_and_url_encoding():
    html = """
    <a href="mailto:Hello%40AcmeRoofing.com?subject=Quote%20request">Email us</a>
    <a href="MAILTO:owner@acmeroofing.com">Owner</a>
    <p>Or write to office@acmeroofing.com.</p>
    """
    assert extract_emails(html) == ["hello@acmeroofing.com", "owner@acmeroofing.com", "office@acmeroofing.com"]


def test_obfuscated_emails():
    html = """
    <p>Email: info [at] lahoredental [dot] pk</p>
    <p>Bookings: bookings(at)lahoredental.pk</p>
    <p>Manager: ali {at} lahoredental {dot} com {dot} pk</p>
    <p>Or: hassan at lahoredental dot pk</p>
    <p>Entity encoded: contact&#64;lahoredental&#46;pk</p>
    """
    emails = extract_emails(html)
    assert "info@lahoredental.pk" in emails
    assert "bookings@lahoredental.pk" in emails
    assert "ali@lahoredental.com.pk" in emails
    assert "hassan@lahoredental.pk" in emails
    assert "contact@lahoredental.pk" in emails


def test_cloudflare_protected_email():
    assert decode_cfemail("422b2c242d0223212f276f322e372f202b2c256c212d2f") == "info@acme-plumbing.com"
    html = """
    <a href="/cdn-cgi/l/email-protection#422b2c242d0223212f276f322e372f202b2c256c212d2f">
      <span class="__cf_email__" data-cfemail="422b2c242d0223212f276f322e372f202b2c256c212d2f">[email&#160;protected]</span>
    </a>"""
    assert extract_emails(html) == ["info@acme-plumbing.com"]
    assert decode_cfemail("zz") is None


def test_junk_emails_are_filtered():
    html = """
    <img src="/images/logo@2x.png"> <img srcset="hero@3x.webp 3x">
    <script>var dsn = "https://0123456789abcdef0123456789abcdef@sentry.io/42";
    var w = "a1b2c3d4e5f60718293a4b5c6d7e8f90@sentry-next.wixpress.com";
    var x = "\\u003csupport@realbiz.com\\u003e";</script>
    <p>Placeholder: you@example.com, user@domain.com, yourname@email.com, john.doe@gmail.com</p>
    <p>noreply@realbiz.com, no-reply@realbiz.com, donotreply@realbiz.com</p>
    <p>jquery@3.6.0 font@1.2.woff2</p>
    <p>Real one: sales@realbiz.com</p>
    """
    emails = extract_emails(html)
    assert emails == ["sales@realbiz.com", "support@realbiz.com"]  # visible text first, then scripts


def test_clean_email_rules():
    assert clean_email(" Info@Example-Shop.CO.UK. ") == "info@example-shop.co.uk"
    assert clean_email("logo@2x.png") is None
    assert clean_email("a" * 70 + "@x.com") is None
    assert clean_email("u003einfo@shop.com") is None
    assert clean_email("info@shop") is None
    assert clean_email("") is None


def test_rank_emails_prefers_same_domain_role_addresses():
    emails = [
        "designer@webagency.com",
        "joe.bloggs@gmail.com",
        "support@acmeplumbing.com",
        "mike@acmeplumbing.com",
        "noreply@acmeplumbing.com",
        "info@acmeplumbing.com",
        "INFO@acmeplumbing.com",
        "sales@www.acmeplumbing.com",
    ]
    assert rank_emails(emails, "https://www.acmeplumbing.com/") == [
        "info@acmeplumbing.com",
        "sales@www.acmeplumbing.com",
        "mike@acmeplumbing.com",
        "support@acmeplumbing.com",
        "joe.bloggs@gmail.com",
        "designer@webagency.com",
    ]
    # also works with a bare domain
    assert rank_emails(["a@gmail.com", "info@shop.pk"], "shop.pk") == ["info@shop.pk", "a@gmail.com"]


# ---------------------------------------------------------------------------
# Phones
# ---------------------------------------------------------------------------

def test_phones_from_tel_links_and_text():
    html = """
    <a href="tel:+1-512-555-0142">Call (512) 555-0142</a>
    <a href="tel:%2B44%2020%207946%200958">London</a>
    <p>Office: 042-35761234 &nbsp; Mobile: 0300 1234567</p>
    <p>India: +91 98765 43210 or 98450 12345</p>
    <p>Toll free 1-800-555-0199</p>
    """
    phones = extract_phones(html)
    assert phones[0] == "+1-512-555-0142"
    assert "+44 20 7946 0958" in phones
    assert "042-35761234" in phones
    assert "0300 1234567" in phones
    assert "+91 98765 43210" in phones
    assert "98450 12345" in phones
    assert "1-800-555-0199" in phones
    # "(512) 555-0142" in the link text is the same number as the tel: link
    assert sum("555-0142" in p for p in phones) == 1


def test_phones_ignore_dates_ids_and_prices():
    html = """
    <p>Posted 2024-01-15, updated 15.01.2024. Order #10023451. Price $1,299.00.</p>
    <p>Tracking 1234567890123456789. Since 1998. Zip 78701. ID: 0000000.</p>
    <p>Coordinates 31.5204, 74.3587</p>
    """
    assert extract_phones(html) == []


def test_phones_dedupe_local_and_international_format():
    html = '<a href="tel:+923001234567">x</a><p>Call 0300-1234567</p>'
    assert extract_phones(html) == ["+923001234567"]


# ---------------------------------------------------------------------------
# Socials
# ---------------------------------------------------------------------------

def test_socials_ignore_share_links_and_posts():
    socials = extract_socials(SHOPIFY_FOOTER, "https://karachikurtas.pk/")
    assert socials == {
        "facebook": "https://www.facebook.com/KarachiKurtas",
        "instagram": "https://www.instagram.com/karachikurtas",
        "twitter": "https://x.com/karachikurtas",
        "youtube": "https://www.youtube.com/@karachikurtas",
        "tiktok": "https://www.tiktok.com/@karachikurtas",
        "whatsapp": "https://wa.me/923001234567",
    }


def test_social_profile_rules():
    assert social_profile("https://www.linkedin.com/company/acme-inc/about/") == (
        "linkedin", "https://www.linkedin.com/company/acme-inc")
    assert social_profile("https://pk.linkedin.com/in/ali-khan") == ("linkedin", "https://www.linkedin.com/in/ali-khan")
    assert social_profile("https://www.linkedin.com/shareArticle?url=x") is None
    assert social_profile("https://www.facebook.com/plugins/page.php?href=x") is None
    assert social_profile("https://www.facebook.com/tr?id=123&ev=PageView") is None
    assert social_profile("https://m.facebook.com/profile.php?id=100012345") == (
        "facebook", "https://www.facebook.com/profile.php?id=100012345")
    assert social_profile("https://twitter.com/acme/status/12345") is None
    assert social_profile("https://api.whatsapp.com/send?phone=+92 300 1234567") == (
        "whatsapp", "https://wa.me/923001234567")
    assert social_profile("https://wa.me/?text=hello") is None
    assert social_profile("https://www.youtube.com/channel/UC123") == ("youtube", "https://www.youtube.com/channel/UC123")


def test_linkedin_company_page_beats_personal_profile():
    html = ('<a href="https://linkedin.com/in/jane-doe-123">Jane</a>'
            '<a href="https://www.linkedin.com/company/acme">Acme</a>')
    assert extract_socials(html)["linkedin"] == "https://www.linkedin.com/company/acme"


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

def test_people_from_wordpress_team_page():
    people = extract_people(WORDPRESS_TEAM_PAGE, "https://brightsmile.com/our-team/")
    by_name = {p.full_name: p for p in people}
    assert set(by_name) == {"Sarah Mitchell", "James O'Connor", "Priya Raman"}

    sarah = by_name["Sarah Mitchell"]
    assert sarah.title == "Owner & Lead Dentist"
    assert sarah.email == "sarah.mitchell@brightsmile.com"
    assert sarah.linkedin_url == "https://www.linkedin.com/in/sarah-mitchell-dds"
    assert sarah.source_url == "https://brightsmile.com/our-team/"

    assert by_name["James O'Connor"].title == "Office Manager"
    assert by_name["James O'Connor"].email is None  # info@ in the footer is not his
    assert by_name["Priya Raman"].title == "Associate Dentist"


def test_people_inline_name_and_title():
    html = """
    <p><strong>Muhammad Bilal - Managing Partner</strong></p>
    <p>Ayesha Siddiqui, Founder &amp; CEO</p>
    <h4>Dr. Rahul Verma</h4>
    """
    people = {p.full_name: p.title for p in extract_people(html)}
    assert people == {"Muhammad Bilal": "Managing Partner", "Ayesha Siddiqui": "Founder & CEO", "Rahul Verma": "Dr."}


def test_people_title_above_name_uses_same_card():
    html = """
    <div class="card"><span>Founder</span><h3>Emma Watson</h3></div>
    <div class="card"><span>Head of Sales</span><h3>Liam Turner</h3></div>
    """
    people = {p.full_name: p.title for p in extract_people(html)}
    assert people == {"Emma Watson": "Founder", "Liam Turner": "Head of Sales"}


def test_people_are_conservative():
    html = """
    <h2>Our Services</h2><p>Office Manager</p>
    <h2>Contact Us</h2><p>Real Estate</p>
    <h3>New York</h3><p>Lorem ipsum dolor sit amet.</p>
    <h3>Book Now</h3><p>Owner operated since 1998</p>
    <h3>Privacy Policy</h3>
    <p>Our founder John Smith started the company in 1998 because he loved helping people.</p>
    <h3>Smith Dental Clinic</h3><p>Dentist</p>
    """
    assert extract_people(html) == []


def test_people_from_json_ld():
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Dentist","name":"Bright Smile Dental",
       "founder":{"@type":"Person","name":"Sarah Mitchell","sameAs":["https://www.linkedin.com/in/sarahm"]},
       "employee":[{"@type":"Person","name":"Tom Baker","jobTitle":"Hygienist","email":"mailto:tom@brightsmile.com",
                    "telephone":"+1 512 555 0100"}]},
      {"@type":"Person","name":"Blog Writer"},
      {"@type":"Article","author":{"@type":"Person","name":"Guest Author","jobTitle":"Writer"}}
    ]}
    </script>
    <script type="application/ld+json">{ not valid json </script>
    """
    people = {p.full_name: p for p in extract_people(html, "https://brightsmile.com/")}
    assert set(people) == {"Sarah Mitchell", "Tom Baker"}
    assert people["Sarah Mitchell"].title == "Founder"
    assert people["Sarah Mitchell"].linkedin_url == "https://www.linkedin.com/in/sarahm"
    assert people["Tom Baker"].title == "Hygienist"
    assert people["Tom Baker"].email == "tom@brightsmile.com"
    assert people["Tom Baker"].phone == "+1 512 555 0100"


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def test_signals_wordpress_page():
    signals = detect_signals(WORDPRESS_TEAM_PAGE)
    assert signals == {
        "platform": "WordPress",
        "copyright_year": 2024,
        "has_contact_form": False,
        "title": "Our Team | Bright Smile Dental",
        "description": "Family dentistry in Austin, TX.",
    }


def test_signals_platforms():
    assert detect_signals(SHOPIFY_FOOTER)["platform"] == "Shopify"
    assert detect_signals('<img src="https://static.wixstatic.com/media/a.jpg">')["platform"] == "Wix"
    assert detect_signals('<link href="https://static1.squarespace.com/static/x.css">')["platform"] == "Squarespace"
    assert detect_signals('<html data-wf-page="123" data-wf-site="456">')["platform"] == "Webflow"
    assert detect_signals('<img src="//img1.wsimg.com/isteam/ip/abc.jpg">')["platform"] == "GoDaddy"
    assert detect_signals('<meta name="generator" content="Joomla! - Open Source Content Management">')["platform"] == "Joomla"
    assert detect_signals('<script type="application/json" data-drupal-selector="drupal-settings-json">{}</script>')["platform"] == "Drupal"
    assert detect_signals("<html><body>Hand written</body></html>")["platform"] is None


def test_copyright_year_and_contact_form():
    next_year = datetime.now(timezone.utc).year + 1
    html = f"""
    <form action="/send"><input type="text" name="your-name"><input type="email" name="your-email">
    <textarea name="message"></textarea></form>
    <p>Established 1985. Copyright (c) 2009-2012 Old Biz. Next sale {next_year + 5}.</p>
    """
    signals = detect_signals(html)
    assert signals["copyright_year"] == 2012
    assert signals["has_contact_form"] is True
    # A search box is not a contact form
    assert detect_signals('<form><input type="search" name="q"></form>')["has_contact_form"] is False
    assert detect_signals("<p>Founded in 2020</p>")["copyright_year"] is None


# ---------------------------------------------------------------------------
# Candidate links
# ---------------------------------------------------------------------------

def test_find_candidate_links_ranking_and_filtering():
    html = """
    <a href="/">Home</a>
    <a href="/privacy-policy/">Privacy</a>
    <a href="/about-us/">About Us</a>
    <a href="https://www.brightsmile.com/contact/#form">Contact</a>
    <a href="/our-team/">Meet the team</a>
    <a href="/kontakt">Kontakt</a>
    <a href="/blog/how-to-contact-your-dentist/">Blog post</a>
    <a href="/files/contact-form.pdf">PDF</a>
    <a href="https://facebook.com/brightsmile">Facebook</a>
    <a href="https://othersite.com/contact">Other site contact</a>
    <a href="mailto:info@brightsmile.com">Email</a>
    <a href="/services/whitening/">Whitening</a>
    <a href="/page?id=7">Get in touch</a>
    """
    links = find_candidate_links(html, "https://brightsmile.com/")
    assert links == [
        "https://www.brightsmile.com/contact/",
        "https://brightsmile.com/kontakt",
        "https://brightsmile.com/page?id=7",
        "https://brightsmile.com/our-team/",
        "https://brightsmile.com/about-us/",
        "https://brightsmile.com/privacy-policy/",
    ]


def test_registered_domain():
    assert registered_domain("www.shop.example.co.uk") == "example.co.uk"
    assert registered_domain("WWW.Example.com.pk") == "example.com.pk"
    assert registered_domain("blog.example.com") == "example.com"
    assert registered_domain("93.184.216.34") == "93.184.216.34"
    assert registered_domain("") == ""


def test_analyze_page_runs_everything_once():
    info = analyze_page(SHOPIFY_FOOTER, "https://karachikurtas.pk/")
    assert info.emails == ["orders@karachikurtas.pk"]
    assert info.phones == ["+923001234567"]
    assert info.signals["platform"] == "Shopify"
    assert info.signals["copyright_year"] == 2023
    assert info.people == []
    assert "facebook" in info.socials


def test_broken_html_does_not_crash():
    html = "<div><p>Jane Doe<p>Owner</div></span></li><a href='mailto:jane@biz.com'>x<td><<<>>> &bogus; </html"
    info = analyze_page(html, "https://biz.com")
    assert info.emails == ["jane@biz.com"]
    assert analyze_page("", "https://biz.com").emails == []


# ---------------------------------------------------------------------------
# Fixes from the live test on 50 Miami dentist websites
# ---------------------------------------------------------------------------

def test_percent_encoded_mailto_gives_no_bogus_email():
    # Seen live: href="mailto:%20info@..." also produced "20info@...".
    html = """
    <a href="mailto:%20info@alfonsodental305.com">Email us</a>
    <a href="mailto:John%20Smith%20%3Cjohn@alfonsodental305.com%3E">John</a>
    <img src="/track?to=%3Cbilling@alfonsodental305.com%3E">
    """
    assert extract_emails(html) == [
        "info@alfonsodental305.com", "john@alfonsodental305.com", "billing@alfonsodental305.com"]


def test_inline_tags_do_not_glue_words_onto_emails():
    html = "<p><b>Email</b><a>info@shop.com</a></p><p>sales<span>@</span>shop.com</p>"
    assert extract_emails(html) == ["info@shop.com", "sales@shop.com"]


def test_emails_and_phones_per_page_are_capped():
    html = "".join(f"<p>person{i}@shop{i}.com</p>" for i in range(500))
    html += "".join(f'<a href="tel:+1555{i:07d}">call</a>' for i in range(500))
    info = analyze_page(html, "https://shop.com/")
    assert len(info.emails) == 50
    assert len(info.phones) == 20


# A dental homepage built like the live ones: a services dropdown menu, "eyebrow"
# headings ("Meet Our Doctor"), review cards and a doctor card with glued-on text.
DENTAL_HOMEPAGE = """
<html><body>
<nav class="navbar"><a href="/">Home</a>
  <div class="dropdown"><a href="/oral-surgery-miami/" class="dropdown__link">Oral Surgery</a>
  <a href="/emergency-dentist-miami/" class="dropdown__link">Emergency Dentist</a>
  <a href="/root-canal/" class="dropdown__link">Root Canal</a>
  <a href="/periodontics/" class="dropdown__link">Periodontist</a></div>
  <div class="dropdown"><a href="/dr-melanie-kucharski-kalb">Dr. Melanie Kucharski-Kalb</a>
  <a href="/dr-cassandra-iglesias">Dr. Cassandra Iglesias</a></div>
</nav>
<section><a href="https://financing.example/">See Financing Options</a></section>
<section id="doctor"><span class="eyebrow">Meet Our Doctor</span>
  <h2>Experience, Precision, and Personalized Smile Care</h2></section>
<div class="doctor-card"><span class="eyebrow">Meet Your Cosmetic Dentist</span>
  <h2>Dr. Maria Becerra <span class="pill">25+ Years Experience</span></h2>
  <a href="/about/" class="button">Learn More</a></div>
<div class="layout19"><h2>Cassandra Iglesias, DMD, MSDr. Kalb</h2>
  <p>Dr. Melanie Kucharski-Kalb has spent years turning dental visits into calm, positive experiences.</p></div>
<div class="reviews">
  <div class="review"><p>Liz Goings</p><p>The doctor is super good.</p></div>
  <div class="review"><p>Robert Gilliam</p><p>I highly recommend My Dentist in Miami</p></div>
</div>
<ul class="services"><li>Teeth Whitening</li><li>Cosmetic Dentist – Miami</li>
  <li>Tooth Extractions</li><li>Find a dentist near you</li></ul>
<div lang="es"><p>Tu Odontóloga</p><p>DMD – Odontología Estética</p>
  <p>Horas Laborales</p><p>Director Técnico Dental</p></div>
</body></html>
"""


def test_people_ignore_menus_buttons_services_and_reviews():
    people = {p.full_name: p.title for p in extract_people(DENTAL_HOMEPAGE, "https://smile.com/")}
    assert people == {
        "Melanie Kucharski-Kalb": "Dr.",  # from the "Dr." in the menu link itself
        "Cassandra Iglesias": "Dr.",  # menu "Dr." and card "DMD"; the glued "MSDr. Kalb" is dropped
        "Maria Becerra": "Dr.",  # not "Meet Your Cosmetic Dentist"
    }


def test_glued_text_and_credentials_are_not_titles():
    html = "<h2>Cassandra Iglesias, DMD, MSDr. Kalb</h2><p>Some long biography text goes here, really.</p>"
    assert [(p.full_name, p.title) for p in extract_people(html)] == [("Cassandra Iglesias", "DMD")]
    html = "<div><h3>Helena Pocaterra, DDS, MS</h3><p>Orthodontist</p></div>"
    assert [(p.full_name, p.title) for p in extract_people(html)] == [("Helena Pocaterra", "Orthodontist")]


TEAM_CARDS = """
<section class="team">
  <div class="team-card"><img src="/a.jpg" alt=""><h3>Dr. David Weiner</h3>
    <a href="/team/david-weiner" class="btn">View Profile</a></div>
  <div class="team-card"><h3>Andy T. Vu, DDS</h3><p>Oral Surgeon</p>
    <a href="/book" class="btn">Book Now</a></div>
  <div class="team-card"><h3>Lauren Light</h3><p>Our Office Manager</p>
    <a href="mailto:lauren@bayfront.com">Email Lauren</a></div>
  <div class="team-card"><h3>John Smith Jr.</h3><p>Read More</p></div>
</section>
<section class="bio"><h2>David J. Weiner</h2><p>General and Cosmetic Dentist</p>
  <p>Dr. Weiner graduated from the University of Florida and has practiced in Miami since 2001.</p></section>
"""


def test_people_from_team_cards_and_bios_are_deduplicated():
    people = extract_people(TEAM_CARDS, "https://bayfront.com/team/")
    assert [(p.full_name, p.title, p.email) for p in people] == [
        ("David J. Weiner", "General and Cosmetic Dentist", None),  # merged with "Dr. David Weiner"
        ("Andy T. Vu", "Oral Surgeon", None),
        ("Lauren Light", "Office Manager", "lauren@bayfront.com"),
    ]  # "John Smith Jr." has no job title ("Read More" is a button), so he is skipped


def test_same_person_and_merge_keep_the_most_complete_record():
    assert same_person("David Weiner", "David J. Weiner")
    assert same_person("Dr. David Weiner, DMD", "david weiner")
    assert same_person("Raúl García", "Raul Garcia")
    assert same_person("Mario Garcia", "Mario Garcia Caceres")
    assert not same_person("David Weiner", "Daniel Weiner")
    assert not same_person("Raul Garcia", "Concepcion Sanchez-Garcia")

    merged = merge_people([
        PersonFound("Raul Garcia", "Dr.", source_url="https://x.com/"),
        PersonFound("Raúl García", "Founder", email="raul@x.com"),
        PersonFound("Ana Lopez", "Hygienist"),
    ])
    assert [(p.full_name, p.title, p.email, p.source_url) for p in merged] == [
        ("Raul Garcia", "Founder", "raul@x.com", "https://x.com/"),
        ("Ana Lopez", "Hygienist", None, None),
    ]


def test_json_ld_names_lose_prefix_and_credentials():
    html = """<script type="application/ld+json">
    {"@type": "Dentist", "employee": [{"@type": "Person", "name": "Dr. David J. Weiner, DMD"},
                                      {"@type": "Person", "name": "Teeth Whitening"}]}
    </script>"""
    assert [(p.full_name, p.title) for p in extract_people(html)] == [("David J. Weiner", "Dr.")]


def test_service_and_category_words_are_never_names():
    for text in ["Oral Surgery", "Teeth Whitening", "Root Canal", "Smile Gallery", "Family Dentistry",
                 "Pediatric Care", "Insurance Financing", "Tu Odontóloga", "Horas Laborales",
                 "Book Appointment", "Our Team", "Miami Realty", "Smith Law"]:
        assert not looks_like_name(text), text
    for text in ["Jane Smith", "Andy T. Vu", "Marta Ortiz-Perez", "Carolis de Los Reyes", "Taylor Light"]:
        assert looks_like_name(text), text


# ---------------------------------------------------------------------------
# Hostile pages: everything must stay fast (no catastrophic regex backtracking)
# ---------------------------------------------------------------------------

_SIZE = 300_000
ADVERSARIAL_PAGES = {
    "dots": "<p>" + "a." * (_SIZE // 2) + "</p>",
    "spaces": "<p>x" + " " * _SIZE + "y</p>",
    "email_dots": "<p>a@" + "a." * (_SIZE // 2) + "!</p>",
    "plus_digits": "<p>" + ("+1" + "1" * 40 + " ") * (_SIZE // 43) + "</p>",
    "at_words": "<p>" + "x at y " * (_SIZE // 7) + "</p>",
    "people_cards": "<div><h3>Jane Smith</h3><p>Owner</p></div>" * (_SIZE // 45),
    "unique_emails": "".join(f"<p>u{i}@site{i}.com</p>" for i in range(_SIZE // 25)),
    "deep_nesting": "<div>" * (_SIZE // 5) + "<p>Jane Smith</p><p>Owner</p>" * 1000,
    "many_lt": "<" * _SIZE,
    "many_links": '<a href="/contact">Contact us</a>' * (_SIZE // 33),
    "deep_json": '<script type="application/ld+json">' + "[" * 50000 + "]" * 50000 + "</script>",
}


@pytest.mark.parametrize("name", list(ADVERSARIAL_PAGES))
def test_hostile_pages_are_analyzed_quickly(name):
    started = time.perf_counter()
    info = analyze_page(ADVERSARIAL_PAGES[name], "https://site.com/")
    assert time.perf_counter() - started < 10  # the old regexes took minutes on some of these
    assert len(info.emails) <= 50 and len(info.people) <= 40 and len(info.phones) <= 20


def test_unclosed_tags_do_not_freeze_the_parser():
    # "<a <a <a ..." made Python's HTMLParser take ~50 s for 25 KB (minutes to days for 2 MB).
    start = time.monotonic()
    analyze_page("<a " * 7000, "https://x.com/")
    analyze_page("<!--" + "<a " * 7000, "https://x.com/")
    assert time.monotonic() - start < 2
    # Comments that contain tags are still skipped, and the page after them is read.
    html = "<!-- <p>Old: old@shop.com</p> --><p>Email <a href='mailto:info@shop.com'>us</a></p>"
    assert "info@shop.com" in extract_emails(html)


def test_father_and_son_or_different_initials_are_different_people():
    assert not same_person("David Weiner Sr.", "David Weiner Jr.")
    assert not same_person("John A. Smith", "John B. Smith")
    assert same_person("David Weiner", "David J. Weiner")
    assert same_person("David Weiner Jr.", "David Weiner, Jr")
    merged = merge_people([PersonFound(full_name="David Weiner Sr.", title="Founder"),
                           PersonFound(full_name="David Weiner Jr.", title="Dentist")])
    assert [p.full_name for p in merged] == ["David Weiner Sr.", "David Weiner Jr."]


def test_street_names_are_not_people():
    html = "<div class='card'><h3>Martin Luther King Blvd</h3><p>Manager</p></div>"
    assert all("Blvd" not in p.full_name for p in extract_people(html))


def test_url_encoded_space_is_not_part_of_an_email():
    assert "x%20info@shop.com" not in extract_emails("<p>x%20info@shop.com</p>")
    assert clean_email("x%20info@shop.com") is None
