"""Twilio Lookup: is a phone number a mobile, a landline or an internet (VoIP) number?

Docs: https://www.twilio.com/docs/lookup/v2-api/line-type-intelligence

    line_type("+15125550123") -> LineType(type="mobile", carrier="Verizon Wireless", valid=True)

The phone library alone can't tell US and Canadian mobiles from landlines (they share area
codes: phone_type "landline_or_mobile"). This matters when you text or WhatsApp leads.
- Optional: only used when TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are set in backend/.env.
- Only asked about US / Canadian numbers the phone library couldn't place (about $0.008 each),
  once per number (the answer is kept on the company, Company.phone_lookup).
- Every request is counted first with usage.consume("twilio_lookup"); the cap raises
  usage.UsageLimitError. Other problems raise SourceError.
"""
import logging
from dataclasses import dataclass

import httpx

from .. import config, usage
from ..sources.base import SourceError

log = logging.getLogger("providers.phone_lookup")

API_URL = "https://lookups.twilio.com/v2/PhoneNumbers/{number}"
USAGE_PROVIDER = "twilio_lookup"
TIMEOUT = 15
# Twilio's line types -> ours (app/phones.py)
TYPES = {
    "mobile": "mobile",
    "landline": "landline",
    "fixedVoip": "voip",
    "nonFixedVoip": "voip",
    "tollFree": "toll_free",
}

# Tests set this to an httpx.MockTransport so no real network is used.
mock_transport: httpx.BaseTransport | None = None


@dataclass
class LineType:
    type: str | None  # mobile | landline | voip | toll_free | other | invalid
    carrier: str | None
    valid: bool


def available() -> bool:
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN)


def needs_lookup(phone_e164: str | None, phone_type: str | None) -> bool:
    """A US / Canadian number the phone library couldn't place."""
    return bool(phone_e164) and phone_e164.startswith("+1") and phone_type == "landline_or_mobile"


def line_type(phone_e164: str, *, http_transport: httpx.BaseTransport | None = None) -> LineType:
    usage.consume(USAGE_PROVIDER)
    try:
        with httpx.Client(transport=http_transport or mock_transport, timeout=TIMEOUT,
                          auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)) as client:
            response = client.get(API_URL.format(number=phone_e164), params={"Fields": "line_type_intelligence"})
    except httpx.HTTPError as e:
        raise SourceError(f"Couldn't reach Twilio ({type(e).__name__})") from None
    if response.status_code in (401, 403):
        raise SourceError("Twilio refused the key (check TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN)")
    if response.status_code == 404:
        return LineType("invalid", None, False)
    if response.status_code != 200:
        raise SourceError(f"Twilio error (HTTP {response.status_code})")
    try:
        data = response.json()
    except ValueError:
        raise SourceError("Twilio sent an unreadable answer") from None
    if data.get("valid") is False:
        return LineType("invalid", None, False)
    info = data.get("line_type_intelligence") or {}
    kind = info.get("type")
    return LineType(TYPES.get(kind, "other" if kind and kind != "unknown" else None), info.get("carrier_name"), True)
