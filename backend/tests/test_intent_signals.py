"""Tests for the buying-intent website signals (extract._intent_signals_from) and how the
crawler merges them and reads job titles from the careers page. No network."""
import asyncio
import time

import httpx
import pytest

from app.enrich import crawler
from app.enrich.crawler import _empty_signals, _merge_intent_signals, crawl_website
from app.enrich.extract import analyze_page, detect_intent_signals, detect_signals

URL = "https://acmedental.com/"

INTENT_KEYS = {"has_analytics", "has_search_console", "ad_pixels", "has_local_schema",
               "has_online_booking", "booking_provider", "has_online_payment", "has_shop",
               "has_online_ordering", "has_pdf_forms", "has_live_chat", "has_viewport",
               "hiring_roles", "hiring_titles", "has_tag_manager"}


# Ordinary visible text. A page with almost no text is treated as built by JavaScript
# ("can't tell", see test_javascript_built_pages_say_unknown), so the snippet tests add this.
FILLER = ("<p>" + "Acme Dental is a friendly family dental practice in Austin, Texas, open six days. " * 4
          + "</p>")


def sig(html: str, url: str = URL, careers: bool = False) -> dict:
    return detect_intent_signals(html + FILLER, url, careers=careers)


# ---------------------------------------------------------------------------
# Keys and defaults
# ---------------------------------------------------------------------------

def test_empty_page_has_every_key_and_nothing_found():
    s = sig("<html><body><p>Welcome to Acme Dental.</p></body></html>")
    assert set(s) == INTENT_KEYS
    assert not any(s[k] for k in INTENT_KEYS - {"booking_provider"})
    assert s["booking_provider"] is None and s["has_viewport"] is False


def test_analyze_page_includes_intent_keys_but_detect_signals_does_not():
    info = analyze_page('<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC123"></script>', URL)
    assert INTENT_KEYS <= set(info.signals) and info.signals["has_analytics"] is True
    assert set(detect_signals("<p>x</p>")) == {"platform", "copyright_year", "has_contact_form",
                                               "title", "description"}


def test_empty_signals_have_the_intent_keys():
    empty = _empty_signals()
    assert INTENT_KEYS <= set(empty)
    assert empty["has_viewport"] is None and empty["booking_provider"] is None
    assert empty["ad_pixels"] == [] and empty["hiring_roles"] == [] and empty["hiring_titles"] == []
    assert all(empty[k] is False for k in INTENT_KEYS if k.startswith("has_") and k != "has_viewport")


# ---------------------------------------------------------------------------
# Analytics / Search Console
# ---------------------------------------------------------------------------

GA4 = """<script async src="https://www.googletagmanager.com/gtag/js?id=G-1A2B3C4D5E"></script>
<script>window.dataLayer = window.dataLayer || []; function gtag(){dataLayer.push(arguments);}
gtag('js', new Date()); gtag('config', 'G-1A2B3C4D5E');</script>"""

GTM = """<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});
var f=d.getElementsByTagName(s)[0],j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;
j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
})(window,document,'script','dataLayer','GTM-ABC1234');</script>
<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-ABC1234" height="0" width="0"></iframe></noscript>"""


@pytest.mark.parametrize("html", [
    GA4,
    GTM,
    '<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-XYZ9"></iframe></noscript>',
    '<script src="https://www.google-analytics.com/analytics.js"></script>',
    "<script>ga('create', 'UA-12345-1', 'auto'); ga('send', 'pageview');</script>",
    # inline config only (the loader added by a plugin elsewhere)
    "<script>gtag('config', 'G-ZZZ9999');</script>",
    # consent tool: tag waits for consent but is installed
    '<script type="text/plain" data-cookieconsent="statistics">gtag("config", "G-QWERTY12");</script>',
    '<script src="//www.googletagmanager.com/gtag/js?id=UA-1234-5"></script>',
])
def test_analytics_found(html):
    assert sig(html)["has_analytics"] is True


@pytest.mark.parametrize("html", [
    # privacy policy text that mentions Google Analytics
    "<p>We use Google Analytics (google-analytics.com) and Google Tag Manager "
    "(googletagmanager.com/gtm.js) to understand traffic. gtag('config', 'G-EXAMPLE1').</p>",
    # opt-out link in a privacy policy
    '<a href="https://tools.google.com/dlpage/gaoptout">Opt out of Google Analytics</a>',
    '<a href="https://analytics.google.com/">Google Analytics</a>',
    # Google Ads tag only: that's advertising, not analytics
    '<script async src="https://www.googletagmanager.com/gtag/js?id=AW-123456789"></script>',
    # just a preconnect hint
    '<link rel="preconnect" href="https://www.googletagmanager.com">',
    # a JSON data block (CMS content) that talks about GTM
    '<script type="application/json">{"post": "How to install googletagmanager.com/gtm.js"}</script>',
    # a template block
    '<script type="text/x-template"><p>gtag(\'config\', \'G-ABCDEF1\')</p></script>',
])
def test_analytics_not_found(html):
    assert sig(html)["has_analytics"] is False


def test_search_console():
    assert sig('<meta name="google-site-verification" content="abc123XYZ">')["has_search_console"] is True
    assert sig('<meta name="google-site-verification" content="">')["has_search_console"] is False
    assert sig('<meta name="msvalidate.01" content="abc">')["has_search_console"] is False
    assert sig("<p>Verify with google-site-verification in Search Console.</p>")["has_search_console"] is False


# ---------------------------------------------------------------------------
# Ad pixels
# ---------------------------------------------------------------------------

META_PIXEL = """<script>!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){};
t=b.createElement(e);t.async=!0;t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}
(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '1234567890'); fbq('track', 'PageView');</script>"""


