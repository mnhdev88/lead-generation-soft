"""What a business's Google reviews tell us (Google returns up to 5 reviews per place).

analyze_reviews() returns
    {"pain_points": {"phone": ['"...nobody answers the phone..."'], ...},
     "likely_new": False, "newest_review": "2026-03-01T10:00:00+00:00",
     "oldest_review": "2025-01-05T09:30:00+00:00", "low_rating_share": 0.4,
     "pain_points_confirmed": ["phone"]}
stored in Company.review_signals and used by the intent score (app/intent.py).
"pain_points_confirmed" lists the pain points strong enough to score: found in at least
CONFIRM_MIN_REVIEWS reviews, or in one review rated SEVERE_RATING stars or less. One remark
in a 3-star review is shown as a quote but should not earn points on its own.

Pain points (things our services fix), precision first: a wrong complaint is worse than none.
    booking        hard to book, couldn't get an appointment, no online booking
    phone          nobody answers, never called back, voicemail full, hung up on me
    website        website down / broken / outdated, wrong hours or info online
    communication  no follow-up, never replied to my email
    wait_time      long wait, waited an hour
    - Only complaints count: a review rated 1-3 stars (or without a rating), or a 4-5 star
      review whose sentence clearly complains ("Great dentist, but nobody answers the phone").
    - We only look for complaint phrases. Praise like "easy to book" or "they always answer
      the phone" never matches anything.
    - A phrase is skipped when its part of the sentence is negated or hypothetical:
      "never had a problem booking", "no long wait", "if nobody answers, leave a message".
    - A sentence that compares with another business ("unlike my old dentist, ...") is
      skipped: the complaint may be about someone else.
    - Some phrases are about something the business can't fix with software and are skipped:
      "hard to get in" about the parking lot or the door, "bad communication" with the
      insurance company or the lab, a "long wait" for a table, for parking or for lab
      results, or a wait that was "worth it". In a 4-5 star review a plain "but" is not
      enough to make a wait a complaint ("Long wait but worth it!").
    Each pain point keeps a few short quotes (<= QUOTE_MAX characters) around the phrase.

Possibly a new business (likely_new): only when we have seen ALL its reviews (Google sends at
most 5, the "most relevant" ones, which favour recent reviews): the total review count is no
more than the dated reviews we got, and every one is younger than NEW_MAX_AGE_DAYS. A review
without a date means we can't tell, so it is not "new".
"""
import re
from datetime import datetime, timezone

# ----- tunable constants --------------------------------------------------------

LOW_RATING = 3  # reviews rated this or lower are complaints
QUOTE_MAX = 160  # characters per quote (including the "...")
QUOTES_PER_POINT = 3  # quotes kept per pain point
NEW_MAX_REVIEWS = 15  # upper bound; in practice we also need to have seen every review (<= 5)
NEW_MAX_AGE_DAYS = 365
SEVERE_RATING = 2  # one complaint in a review rated this or lower is enough to score
CONFIRM_MIN_REVIEWS = 2  # otherwise the pain point must show up in this many reviews
NEGATION_WORDS = 4  # how many words before a phrase are checked for "never", "no", ...

# Complaint phrases per pain point. They are matched on lower-case text with "'" quotes.
_NEG = (r"(?:can't|cant|cannot|can not|couldn't|couldnt|could not|unable to|wasn't able to|was not able to|"
        r"weren't able to|never able to|never could|(?:have|has|had)(?:n't| not) been able to)")
