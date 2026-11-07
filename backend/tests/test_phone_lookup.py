"""Mobile or landline? Twilio Lookup (app/providers/phone_lookup.py) during the email check."""
import httpx
import pytest

from app import config
from app.models import Company
from app.phones import apply_company_phone
from app.providers import phone_lookup
from app.sources import SourceError
from tests.test_worker_emails import company, engine, run_emails  # noqa: F401  (fixture)


class FakeTwilio:
    def __init__(self, body=None, status=200):
        self.body = body or {"valid": True, "line_type_intelligence": {"type": "mobile", "carrier_name": "Verizon"}}
        self.status, self.requests = status, []

    def transport(self):
        def handler(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.body)
        return httpx.MockTransport(handler)


@pytest.fixture
def twilio(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret")
    fake = FakeTwilio()
    monkeypatch.setattr(phone_lookup, "mock_transport", fake.transport())
    return fake


def test_only_unplaced_north_american_numbers_are_asked():
    assert phone_lookup.needs_lookup("+15125550123", "landline_or_mobile")
    assert not phone_lookup.needs_lookup("+15125550123", "mobile")
    assert not phone_lookup.needs_lookup("+923001234567", "landline_or_mobile")


@pytest.mark.parametrize("body, expected", [
    ({"valid": True, "line_type_intelligence": {"type": "landline", "carrier_name": "AT&T"}}, ("landline", "AT&T")),
    ({"valid": True, "line_type_intelligence": {"type": "nonFixedVoip"}}, ("voip", None)),
    ({"valid": True, "line_type_intelligence": {"type": "unknown"}}, (None, None)),
    ({"valid": False}, ("invalid", None)),
])
def test_line_types(twilio, monkeypatch, db, body, expected):
    monkeypatch.setattr(phone_lookup, "mock_transport", FakeTwilio(body).transport())
    answer = phone_lookup.line_type("+15125550123")
    assert (answer.type, answer.carrier) == expected


def test_bad_key_is_explained(twilio, monkeypatch, db):
    monkeypatch.setattr(phone_lookup, "mock_transport", FakeTwilio(status=401).transport())
    with pytest.raises(SourceError, match="TWILIO"):
        phone_lookup.line_type("+15125550123")


def test_email_check_asks_once_and_keeps_the_answer(db, engine, twilio):
    c = company(db, "Smile", phone="(512) 555-0123", country="US")
    apply_company_phone(c)
    db.commit()
    assert c.phone_type == "landline_or_mobile"
    job = run_emails(db, company_ids=[c.id])
    c = db.get(Company, c.id)
    assert c.phone_type == "mobile" and c.phone_lookup["carrier"] == "Verizon"
    assert "1 phone numbers checked" in job.progress_message
    assert twilio.requests[0].url.params["Fields"] == "line_type_intelligence"
    run_emails(db, company_ids=[c.id])
    assert len(twilio.requests) == 1  # never twice for one number
    # Tidying the phone again keeps the carrier's answer.
    apply_company_phone(c)
    assert c.phone_type == "mobile"