def test_ad_pixels_each_network():
    assert sig('<script src="https://www.googletagmanager.com/gtag/js?id=AW-987654321"></script>')["ad_pixels"] == ["google_ads"]
    assert sig("<script>gtag('config', 'AW-987654321');</script>")["ad_pixels"] == ["google_ads"]
    assert sig('<script src="https://www.googleadservices.com/pagead/conversion.js"></script>')["ad_pixels"] == ["google_ads"]
    assert sig('<iframe src="https://googleads.g.doubleclick.net/pagead/viewthroughconversion/1/"></iframe>')["ad_pixels"] == ["google_ads"]
    assert sig(META_PIXEL)["ad_pixels"] == ["meta"]
    assert sig("<script>!function (w, d, t) {w.TiktokAnalyticsObject=t; var u='https://analytics.tiktok.com/i18n/pixel/events.js';}"
               "(window, document, 'ttq');</script>")["ad_pixels"] == ["tiktok"]
    assert sig('<script>_linkedin_partner_id = "12345";</script>')["ad_pixels"] == ["linkedin"]
    assert sig('<script src="https://snap.licdn.com/li.lms-analytics/insight.min.js"></script>')["ad_pixels"] == ["linkedin"]
    assert sig("<script>(function(w,d,t,r,u){var f,n,i;w[u]=w[u]||[],f=function(){};n=d.createElement(t),"
               "n.src='//bat.bing.com/bat.js'})(window,document,'script','//bat.bing.com/bat.js','uetq');</script>")["ad_pixels"] == ["bing"]


def test_ad_pixels_sorted_unique():
    html = META_PIXEL + "<script>gtag('config','AW-111111111'); gtag('config','AW-222222222');</script>" + META_PIXEL
    assert sig(html)["ad_pixels"] == ["google_ads", "meta"]


@pytest.mark.parametrize("html", [
    # Facebook "Like" / page plugin SDK is not the ads pixel
    '<script async defer src="https://connect.facebook.net/en_US/sdk.js#xfbml=1&version=v18.0"></script>',
    '<a href="https://www.facebook.com/acmedental">Follow us on Facebook</a>',
    '<a href="https://www.tiktok.com/@acmedental">TikTok</a>',
    '<a href="https://www.linkedin.com/company/acme">LinkedIn</a>',
    '<blockquote class="tiktok-embed"></blockquote><script async src="https://www.tiktok.com/embed.js"></script>',
    "<p>We advertise on Google Ads (googleadservices.com) and Bing (bat.bing.com).</p>",
    # GA4 analytics tag only
    GA4,
    # an unrelated "draw-1234567" in code
    "<script>var cls = 'draw-1234567'; var x = 'saw-12345678';</script>",
])
def test_ad_pixels_not_found(html):
    assert sig(html)["ad_pixels"] == []


# ---------------------------------------------------------------------------
# LocalBusiness / Organization schema
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("schema_type", [
    '"Dentist"', '"LocalBusiness"', '"Organization"', '"https://schema.org/Plumber"',
    '["Attorney", "LegalService"]', '"MedicalClinic"', '"HairSalon"', '"AutoRepair"', '"Restaurant"',
    '"schema:Electrician"',
])
def test_local_schema_types(schema_type):
    html = f'<script type="application/ld+json">{{"@context": "https://schema.org", "@type": {schema_type}, "name": "Acme"}}</script>'
    assert sig(html)["has_local_schema"] is True


def test_local_schema_inside_graph_and_microdata():
    graph = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
             '{"@type":"WebSite","name":"Acme"},{"@type":"Dentist","name":"Acme Dental"}]}</script>')
    assert sig(graph)["has_local_schema"] is True
    micro = '<div itemscope itemtype="https://schema.org/LocalBusiness"><span itemprop="name">Acme</span></div>'
    assert sig(micro)["has_local_schema"] is True


@pytest.mark.parametrize("html", [
    '<script type="application/ld+json">{"@type": "WebSite", "name": "Acme"}</script>',
    '<script type="application/ld+json">{"@type": "BreadcrumbList", "itemListElement": []}</script>',
    '<script type="application/ld+json">{"@type": "Person", "name": "Dr. Jane Smith"}</script>',
    '<script type="application/ld+json">{"@type": "WebPage", "about": "Our LocalBusiness"}</script>',
    '<script type="application/ld+json">{not json</script>',
    "<p>We are a LocalBusiness Dentist Organization.</p>",
    '<div itemscope itemtype="https://example.com/Organization"></div>',
])
def test_local_schema_not_found(html):
    assert sig(html)["has_local_schema"] is False


# ---------------------------------------------------------------------------
# Online booking
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html, provider", [
    ('<a href="https://calendly.com/acme-dental/consult">Book a consult</a>', "Calendly"),
    ('<div class="calendly-inline-widget" data-url="https://calendly.com/acme"></div>'
     '<script src="https://assets.calendly.com/assets/external/widget.js"></script>', "Calendly"),
    ('<a href="https://app.acuityscheduling.com/schedule.php?owner=123456">Schedule</a>', "Acuity Scheduling"),
    ('<a href="https://acmespa.as.me/">Book</a>', "Acuity Scheduling"),
    ('<iframe src="https://app.acuityscheduling.com/schedule.php?owner=1"></iframe>', "Acuity Scheduling"),
    ('<a href="https://www.zocdoc.com/practice/acme-dental-12345">Book on Zocdoc</a>', "Zocdoc"),
    ('<script src="https://offsiteschedule.zocdoc.com/plugin/embed"></script>', "Zocdoc"),
    ('<a href="https://www.opentable.com/r/acme-bistro-austin">Reserve</a>', "OpenTable"),
    ('<script src="//www.opentable.com/widget/reservation/loader?rid=1234"></script>', "OpenTable"),
    ('<a href="https://resy.com/cities/ny/acme-bistro">Reservations</a>', "Resy"),
    ('<a href="https://www.vagaro.com/acmesalon">Book</a>', "Vagaro"),
    ('<a href="https://clients.mindbodyonline.com/classic/ws?studioid=1234">Classes</a>', "Mindbody"),
    ('<script src="https://widgets.mindbodyonline.com/javascripts/healcode.js"></script>', "Mindbody"),
    ('<a href="https://booksy.com/en-us/12345_acme-barbers_barber-shop_123_austin">Book</a>', "Booksy"),
    ('<a href="https://www.schedulicity.com/scheduling/ACM123">Book</a>', "Schedulicity"),
    ('<a href="https://acme.setmore.com/">Book</a>', "Setmore"),
    ('<a href="https://acme.simplybook.me/v2/">Book</a>', "SimplyBook.me"),
    ('<a href="https://www.fresha.com/a/acme-salon-austin-123">Book</a>', "Fresha"),
    ('<a href="https://www.styleseat.com/m/v/janedoe">Book</a>', "StyleSeat"),
    ('<a href="https://squareup.com/appointments/book/abc123/LXYZ/start">Book</a>', "Square Appointments"),
    ('<a href="https://acme-salon.square.site/book">Book</a>', "Square Appointments"),
    ('<a href="https://www.localmed.com/dentists/acme-dental/">Request</a>', "LocalMed"),
    ('<a href="https://app.nexhealth.com/appt/acme-dental">Book</a>', "NexHealth"),
    ('<a href="https://www.healthgrades.com/dentist/dr-jane-smith-abc12#book-appointment">Book</a>', None),
    ('<a href="https://www.healthgrades.com/book/dr-jane-smith-abc12">Book</a>', "Healthgrades"),
    ('<a href="https://book.housecallpro.com/book/Acme-Plumbing/abc">Book online</a>', "Housecall Pro"),
    ('<script src="https://online-booking.housecallpro.com/script.js?token=abc"></script>', "Housecall Pro"),
    ('<a href="https://clienthub.getjobber.com/booking/abc-123">Book</a>', "Jobber"),
    ('<script src="https://embed.scheduler.servicetitan.com/scheduler-v1.js"></script>', "ServiceTitan"),
    ('<a href="https://meetings.hubspot.com/jane-acme">Book a call</a>', "HubSpot Meetings"),
    ('<a href="https://www.practo.com/bangalore/doctor/dr-jane-dentist">Book</a>', "Practo"),
])
def test_booking_platforms(html, provider):
    s = sig(html)
    assert s["booking_provider"] == provider
    assert s["has_online_booking"] is (provider is not None)


