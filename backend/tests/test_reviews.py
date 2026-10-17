"""Tests for app/reviews.py: pain points from Google reviews (precision first)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.reviews import QUOTE_MAX, analyze_reviews

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def rev(text, rating=2, days_ago=30, **extra):
    when = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    return {"rating": rating, "text": text, "publish_time": when, "relative": "a month ago", **extra}


def pains(*reviews):
    return analyze_reviews(list(reviews), 100, now=NOW)["pain_points"]


# ----- complaints that count ---------------------------------------------------

@pytest.mark.parametrize("category, text", [
    ("booking", "It is impossible to get an appointment here."),
    ("booking", "I couldn't book anything for weeks."),
    ("booking", "They don't offer online booking in 2026?!"),
    ("booking", "Had real trouble scheduling a cleaning."),
    ("phone", "I called five times and nobody answers."),
    ("phone", "They never pick up the phone."),
    ("phone", "Nobody ever called me back."),
    ("phone", "Their voicemail is always full."),
    ("phone", "The receptionist hung up on me."),
    ("phone", "I can't get through to anyone at this office."),
    ("website", "Their website is down half the time."),
    ("website", "The hours on their website are wrong, I drove there for nothing."),
    ("communication", "They never replied to my emails."),
    ("communication", "Terrible communication from start to finish."),
    ("communication", "They didn't follow up after the quote."),
    ("wait_time", "We waited over an hour past our appointment time."),
    ("wait_time", "I waited 45 minutes in the lobby."),
    ("wait_time", "Ridiculous wait times every visit."),
])
def test_complaints_found(category, text):
    found = pains(rev(text, rating=1))
    assert list(found) == [category], found
    assert found[category] == [text]


def test_happy_review_with_clear_complaint_counts():
    found = pains(rev("Dr. Lee is great, but nobody answers the phone.", rating=5))
    assert list(found) == ["phone"]
    found = pains(rev("Lovely salon. I wish they had online booking.", rating=4))
    assert list(found) == ["booking"]


def test_review_without_rating_still_checked():
    assert list(pains(rev("Nobody answers the phone.", rating=None))) == ["phone"]


# ----- things that must NOT count ----------------------------------------------

@pytest.mark.parametrize("text, rating", [
    ("Never had a problem booking an appointment.", 5),
    ("Never had a problem booking an appointment.", 3),
    ("So easy to book online!", 5),
    ("They always answer the phone right away.", 5),
    ("They always answer the phone right away.", 2),
    ("No long wait, in and out in 20 minutes.", 3),
    ("It wasn't hard to get an appointment at all.", 3),
    ("I had no trouble scheduling.", 2),
    ("Zero issues booking online.", 3),
    ("If nobody answers, just leave a message.", 3),
    ("I waited 10 minutes, no big deal.", 3),
    ("Unlike my old dentist, nobody here makes you wait an hour.", 2),
    ("Staff were rude.", 1),
    ("Nobody answered my questions about the bill.", 1),
    ("My back pain is gone, they never called it back surgery.", 2),
    ("The doctor said no follow up needed.", 3),
    ("No communication issues at all.", 3),
    ("Nobody answers the phone at my old office, but here they do.", 5),
])
def test_not_a_complaint(text, rating):
    assert pains(rev(text, rating=rating)) == {}


@pytest.mark.parametrize("text, rating", [
    # Regressions: phrases about something the business can't fix with software.
    ("Parking is hard to get in.", 3),
    ("The parking lot is hard to get in and out of", 3),
    ("It's hard to get into the building with a stroller.", 2),
    ("Bad communication between the insurance company and me", 1),
    ("Poor communication from the lab about my results.", 2),
    ("Long wait for a table is expected on Friday though.", 1),
    ("I had to wait 2 hours for my x-rays results to come back from the lab", 2),
    ("Only complaint is the long wait for parking", 5),
    ("Great food. Long wait but worth it!", 5),
    ("Long wait, but worth it.", 3),
])
def test_not_about_the_business(text, rating):
    assert pains(rev(text, rating=rating)) == {}


@pytest.mark.parametrize("category, text, rating", [
    # The same kinds of complaints about the business itself still count.
    ("booking", "It is so hard to get in.", 2),
    ("booking", "Very hard to get in to see the doctor.", 3),
    ("communication", "Bad communication from the front desk.", 1),
    ("wait_time", "We waited over an hour past our appointment time.", 2),
    ("wait_time", "Great doctor. Unfortunately the wait times are ridiculous.", 5),
])
def test_business_complaints_still_found(category, text, rating):
    assert list(pains(rev(text, rating=rating))) == [category]


def test_confirmed_pain_points():
    # One remark in a 3-star review: shown as a quote, but not strong enough to score.
    out = analyze_reviews([rev("Nobody answers the phone.", rating=3)], 50, now=NOW)
    assert out["pain_points"] == {"phone": ["Nobody answers the phone."]}
    assert out["pain_points_confirmed"] == []
    # One 1-2 star review is enough ...
    out = analyze_reviews([rev("Nobody answers the phone.", rating=2)], 50, now=NOW)
    assert out["pain_points_confirmed"] == ["phone"]
    # ... and so are two reviews of any rating.
    out = analyze_reviews([rev("Nobody answers the phone.", rating=3),
                           rev("Nice staff, but nobody ever called me back.", rating=5)], 50, now=NOW)
    assert out["pain_points_confirmed"] == ["phone"]
    # Two mentions in the SAME review are one review.
    out = analyze_reviews([rev("Nobody answers the phone. Nobody called me back.", rating=3)], 50, now=NOW)
    assert out["pain_points_confirmed"] == []


def test_happy_review_without_complaint_marker_ignored():
    # Five stars and no "but"/"unfortunately": the phrase is not trusted.
    assert pains(rev("Long wait is worth it for this doctor.", rating=5)) == {}


def test_quotes_short_and_around_the_phrase():
    filler = "The office was clean and the staff was friendly enough when we arrived there " * 3
    text = filler + "but honestly nobody answers the phone when you call " + filler
    [quote] = pains(rev(text, rating=2))["phone"]
    assert len(quote) <= QUOTE_MAX
    assert "nobody answers the phone" in quote
    assert quote.startswith("...") and quote.endswith("...")
    assert "  " not in quote


def test_quotes_deduplicated_and_capped():
    reviews = [rev(f"Visit {i}: nobody answers the phone.", rating=1) for i in range(5)]
    reviews.append(rev("Visit 0: nobody answers the phone.", rating=1))
    quotes = pains(*reviews)["phone"]
    assert len(quotes) == 3
    assert len(set(quotes)) == 3


def test_several_categories_in_one_review():
    text = "Waited an hour. Then nobody called me back! Their website is outdated too."
    assert set(pains(rev(text, rating=1))) == {"wait_time", "phone", "website"}


# ----- dates, new business, rating share ---------------------------------------

def test_dates_and_low_rating_share():
    out = analyze_reviews([rev("ok", 5, days_ago=10), rev("meh", 2, days_ago=400), rev("x", None, days_ago=50)],
                          40, now=NOW)
    assert out["newest_review"].startswith("2026-10-07T12:00:00.123456")
    assert out["oldest_review"].startswith("2025-09-12")
    assert out["low_rating_share"] == 0.5
    assert out["likely_new"] is False


def test_likely_new():
    young = [rev("Great!", 5, days_ago=d) for d in (5, 60, 200)]
    assert analyze_reviews(young, 3, now=NOW)["likely_new"] is True  # we saw all 3 reviews
    assert analyze_reviews(young, 2, now=NOW)["likely_new"] is True  # count lags behind a little
    assert analyze_reviews(young + [rev("Old", 5, days_ago=400)], 4, now=NOW)["likely_new"] is False
    assert analyze_reviews([], 3, now=NOW)["likely_new"] is False  # no review at all
    assert analyze_reviews(young, None, now=NOW)["likely_new"] is False
    undated = young + [{"rating": 5, "text": "hi", "publish_time": None}]
    assert analyze_reviews(undated, 3, now=NOW)["likely_new"] is False


def test_likely_new_needs_every_review_seen():
    # Regression: Google sends at most 5 "most relevant" (often recent) reviews. A business
    # with 14 reviews whose 5 returned ones are recent may have 9 old ones we never saw.
    five_recent = [rev("Great!", 5, days_ago=d) for d in (10, 40, 70, 100, 130)]
    assert analyze_reviews(five_recent, 14, now=NOW)["likely_new"] is False
    assert analyze_reviews(five_recent, 6, now=NOW)["likely_new"] is False
    assert analyze_reviews(five_recent, 5, now=NOW)["likely_new"] is True


def test_odd_input_never_raises():
    odd = [None, "text", 5, {"rating": "five", "text": None, "publish_time": "yesterday"},
           {"rating": True, "text": 123}, {}, {"text": "nobody answers the phone", "publish_time": 20260101}]
    out = analyze_reviews(odd, "12", now=NOW)
    assert out["pain_points"] == {"phone": ["nobody answers the phone"]}
    assert out["likely_new"] is False
    assert out["newest_review"] is None and out["low_rating_share"] is None
    empty = analyze_reviews(None, None)
    assert empty == {"pain_points": {}, "likely_new": False, "newest_review": None,
                     "oldest_review": None, "low_rating_share": None, "pain_points_confirmed": []}


def test_naive_now_and_offsets():
    out = analyze_reviews([rev("ok", 5, days_ago=1, publish_time="2026-10-16T08:00:00+05:00")], 1,
                          now=datetime(2026, 10, 17))
    assert out["likely_new"] is True
    assert out["newest_review"] == "2026-10-16T08:00:00+05:00"