# "back" as in "call me back", not "my back pain".
_NOT_BODY = r"(?!\s*(?:pain|problems?|issues?|surgery|injur(?:y|ies)|muscles?)\b)\b"
_NOT = r"(?:never|don't|dont|doesn't|doesnt|didn't|didnt|did not|do not|does not|won't|wont|will not|wouldn't|wouldnt|would not)"
_PHONE_OBJ = r"(?:the |their |my |our |any |a )?(?:phones?|calls?|telephone)"
_PAIN_PATTERNS: dict[str, list[str]] = {
    "booking": [
        # "get in" only as "get in to see ..." / "get in for an appointment" / at the end
        # ("so hard to get in."), never "get into" or "get in and out of the parking lot".
        rf"(?:hard|difficult|impossible|tough|a nightmare|a pain) to (?:book|schedule|get an? appointment|make an? appointment|"
        rf"get in(?: to see\b| for an? (?:appointment|visit|cleaning|check-?up|consult\w*)\b| with (?:a |the )?(?:doctor|dentist|dr)\b|\s*[.!?]|\s*$))",
        rf"{_NEG} (?:book|schedule|get an? appointment|get in to see|make an? appointment|get a booking)",
        r"no (?:way to book|online booking|online scheduling|way to schedule) ?(?:online)?",
        rf"{_NOT} (?:have|offer) (?:any )?online (?:booking|scheduling|appointments)",
        r"wish (?:they|you|there) (?:had|offered|was|were) (?:an? )?online (?:booking|scheduling|appointments?)",
        r"(?:problems?|trouble|issues?|difficulty) (?:booking|scheduling|getting an appointment|making an appointment)",
        r"(?:booking|scheduling) (?:system|process) (?:is|was) (?:terrible|awful|horrible|a nightmare|confusing|broken|a mess)",
        r"(?:lost|messed up) my appointment",
        r"double[- ]booked",
    ],
    "phone": [
        rf"(?:nobody|no one|no-one|noone) (?:ever )?(?:answers|answered|answer|picks up|picked up|pick up)(?= {_PHONE_OBJ}|\s*[.,!;]|\s*$)",
        rf"{_NOT} (?:ever )?(?:answers?|answered|picks? up|picked up) {_PHONE_OBJ}",
        rf"{_NEG} (?:get through|reach (?:anyone|anybody|someone|them|a person|a human|the office))",
        r"(?:impossible|hard|difficult) to (?:get through|reach (?:anyone|anybody|someone|them|a person|a human|the office))",
        rf"{_NOT} (?:ever )?(?:called|call|calls) (?:me |us )?back{_NOT_BODY}",
        rf"(?:no one|nobody) (?:ever )?(?:called|calls|call) (?:me |us )?back{_NOT_BODY}",
        r"never returned (?:my|our|any) (?:calls?|phone calls?|voicemails?|messages?)",
        r"no (?:call|calls|phone call) back",
        r"never (?:received|got|get) a (?:call ?back|return call|phone call back)",
        r"voice ?mail (?:box )?(?:is |was )?(?:always |constantly |completely )?full",
        r"(?:hung|hangs?) up on (?:me|us)",
        r"(?:put|left|kept) (?:me |us )?on hold for (?:over |more than |almost |nearly )?(?:an hour|\d+ (?:minutes|mins|hours))",
    ],
    "website": [
        r"(?:the |their |your )?web ?site (?:is|was|has been) (?:always |constantly |completely )?(?:down|broken|outdated|out of date|terrible|awful|useless|not working|wrong|a mess|confusing)",
        r"(?:the |their |your )?(?:web ?site|online booking|online form) (?:doesn't|does not|didn't|did not|never) (?:work|load)",
        r"(?:hours|address|information|info|prices|phone number) (?:listed )?(?:on (?:the |their |your )?(?:web ?site|site|google)|online) (?:is|are|was|were) (?:wrong|incorrect|outdated|out of date|not accurate|inaccurate)",
        r"wrong (?:hours|address|info|information|phone number) (?:online|on (?:the |their |your )?(?:web ?site|site|google))",
        r"(?:web ?site|google) (?:says|said|shows|showed|listed|lists|has|had) the wrong",
    ],
    "communication": [
        r"no (?:follow[- ]?up|followup)(?! (?:needed|necessary|required|visits?|appointments?|was|is)\b)",
        r"never (?:followed|follow|follows) ?up",
        r"(?:didn't|did not|failed to|forgot to|don't|do not) (?:ever )?follow[- ]?up",
        r"never (?:responded|replied|got back|answered|wrote back) to (?:my|our|any) (?:emails?|e-mails?|messages?|texts?|inquir(?:y|ies)|requests?)",
        r"(?:never|didn't|did not|don't|do not|won't) (?:ever )?(?:respond(?:ed)?|repl(?:y|ied)) to (?:my|our|any|the|multiple|several) (?:emails?|e-mails?|messages?|texts?|inquir(?:y|ies)|requests?|voicemails?)",
        r"(?:never|didn't|did not|don't|do not|won't) (?:ever )?(?:get|got|gets) back to (?:me|us)",
        r"(?:no|never got a|never received a|never got any|never received any) (?:response|reply|answer) (?:to (?:my|our) (?:emails?|e-mails?|messages?|inquir(?:y|ies)))",
        r"(?:emails?|e-mails?|messages?) (?:went|go|goes|have gone) unanswered",
        r"ignored (?:my|our|all my|all of my) (?:emails?|e-mails?|messages?|texts?)",
        r"(?:poor|lack of|zero|terrible|horrible|bad|no) communication(?! (?:issues?|problems?)\b)",
        r"communication (?:is|was) (?:terrible|awful|poor|non-?existent|horrible|lacking|bad)",
    ],
    "wait_time": [
        r"(?:waited|waiting|wait|sat|sitting) (?:in the (?:waiting room|lobby) )?(?:for )?(?:over |more than |almost |nearly |about |around |at least )?(?:an|one|two|three|four|\d+(?:\.\d+)?) (?:and a half )?(?:hours?|hrs?)",
        r"(?:waited|waiting|wait|sat|sitting) (?:in the (?:waiting room|lobby) )?(?:for )?(?:over |more than |almost |nearly |about |around |at least )?(?P<mins>\d{2,3}) ?(?:minutes|mins)",
        r"(?:long|forever|ridiculous|excessive|horrible|terrible|awful|insane|crazy) wait(?:ing)?(?: times?)?",
        r"wait(?:ing)? times? (?:is|are|was|were) (?:so |very |really )?(?:long|ridiculous|terrible|horrible|awful|insane|crazy|excessive)",
        r"(?:an|one|two|\d+) hours? (?:wait|late|behind)",
        r"(?:always|constantly) running (?:late|behind)",
    ],
}
_PAIN_RES = {cat: [re.compile(p) for p in pats] for cat, pats in _PAIN_PATTERNS.items()}
MIN_WAIT_MINUTES = 30  # "waited 45 minutes" counts, "waited 10 minutes" doesn't