@pytest.mark.parametrize("html", [
    '<a href="/book-online/">Book Online</a>',
    '<a href="/appointments/request">Request an Appointment</a>',
    '<a href="https://acmedental.com/schedule">Schedule Online</a>',
    '<a href="/booking">Book Now »</a>',
    '<a href="/request-an-appointment/">Book an appointment</a>',
    '<a href="https://www.acmedental.com/reservations">Reserve a Table</a>',
])
def test_booking_own_form(html):
    s = sig(html)
    assert s["has_online_booking"] is True and s["booking_provider"] == "own form"


@pytest.mark.parametrize("html", [
    # announced, no link
    "<p>Easy online booking coming soon!</p>",
    "<h2>Book Online</h2><p>Call us to book: (512) 555-0100</p>",
    # Facebook "Book now" share / page buttons
    '<a href="https://www.facebook.com/sharer/sharer.php?u=https://acmedental.com">Book now</a>',
    '<a href="https://www.facebook.com/acmedental/services">Book Now</a>',
    # "Book now" links that go nowhere bookable
    '<a href="#">Book Now</a>',
    '<a href="tel:+15125550100">Book Now</a>',
    '<a href="mailto:info@acmedental.com">Book an appointment</a>',
    '<a href="/contact-us/">Book an Appointment</a>',
    '<a href="/">Book Now</a>',
    # blog post about booking
    '<a href="/blog/how-to-book-an-appointment">Book an appointment</a>',
    # booking-looking path but the text is not a booking button
    '<a href="/appointments/">Our hours</a>',
    # provider homepages / marketing pages ("powered by" links)
    '<a href="https://calendly.com/">Calendly</a>',
    '<a href="https://www.vagaro.com/pro/">Powered by Vagaro</a>',
    '<a href="https://www.zocdoc.com/about/">Zocdoc</a>',
    '<a href="https://squareup.com/us/en/appointments">Square Appointments</a>',
    '<a href="https://www.housecallpro.com/features/online-booking/">Housecall Pro</a>',
    '<a href="https://pos.toasttab.com/">Powered by Toast</a>',
    # another website's booking page wording, not a provider
    '<a href="https://www.yelp.com/biz/acme-dental-austin">Book now</a>',
])
def test_booking_not_found(html):
    s = sig(html)
    assert s["has_online_booking"] is False and s["booking_provider"] is None


# ---------------------------------------------------------------------------
# Online payment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    '<script src="https://js.stripe.com/v3/"></script>',
    '<a href="https://buy.stripe.com/test_abc123">Pay deposit</a>',
    '<a href="https://checkout.stripe.com/c/pay/cs_live_abc">Checkout</a>',
    '<script src="https://www.paypal.com/sdk/js?client-id=abc"></script>',
    '<form action="https://www.paypal.com/cgi-bin/webscr" method="post"><input type="hidden" name="cmd" value="_s-xclick"></form>',
    '<a href="https://paypal.me/acmedental">PayPal</a>',
    '<a href="https://www.paypal.com/paypalme/acmedental">PayPal</a>',
    '<script src="https://web.squarecdn.com/v1/square.js"></script>',
    '<a href="https://square.link/u/AbC123">Pay</a>',
    '<a href="https://secure.lawpay.com/pages/acmelaw/operating">Pay Online</a>',
    '<a href="https://acme.paysimple.com/">Pay your bill</a>',
    '<a href="/pay-online/">Pay Your Bill</a>',
    '<a href="/payments">Make a Payment</a>',
    '<a href="https://connect.intuit.com/portal/app/CommerceNetwork/view/abc">Pay Invoice</a>',
])
def test_payment_found(html):
    assert sig(html)["has_online_payment"] is True


@pytest.mark.parametrize("html", [
    # footer link to the Stripe / PayPal / Square homepages
    '<a href="https://stripe.com/">Payments by Stripe</a>',
    '<a href="https://stripe.com/payments">Stripe</a>',
    '<a href="https://www.paypal.com/">PayPal</a>',
    '<a href="https://squareup.com/us/en">Square</a>',
    # wording without a payment link
    "<p>We accept all major credit cards and you can pay online soon.</p>",
    '<a href="#">Pay Online</a>',
    '<a href="/contact">Pay your bill</a>',
    '<a href="https://www.paysimple.com/">PaySimple</a>',
    '<img src="https://www.paypalobjects.com/webstatic/mktg/logo/AM_mc_vs_dc_ae.jpg" alt="We accept PayPal">',
    "<p>Payment plans available</p>",
])
def test_payment_not_found(html):
    assert sig(html)["has_online_payment"] is False


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    '<script src="https://cdn.shopify.com/s/files/1/0001/theme.js"></script>',
    "<script>window.Shopify = window.Shopify || {}; Shopify.theme = {\"name\":\"Dawn\"};</script>",
    '<script src="https://sdks.shopifycdn.com/buy-button/latest/buy-button-storefront.min.js"></script>',
    '<script src="https://cdn11.bigcommerce.com/s-abc/stencil/theme.js"></script>',
    '<script src="https://app.ecwid.com/script.js?123" charset="utf-8"></script>',
    '<form action="/cart/add" method="post"><button type="submit">Add</button></form>',
    '<button class="single_add_to_cart_button button alt">Add to cart</button>',
    '<a href="/?add-to-cart=123" class="button add_to_cart_button">Add to cart</a>',
    '<script src="/wp-content/plugins/woocommerce/assets/js/frontend/cart-fragments.min.js"></script>'
    '<a href="/cart/">Cart</a>',
])
def test_shop_found(html):
    s = sig(html)
    assert s["has_shop"] is True
    assert s["has_online_payment"] is True  # a shop takes payments at checkout


@pytest.mark.parametrize("html", [
    "<p>Visit our shop on Main Street! Add to cart coming soon.</p>",
    '<a href="https://www.shopify.com/">Shopify</a>',
    '<img src="https://cdn.shopify.com/s/files/1/logo.png">',
    # WooCommerce plugin files alone (installed but no shop links)
    '<script src="/wp-content/plugins/woocommerce/assets/js/frontend/woocommerce.min.js"></script>',
    '<a href="/shop-talk-podcast">Podcast</a>',
])
def test_shop_not_found(html):
    assert sig(html)["has_shop"] is False


# ---------------------------------------------------------------------------
# Online ordering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    '<a href="https://order.toasttab.com/online/acme-pizza-123-main-st">Order Online</a>',
    '<a href="https://www.toasttab.com/acme-pizza/v3">Order</a>',
    '<a href="https://ordering.chownow.com/order/1234/locations">Order</a>',
    '<a href="https://www.doordash.com/store/acme-pizza-austin-123/">DoorDash</a>',
    '<a href="https://www.ubereats.com/store/acme-pizza/abc123">Uber Eats</a>',
    '<a href="https://www.grubhub.com/restaurant/acme-pizza-123-main-st-austin/1234">Grubhub</a>',
    '<a href="https://slicelife.com/restaurants/tx/austin/78701/acme-pizza/menu">Slice</a>',
    '<a href="https://order.online/store/acme-pizza-123">Order</a>',
    '<a href="https://acme-pizza.square.site/">Order Online</a>',
    '<a href="https://order.acmepizza.com/">Order Now</a>',
    '<script src="https://www.fbgcdn.com/embedder/js/ewm2.js" defer async></script>',
    '<a href="https://www.foodpanda.pk/restaurant/ab12/acme-karahi">foodpanda</a>',
])
def test_ordering_found(html):
    assert sig(html, "https://acmepizza.com/")["has_online_ordering"] is True


@pytest.mark.parametrize("html", [
    "<p>Online ordering coming soon. Call to order: 512-555-0100</p>",
    '<a href="https://www.doordash.com/">DoorDash</a>',
    '<a href="https://www.doordash.com/dasher/signup/">Become a Dasher</a>',
    '<a href="https://pos.toasttab.com/">Powered by Toast</a>',
    '<a href="https://www.ubereats.com/">Uber Eats</a>',
    '<a href="/menu.pdf">Order Online</a>',
    '<a href="#">Order Now</a>',
    '<a href="tel:5125550100">Order Now</a>',
    '<a href="https://acme-pizza.square.site/">Visit our store</a>',
])
def test_ordering_not_found(html):
    assert sig(html, "https://acmepizza.com/")["has_online_ordering"] is False


# ---------------------------------------------------------------------------
# PDF forms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    '<a href="/wp-content/uploads/2023/01/new-patient-forms.pdf">Download</a>',
    '<a href="/files/intake.pdf">Client intake</a>',
    '<a href="/docs/doc123.pdf">New Patient Paperwork</a>',
    '<a href="https://acmedental.com/Registration%20Form.pdf">Registration</a>',
    '<a href="https://static1.squarespace.com/static/abc/t/123/Medical_History.pdf">Medical history</a>',
    '<a href="/employment-application.pdf">Apply</a>',
    '<a href="https://img1.wsimg.com/blobby/go/abc/downloads/Consent.pdf?ver=1">Consent</a>',
])
def test_pdf_forms_found(html):
    assert sig(html)["has_pdf_forms"] is True


@pytest.mark.parametrize("html", [
    '<a href="/menu.pdf">Our Menu (PDF)</a>',
    '<a href="/files/brochure-2024.pdf">Brochure</a>',
    '<a href="/docs/information.pdf">More information</a>',
    '<a href="/docs/platform-overview.pdf">Overview</a>',
    # forms that are web pages, not PDFs
    '<a href="/new-patient-forms/">New Patient Forms</a>',
    '<a href="/forms.html">Forms</a>',
    # a PDF form on another website (e.g. the IRS)
    '<a href="https://www.irs.gov/pub/irs-pdf/fw9.pdf">W-9 Form</a>',
    "<p>Please print our new patient forms (PDF) before your visit.</p>",
])
def test_pdf_forms_not_found(html):
    assert sig(html)["has_pdf_forms"] is False


# ---------------------------------------------------------------------------
# Live chat
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    "<script>window.intercomSettings = {app_id: 'abc'};(function(){var s=d.createElement('script');"
    "s.src='https://widget.intercom.io/widget/abc';})();</script>",
    '<script src="https://js.driftt.com/include/123/abc.js"></script>',
    "<script>var Tawk_API=Tawk_API||{};(function(){var s1=document.createElement('script');"
    "s1.src='https://embed.tawk.to/abc/default';})();</script>",
    '<script src="https://cdn.livechatinc.com/tracking.js"></script>',
    '<script id="ze-snippet" src="https://static.zdassets.com/ekr/snippet.js?key=abc"></script>',
    '<script src="//code.tidio.co/abcdef.js" async></script>',
    '<script>window.$crisp=[];window.CRISP_WEBSITE_ID="abc";(function(){s.src="https://client.crisp.chat/l.js";})();</script>',
    '<script src="https://connect.podium.com/widget.js#ORG_TOKEN=abc" id="podium-widget"></script>',
    '<script src="//js.usemessages.com/conversations-embed.js"></script>',
])
def test_live_chat_found(html):
    assert sig(html)["has_live_chat"] is True