# A phrase is skipped when the same sentence shows it is about something else.
_NOT_ABOUT_BOOKING = re.compile(  # "the parking lot is hard to get in"
    r"\b(?:parking|lot|garage|building|doors?|driveway|elevator|stairs|ramp|wheelchair|stroller|car|gate)\b")
_THIRD_PARTY = re.compile(  # "bad communication between the insurance company and me"
    r"\b(?:insurance|insurer|lab|labs|laboratory|pharmacy|hospital|contractor|subcontractor|landlord|"
    r"manufacturer|supplier|lender|bank|dealer(?:ship)?|warranty company|carrier)\b")
# Right after a wait phrase: what was waited for. "long wait for a table / for parking /
# for my x-ray results" is not a scheduling problem.
_WAIT_FOR_OTHER = re.compile(
    r"^\W*(?:[\w'-]+\W+){0,4}?for (?:[\w'-]+ ){0,3}?(?:tables?|seats?|seating|parking|spots?|food|meals?|"
    r"orders?|drinks?|pizza|coffee|results?|lab|x-?rays?|tests?|prescriptions?|delivery)\b")
_WAIT_WORTH_IT = re.compile(r"\bworth (?:it|the wait)\b")
_NOT_ABOUT = {
    "booking": lambda low, m: bool(_NOT_ABOUT_BOOKING.search(low)),
    "communication": lambda low, m: bool(_THIRD_PARTY.search(low)),
    "wait_time": lambda low, m: bool(_WAIT_WORTH_IT.search(low)) or bool(_WAIT_FOR_OTHER.search(low[m.end():]))
    or bool(_WAIT_FOR_OTHER.search(low[m.start():])),
}