@pytest.mark.parametrize("html", [
    "<p>Chat with us live! Live chat available 9-5.</p>",
    '<a href="https://www.tawk.to/">Free live chat by tawk.to</a>',
    '<a href="https://www.intercom.com/">Intercom</a>',
    '<a href="https://www.podium.com/">Podium</a>',
    # HubSpot tracking code alone doesn't prove a chat widget is switched on
    '<script id="hs-script-loader" async defer src="//js.hs-scripts.com/123456.js"></script>',
])
def test_live_chat_not_found(html):
    assert sig(html)["has_live_chat"] is False


# ---------------------------------------------------------------------------
# Mobile viewport
# ---------------------------------------------------------------------------

def test_viewport():
    assert sig('<meta name="viewport" content="width=device-width, initial-scale=1">')["has_viewport"] is True
    assert sig('<meta name="Viewport" content="initial-scale=1.0, width = device-width">')["has_viewport"] is True
    assert sig('<meta name="viewport" content="width=1024">')["has_viewport"] is False
    assert sig("<p>width=device-width</p>")["has_viewport"] is False


# ---------------------------------------------------------------------------
# Job titles (careers page)
# ---------------------------------------------------------------------------

CAREERS_PAGE = """
<html><body>
<nav class="main-menu"><a href="/">Home</a><a href="/team">Our Dentists</a><a href="/careers">Careers</a></nav>
<h1>Join Our Team</h1>
<p>We are a growing family dental practice in Austin. We offer great benefits.</p>
<h2>Open Positions</h2>
<div class="job"><h3>Front Desk Receptionist</h3><p>Full-time. Answer phones and greet patients.</p>
<ul><li>Assist the office manager with scheduling</li><li>Answer phones</li></ul>
<a href="/careers/front-desk">Apply Now</a></div>
<div class="job"><h3>Dental Assistant - Part Time</h3></div>
<div class="job"><h3>Scheduling Coordinator</h3></div>
<div class="job"><h3>Marketing &amp; Social Media Manager</h3></div>
<div class="job"><h3>Office Manager</h3></div>
<div class="job"><h3>Customer Service Representative (Remote)</h3></div>
<div class="job"><h3>IT Support Specialist</h3></div>
<div class="job"><h3>Sales Associate</h3></div>
<div class="job"><h3>Data Entry Clerk</h3></div>
<p>Questions? Contact our Office Manager.</p>
<h4>Benefits</h4><h4>Equal Opportunity Employer</h4>
<footer>&copy; 2024 Acme Dental</footer>
</body></html>
"""


def test_job_titles_from_careers_page():
    s = sig(CAREERS_PAGE, "https://acmedental.com/careers", careers=True)
    assert s["hiring_titles"] == [
        "Front Desk Receptionist", "Dental Assistant - Part Time", "Scheduling Coordinator",
        "Marketing & Social Media Manager", "Office Manager", "Customer Service Representative (Remote)",
        "IT Support Specialist", "Sales Associate", "Data Entry Clerk",
    ]
    assert s["hiring_roles"] == ["customer_service", "data_entry", "front_desk", "marketing",
                                 "office_admin", "sales", "scheduling", "social_media", "web_it"]


def test_job_titles_only_read_on_the_careers_page():
    s = sig(CAREERS_PAGE, "https://acmedental.com/about")
    assert s["hiring_titles"] == [] and s["hiring_roles"] == []


def test_job_titles_are_capped_and_short():
    html = "".join(f"<h3>Dental Assistant {i}</h3>" for i in range(30))
    html += "<h3>Senior Office Manager for the North Austin Location Downtown</h3>"
    s = sig(html, "https://acmedental.com/careers", careers=True)
    assert len(s["hiring_titles"]) == 10
    assert all(len(t) <= 80 for t in s["hiring_titles"])
    assert s["hiring_roles"] == []  # dental assistants are not one of the tracked roles (and the long line is a sentence)


@pytest.mark.parametrize("text", [
    "We are hiring!", "Marketing", "Join our team", "Apply now", "Benefits", "Our Dentists",
    "Meet our Office Manager", "Questions? Call our office manager.", "it was a great place to work",
    "We use SEO to grow.", "Sales",
])
def test_not_job_titles(text):
    s = sig(f"<h3>{text}</h3>", "https://acmedental.com/careers", careers=True)
    assert s["hiring_titles"] == [] and s["hiring_roles"] == []


# ---------------------------------------------------------------------------
# Hostile pages stay fast
# ---------------------------------------------------------------------------

def test_hostile_pages_are_fast():
    hostile = [
        "<script>" + "gtag('config', " * 100_000 + "</script>",
        "<script>" + "AW-" * 300_000 + "</script>",
        "<script>" + "fbq(" * 300_000 + "</script>",
        '<a href="/book">Book now</a>' * 20_000,
        '<a href="https://calendly.com/">x</a>' * 20_000,
        '<script src="https://x.com/a.js"></script>' * 20_000,
        '<iframe src="https://x.com/"></iframe>' * 20_000,
        '<a href="/a.pdf">form</a>' * 20_000,
        "<h3>Dental Assistant</h3>" * 50_000,
        '<script type="application/ld+json">' + '{"@type":"WebSite","a":' * 20_000 + "1" + "}" * 20_000 + "</script>",
        '<div itemtype="https://schema.org/Thing">' * 20_000,
        "<button class='" + "add" * 100_000 + "'>",
    ]
    for html in hostile:
        html = html[:2_000_000]
        started = time.perf_counter()
        detect_intent_signals(html, "https://acme.com/", careers=True)
        assert time.perf_counter() - started < 5, html[:40]


# ---------------------------------------------------------------------------
# Crawler: merge rules and the careers page
# ---------------------------------------------------------------------------

def test_merge_intent_signals_across_pages():
    pages = [
        {"has_analytics": False, "has_viewport": True, "ad_pixels": ["meta"], "booking_provider": None,
         "has_online_booking": False, "has_live_chat": False, "hiring_roles": [], "hiring_titles": []},
        {"has_analytics": True, "has_viewport": False, "ad_pixels": ["google_ads", "meta"],
         "has_online_booking": True, "booking_provider": "Calendly", "hiring_roles": [], "hiring_titles": []},
        {"has_online_booking": True, "booking_provider": "own form", "has_pdf_forms": True,
         "hiring_roles": ["front_desk", "marketing"], "hiring_titles": ["Receptionist", "Marketing Coordinator"]},
    ]
    signals = _empty_signals()
    _merge_intent_signals(signals, pages)
    assert signals["has_analytics"] is True and signals["has_pdf_forms"] is True
    assert signals["has_live_chat"] is False
    assert signals["ad_pixels"] == ["google_ads", "meta"]
    assert signals["has_online_booking"] is True and signals["booking_provider"] == "Calendly"
    assert signals["has_viewport"] is True  # homepage only
    assert signals["hiring_roles"] == ["front_desk", "marketing"]
    assert signals["hiring_titles"] == ["Marketing Coordinator", "Receptionist"]

    signals = _empty_signals()
    _merge_intent_signals(signals, [{}, {"has_viewport": True}])
    assert signals["has_viewport"] is None  # the homepage didn't say
    signals = _empty_signals()
    _merge_intent_signals(signals, [{}])
    # No page could tell: every absence signal is "unknown", never "they have none".
    assert all(signals[k] is None for k in crawler._INTENT_BOOL_KEYS) and signals["ad_pixels"] is None
    assert signals["hiring_roles"] == [] and signals["hiring_titles"] == []


@pytest.fixture
def fake_dns(monkeypatch):
    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(crawler, "_resolve_host", resolve)