# A word in the few words before the phrase (same part of the sentence) that cancels it.
_NEGATORS = {
    "never", "no", "not", "without", "zero", "none", "hardly", "barely", "rarely", "if",
    "unless", "didn't", "didnt", "don't", "dont", "doesn't", "doesnt", "wasn't", "wasnt",
    "isn't", "isnt", "haven't", "havent", "hasn't", "hasnt", "won't", "wont",
}
# Parts of a sentence: split at , ; : ( ) and at "but" / "although" / "though" / "however".
_CLAUSE_SPLIT = re.compile(r"[,;:()\-–—]|\b(?:but|although|though|however|whereas|and|so|because)\b")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
# 4-5 star reviews: the complaint must be clearly marked as one ...
_COMPLAINT_MARKERS = re.compile(
    r"\b(?:but|however|unfortunately|sadly|only (?:complaint|issue|problem|downside|negative|thing)|"
    r"wish|frustrat\w*|annoying|disappoint\w*|downside|drawback|con:|cons:)\b"
)
# ... for a wait, a plain "but" is not enough: "Great food. Long wait but worth it!"
_STRONG_COMPLAINT_MARKERS = re.compile(
    r"\b(?:unfortunately|sadly|only (?:complaint|issue|problem|downside|negative|thing)|"
    r"frustrat\w*|annoying|disappoint\w*|downside|drawback|con:|cons:)\b"
)
_NEEDS_STRONG_MARKER = {"wait_time"}
# ... and not be about another business.
_OTHER_BUSINESS = re.compile(
    r"\b(?:unlike|compared to|previous|prior|old (?:dentist|doctor|office|clinic|place|vet|mechanic|lawyer)|"
    r"last (?:dentist|doctor|office|clinic|place|vet|mechanic|lawyer)|other (?:dentists|doctors|offices|clinics|places))\b"
)


# ----- main entry point ---------------------------------------------------------