def run_crawl(pages: dict[str, str], requested: list[str], url: str = "acmedental.com", **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await crawl_website(url, client=client, **kwargs)

    return asyncio.run(run())


HOME = ('<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body>'
        + GA4 + '<nav><a href="/contact">Contact</a><a href="/careers/">Careers</a></nav></body></html>')
CONTACT = ('<html><body><a href="https://calendly.com/acme/visit">Book a visit</a>'
           '<a href="/forms/new-patient.pdf">New patient forms</a>' + META_PIXEL + "</body></html>")
CAREERS = "<html><body><h1>Careers</h1><h3>Front Desk Receptionist</h3><h3>Social Media Coordinator</h3></body></html>"


def test_crawl_merges_intent_signals_and_reads_the_careers_page(fake_dns):
    requested: list[str] = []
    data = run_crawl({"https://acmedental.com/": HOME, "https://acmedental.com/contact": CONTACT,
                      "https://acmedental.com/careers/": CAREERS}, requested)
    s = data.signals
    assert s["has_analytics"] is True and s["has_viewport"] is True
    assert s["ad_pixels"] == ["meta"]
    assert s["has_online_booking"] is True and s["booking_provider"] == "Calendly"
    assert s["has_pdf_forms"] is True
    assert s["hiring_titles"] == ["Front Desk Receptionist", "Social Media Coordinator"]
    assert s["hiring_roles"] == ["front_desk", "social_media"]
    assert s["pages_crawled"] == 3
    assert requested.count("https://acmedental.com/careers/") == 1


def test_careers_page_is_skipped_when_the_page_budget_is_used(fake_dns):
    requested: list[str] = []
    data = run_crawl({"https://acmedental.com/": HOME, "https://acmedental.com/contact": CONTACT,
                      "https://acmedental.com/careers/": CAREERS}, requested, max_pages=2)
    assert "https://acmedental.com/careers/" not in requested
    assert data.signals["hiring_titles"] == [] and data.signals["pages_crawled"] == 2


def test_careers_page_respects_robots_txt(fake_dns):
    requested: list[str] = []
    data = run_crawl({"https://acmedental.com/": HOME, "https://acmedental.com/contact": CONTACT,
                      "https://acmedental.com/careers/": CAREERS,
                      "https://acmedental.com/robots.txt": "User-agent: *\nDisallow: /careers/"}, requested)
    assert "https://acmedental.com/careers/" not in requested
    assert data.signals["hiring_titles"] == []
    assert data.signals["has_careers"] is True  # the link itself still counts


def test_careers_page_on_a_job_site_is_not_fetched(fake_dns):
    requested: list[str] = []
    home = HOME.replace('href="/careers/"', 'href="https://boards.greenhouse.io/acmedental"')
    data = run_crawl({"https://acmedental.com/": home}, requested)
    assert data.signals["careers_url"] == "https://boards.greenhouse.io/acmedental"
    assert not any("greenhouse" in u for u in requested)
    assert data.signals["hiring_titles"] == []


def test_viewport_comes_from_the_homepage_only(fake_dns):
    requested: list[str] = []
    home = "<html><body><a href='/contact'>Contact</a></body></html>"
    contact = '<html><head><meta name="viewport" content="width=device-width"></head><body>Hi</body></html>'
    data = run_crawl({"https://acmedental.com/": home, "https://acmedental.com/contact": contact}, requested)
    assert data.signals["has_viewport"] is False


def test_failed_crawl_has_unknown_viewport(fake_dns):
    data = run_crawl({}, [])
    assert data.ok is False
    assert data.signals["has_viewport"] is None and data.signals["has_analytics"] is False


# ---------------------------------------------------------------------------
# Speed plugins and consent tools that delay scripts (WP Rocket, LiteSpeed...)
# ---------------------------------------------------------------------------

GTAG_CONFIG = "window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('config','G-1A2B3C4D5E');"


@pytest.mark.parametrize("html", [
    # WP Rocket "delay JavaScript"
    '<script type="rocketlazyloadscript" data-rocket-src="https://www.googletagmanager.com/gtag/js?id=G-1A2B3C4D5E" async></script>'
    f'<script type="rocketlazyloadscript">{GTAG_CONFIG}</script>',
    # LiteSpeed Cache
    '<script type="litespeed/javascript" data-src="https://www.googletagmanager.com/gtag/js?id=G-1A2B3C4D5E"></script>',
    f'<script type="litespeed/javascript">{GTAG_CONFIG}</script>',
    # Cloudflare Rocket Loader: GTM snippet
    "<script type=\"4d1c5a2e8f9b-text/javascript\">(function(w,d,s,l,i){j.src='https://www.googletagmanager.com/gtm.js?id='+i;"
    "})(window,document,'script','dataLayer','GTM-ABC1234');</script>",
    # Complianz: external script waiting for consent
    '<script type="text/plain" data-category="statistics" '
    'data-cmplz-src="https://www.googletagmanager.com/gtag/js?id=G-1A2B3C4D5E"></script>',
    # Perfmatters
    f'<script type="pmdelayedscript" data-perfmatters-type="text/javascript">{GTAG_CONFIG}</script>',
    '<script type="pmdelayedscript" data-src="https://www.google-analytics.com/analytics.js"></script>',
])
def test_analytics_found_behind_speed_plugins(html):
    assert sig(html)["has_analytics"] is True


def test_live_chat_and_pixels_found_behind_speed_plugins():
    tawk = ("<script type=\"litespeed/javascript\">var Tawk_API=Tawk_API||{};(function(){var s1=document.createElement('script');"
            "s1.src='https://embed.tawk.to/abc/default';})();</script>")
    assert sig(tawk)["has_live_chat"] is True
    pixel = META_PIXEL.replace("<script>", '<script type="rocketlazyloadscript">')
    assert sig(pixel)["ad_pixels"] == ["meta"]


def test_delayed_scripts_with_nothing_found_say_unknown():
    # A WP Rocket page whose delayed scripts we read but that shows no analytics / chat /
    # pixel: the plugin may still hide them, so "unknown", not "they have none".
    s = sig('<script type="rocketlazyloadscript" data-rocket-src="/wp-content/themes/acme/main.js"></script>'
            '<a href="/contact">Contact</a>')
    assert s["has_analytics"] is None and s["has_live_chat"] is None and s["ad_pixels"] is None
    # Signals read from links and markup are not affected
    assert s["has_online_booking"] is False and s["has_local_schema"] is False


def test_data_blocks_still_not_read_as_code():
    s = sig('<script type="text/x-handlebars-template">gtag("config", "G-ABCDEF12")</script>'
            '<script type="application/json">{"x": "embed.tawk.to"}</script>')
    assert s["has_analytics"] is False and s["has_live_chat"] is False


# ---------------------------------------------------------------------------
# Pages built by JavaScript and Google Tag Manager: absence is "unknown"
# ---------------------------------------------------------------------------

SPA_SHELL = """<html><head><title>Acme Dental</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(w,d,s,l,i){j.src='https://www.googletagmanager.com/gtm.js?id='+i;})(window,document,'script','dataLayer','GTM-ABC123');</script>
<script type="module" src="/assets/index-4f3a.js"></script></head>
<body><noscript>You need to enable JavaScript to run this app.</noscript><div id="root"></div></body></html>"""


def test_javascript_built_pages_say_unknown():
    s = detect_intent_signals(SPA_SHELL, URL)
    for key in ("has_online_booking", "has_online_payment", "has_shop", "has_online_ordering",
                "has_live_chat", "has_local_schema", "ad_pixels"):
        assert s[key] is None, key
    assert s["has_analytics"] is True and s["has_viewport"] is True  # found things stay found


def test_empty_app_mount_with_some_text_is_still_unknown():
    html = ('<header><p>' + "Acme Dental, family dentistry in Austin since 1998. " * 6 + '</p></header>'
            '<div id="app"></div>')
    s = detect_intent_signals(html, URL)
    assert s["has_online_booking"] is None and s["has_live_chat"] is None


def test_server_rendered_app_is_judged_normally():
    html = '<div id="__next"><main>' + FILLER + '<a href="/contact">Contact</a></main></div>'
    s = detect_intent_signals(html, URL)
    assert s["has_online_booking"] is False and s["has_live_chat"] is False and s["ad_pixels"] == []


def test_thin_page_keeps_what_it_found():
    s = detect_intent_signals('<a href="https://calendly.com/acme/visit">Book</a>', URL)
    assert s["has_online_booking"] is True and s["booking_provider"] == "Calendly"
    assert s["has_live_chat"] is None


def test_tag_manager_hides_chat_and_pixels_only():
    s = sig(GTM)
    assert s["has_analytics"] is True
    assert s["has_live_chat"] is None and s["ad_pixels"] is None
    assert s["has_online_booking"] is False and s["has_local_schema"] is False
    s = sig(GTM + META_PIXEL)
    assert s["ad_pixels"] == ["meta"]


def test_merge_full_sub_page_decides_over_javascript_homepage():
    home = detect_intent_signals(SPA_SHELL, URL)
    contact = sig('<a href="/contact">Contact</a>', "https://acmedental.com/contact")
    signals = _empty_signals()
    _merge_intent_signals(signals, [home, contact])
    assert signals["has_online_booking"] is False and signals["has_local_schema"] is False
    assert signals["has_live_chat"] is False  # the full contact page has no chat (and no Tag Manager)
    signals = _empty_signals()
    _merge_intent_signals(signals, [home])
    assert signals["has_online_booking"] is None and signals["ad_pixels"] is None
    assert signals["has_analytics"] is True


# ---------------------------------------------------------------------------
# Online payment: ordering platforms, India / Pakistan providers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    '<a href="https://order.toasttab.com/online/marios">Order Online</a>',
    '<a href="https://direct.chownow.com/order/123/locations/456">Order now</a>',
    '<a href="https://www.doordash.com/store/marios-pizza-123/">Order delivery</a>',
])
def test_online_ordering_counts_as_online_payment(html):
    s = sig(html, "https://acmepizza.com/")
    assert s["has_online_ordering"] is True and s["has_online_payment"] is True


@pytest.mark.parametrize("html", [
    '<script src="https://checkout.razorpay.com/v1/checkout.js"></script>',
    '<a href="https://rzp.io/l/acmedental">Pay</a>',
    '<a href="https://pages.razorpay.com/pl_ABC123/view">Pay fees</a>',
    '<a href="https://razorpay.me/@acmedental">Pay now</a>',
    '<a href="https://www.instamojo.com/@acmedental/">Pay</a>',
    '<script src="https://js.instamojo.com/v1/checkout.js"></script>',
    '<a href="https://paytm.me/AbC-123">Pay with Paytm</a>',
    '<a href="https://payments.cashfree.com/forms/acme">Pay</a>',
    '<a href="https://u.payu.in/AbCdEf">Pay</a>',
    '<a href="https://payments.jazzcash.com.pk/CustomerPortal/transactionmanagement/merchantform/">Pay</a>',
    '<a href="https://easypay.easypaisa.com.pk/easypay/Index.jsf">Pay</a>',
    '<a href="https://www.instamojo.com/@acme">Pay online</a>',
])
def test_payment_india_pakistan(html):
    assert sig(html)["has_online_payment"] is True


@pytest.mark.parametrize("html", [
    '<a href="https://razorpay.com/">Payments by Razorpay</a>',
    '<a href="https://www.instamojo.com/pricing/">Instamojo</a>',
    '<a href="https://www.jazzcash.com.pk/">JazzCash</a>',
    '<a href="https://wa.me/923001234567?text=I%20want%20to%20order">Order on WhatsApp</a>',
])
def test_payment_india_pakistan_not_found(html):
    assert sig(html)["has_online_payment"] is False


# ---------------------------------------------------------------------------
# Booking: vertical platforms (vets, dental, HVAC, auto, gyms, legal) and WhatsApp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html, provider", [
    ('<a href="https://booking.vetstoria.com/acme-vet/abc">Book</a>', "Vetstoria"),
    ('<script src="https://booking.vetstoria.com/js/widget.js"></script>', "Vetstoria"),
    ('<a href="https://request.petdesk.com/?id=123">Request appointment</a>', "PetDesk"),
    ('<a href="https://book.getweave.com/acme-dental">Schedule</a>', "Weave"),
    ('<script src="https://scheduling.scheduleengine.net/widget.js"></script>', "Schedule Engine"),
    ('<a href="https://shop.tekmetric.com/appointments/123">Book service</a>', "Tekmetric"),
    ('<a href="https://app.shopmonkey.io/book/acme-auto">Book</a>', "Shopmonkey"),
    ('<a href="https://acmegym.sites.zenplanner.com/calendar.cfm">Schedule</a>', "Zen Planner"),
    ('<a href="https://acme.pushpress.com/landing/plans">Join</a>', "PushPress"),
    ('<a href="https://app.doctible.com/booking/acme">Book</a>', "Doctible"),
    ('<a href="https://flex.dental/book/acme">Book</a>', "Flex Dental"),
    ('<a href="https://acme.solutionreach.com/schedule">Book</a>', "Solutionreach"),
    ('<a href="https://app.lawmatics.com/forms/share/abc">Free consultation</a>', "Lawmatics"),
    ('<a href="https://wa.me/923001234567">Book Appointment</a>', "WhatsApp"),
    ('<a href="https://api.whatsapp.com/send?phone=919812345678&text=I%20want%20to%20book%20an%20appointment">'
     'WhatsApp us</a>', "WhatsApp"),
])
def test_booking_vertical_platforms_and_whatsapp(html, provider):
    s = sig(html, "https://acmeclinic.com/")
    assert s["has_online_booking"] is True and s["booking_provider"] == provider


@pytest.mark.parametrize("html", [
    '<a href="https://www.vetstoria.com/">Powered by Vetstoria</a>',
    '<a href="https://help.petdesk.com/">PetDesk help</a>',
    '<a href="https://www.getweave.com/">Weave</a>',
    '<a href="https://www.tekmetric.com/pricing">Tekmetric</a>',
    '<a href="https://wa.me/923001234567">Chat on WhatsApp</a>',
    '<a href="https://api.whatsapp.com/send?phone=919812345678&text=Hi">Message us</a>',
])
def test_booking_vertical_platforms_not_found(html):
    assert sig(html, "https://acmeclinic.com/")["has_online_booking"] is False


# ---------------------------------------------------------------------------
# Separate mobile site
# ---------------------------------------------------------------------------

def test_separate_mobile_site_is_not_called_not_mobile_friendly():
    alternate = '<link rel="alternate" media="only screen and (max-width: 640px)" href="https://m.example.com/">'
    assert sig(alternate, "https://www.example.com/")["has_viewport"] is None
    assert sig("<p>Hi</p>", "https://m.example.com/")["has_viewport"] is None
    # an alternate language version is not a mobile site
    assert sig('<link rel="alternate" hreflang="es" href="https://example.com/es/">')["has_viewport"] is False


# ---------------------------------------------------------------------------
# Crawler: careers page that also looks like a "team" page
# ---------------------------------------------------------------------------

JOBS_PAGE = ("<html><body><h1>Careers</h1><h3>Front Desk Receptionist</h3><h3>Marketing Coordinator</h3>"
             + FILLER + "</body></html>")


@pytest.mark.parametrize("link", [
    '<a href="/join-our-team">Careers</a>',
    '<a href="/careers">Join our team</a>',
])
def test_careers_page_that_looks_like_a_team_page_is_read_for_jobs(fake_dns, link):
    path = "/join-our-team" if "join-our-team" in link else "/careers"
    requested: list[str] = []
    home = f"<html><body><nav><a href='/contact'>Contact</a>{link}</nav>{FILLER}</body></html>"
    data = run_crawl({"https://acmedental.com/": home, "https://acmedental.com/contact": CONTACT,
                      "https://acmedental.com" + path: JOBS_PAGE}, requested)
    assert data.signals["hiring_roles"] == ["front_desk", "marketing"]
    assert data.signals["hiring_titles"] == ["Front Desk Receptionist", "Marketing Coordinator"]
    assert requested.count("https://acmedental.com" + path) == 1


def test_careers_page_found_on_a_sub_page_and_already_crawled_is_read_again(fake_dns):
    requested: list[str] = []
    home = f"<html><body><a href='/about'>About</a>{FILLER}</body></html>"
    about = f"<html><body><a href='/join-the-team'>Careers</a>{FILLER}</body></html>"
    data = run_crawl({"https://acmedental.com/": home, "https://acmedental.com/about": about,
                      "https://acmedental.com/join-the-team": JOBS_PAGE}, requested)
    assert data.signals["careers_url"] == "https://acmedental.com/join-the-team"
    assert data.signals["hiring_roles"] == ["front_desk", "marketing"]
    assert requested.count("https://acmedental.com/join-the-team") == 1
    assert data.signals["pages_crawled"] == 3