def analyze_reviews(reviews: list[dict], reviews_count: int | None, now: datetime | None = None) -> dict:
    """Pain points, "possibly new business" and review dates from Google reviews.
    Never raises on odd input: entries that aren't dicts or have odd fields are skipped."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    reviews = [r for r in (reviews or []) if isinstance(r, dict)] if isinstance(reviews, list) else []

    pain: dict[str, list[str]] = {}
    pain_reviews: dict[str, int] = {}  # how many reviews mention each pain point
    severe: set[str] = set()  # pain points found in a review rated SEVERE_RATING or less
    dates: list[datetime] = []
    undated = False
    ratings: list[int] = []
    for rev in reviews:
        rating = _rating(rev.get("rating"))
        if rating is not None:
            ratings.append(rating)
        when = _parse_time(rev.get("publish_time"))
        if when:
            dates.append(when)
        else:
            undated = True
        text = rev.get("text")
        if isinstance(text, str) and text.strip():
            found = _pain_points(text, rating)
            for cat, quote in found:
                quotes = pain.setdefault(cat, [])
                if quote not in quotes and len(quotes) < QUOTES_PER_POINT:
                    quotes.append(quote)
            for cat in {cat for cat, _ in found}:
                pain_reviews[cat] = pain_reviews.get(cat, 0) + 1
                if rating is not None and rating <= SEVERE_RATING:
                    severe.add(cat)

    likely_new = (
        isinstance(reviews_count, int) and not isinstance(reviews_count, bool)
        and reviews_count <= NEW_MAX_REVIEWS
        and reviews_count <= len(dates)  # we have seen every review the business has
        and bool(dates) and not undated
        and all((now - d).days < NEW_MAX_AGE_DAYS for d in dates)
    )
    confirmed = sorted(cat for cat, n in pain_reviews.items() if n >= CONFIRM_MIN_REVIEWS or cat in severe)
    return {
        "pain_points": pain,
        "likely_new": likely_new,
        "newest_review": max(dates).isoformat() if dates else None,
        "oldest_review": min(dates).isoformat() if dates else None,
        "low_rating_share": round(sum(1 for r in ratings if r <= LOW_RATING) / len(ratings), 2) if ratings else None,
        "pain_points_confirmed": confirmed,
    }


# ----- helpers ------------------------------------------------------------------

def _rating(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if 1 <= value <= 5 else None


def _parse_time(value) -> datetime | None:
    """Google's publishTime, e.g. "2026-01-05T12:34:56.123456789Z" -> datetime (UTC)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # Python reads at most 6 decimals
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _pain_points(text: str, rating: int | None) -> list[tuple[str, str]]:
    """(category, quote) for every complaint phrase found in one review."""
    found = []
    happy = rating is not None and rating > LOW_RATING
    for sentence in _SENTENCE_SPLIT.split(text):
        low = _normalize(sentence)
        if not low:
            continue
        if _OTHER_BUSINESS.search(low):
            continue  # about another business ("unlike my old dentist, ...")
        if happy and not _COMPLAINT_MARKERS.search(low):
            continue  # a happy review: only a clearly marked complaint counts
        strong = bool(_STRONG_COMPLAINT_MARKERS.search(low))
        for cat, patterns in _PAIN_RES.items():
            if happy and cat in _NEEDS_STRONG_MARKER and not strong:
                continue
            not_about = _NOT_ABOUT.get(cat)
            for rx in patterns:
                m = next((m for m in rx.finditer(low)
                          if _counts(low, m) and not (not_about and not_about(low, m))), None)
                if m:
                    found.append((cat, _quote(sentence, low, m.start(), m.end())))
                    break  # one quote per category per sentence
    return found


def _normalize(text: str) -> str:
    """Lower case, curly quotes -> ', single spaces. Same length rules as the quote uses."""
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def _counts(low: str, m: re.Match) -> bool:
    """False when the phrase is negated / hypothetical, or a short wait in minutes."""
    mins = m.groupdict().get("mins")
    if mins and int(mins) < MIN_WAIT_MINUTES:
        return False
    before = low[: m.start()]
    clause = _CLAUSE_SPLIT.split(before)[-1] if before else ""
    words = re.findall(r"[a-z']+", clause)[-NEGATION_WORDS:]
    return not any(w in _NEGATORS for w in words)


def _quote(sentence: str, low: str, start: int, end: int) -> str:
    """A short excerpt of the (normalized-space) sentence around the matched phrase."""
    clean = re.sub(r"\s+", " ", sentence).strip()  # same positions as `low`
    if len(clean) != len(low):
        clean = low  # a few letters change length in lower case: quote the lower-case text
    if len(clean) <= QUOTE_MAX:
        return clean
    room = QUOTE_MAX - 6  # space for "..." on both sides
    extra = max(0, room - (end - start))
    left = max(0, start - extra // 2)
    right = min(len(clean), left + room)
    left = max(0, right - room)
    # Don't cut words in half.
    if left > 0:
        space = clean.find(" ", left, start + 1)
        left = space + 1 if space != -1 else left
    if right < len(clean):
        space = clean.rfind(" ", end, right)
        right = space if space != -1 else right
    piece = clean[left:right].strip()
    return ("..." if left > 0 else "") + piece + ("..." if right < len(clean) else "")
