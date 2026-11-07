"""What wins (app/insights.py)."""
from datetime import datetime, timedelta, timezone

from app import insights
from app.models import Activity, Company, Contact

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def biz(i, status="new", **kw):
    fields = dict(name=f"Biz {i}", source="google", source_ref=str(i), lead_status=status, contacts=[])
    fields.update(kw)
    return Company(**fields)


def test_outcomes():
    assert insights.outcome(biz(1, "won"), None, NOW) == "good"
    assert insights.outcome(biz(1, "not_interested"), None, NOW) == "bad"
    assert insights.outcome(biz(1, "new"), None, NOW) is None
    # A person replied: the business counts as good.
    assert insights.outcome(biz(1, contacts=[Contact(full_name="A", lead_status="replied")]), None, NOW) == "good"
    # Contacted: no reply after 14 days is bad; before that, still open.
    assert insights.outcome(biz(1, "contacted"), NOW - timedelta(days=20), NOW) == "bad"
    assert insights.outcome(biz(1, "contacted"), NOW - timedelta(days=3), NOW) is None


def test_signals_that_win_are_shown_first(client, db):
    # 10 businesses without a website: 7 replied. 10 with a website: 2 replied.
    rows = []
    for i in range(10):
        rows.append(biz(i, "replied" if i < 7 else "lost", category="Dentist"))
    for i in range(10, 20):
        rows.append(biz(i, "replied" if i < 12 else "not_interested", website="https://x.example",
                        domain="x.example", category="Dentist"))
    rows.append(biz(99, "contacted"))  # contacted just now: open
    db.add_all(rows)
    db.flush()
    db.add(Activity(company_id=rows[-1].id, kind="status", text="Status: New → Contacted",
                    created_at=datetime.now(timezone.utc)))
    db.commit()

    r = client.get("/api/quality/insights").json()
    assert (r["worked"], r["good"], r["open"]) == (20, 9, 1)
    assert r["baseline"] == 0.45
    top = r["signals"][0]
    assert top["signal"] == "No website" and top["leads"] == 10 and top["rate"] == 0.7
    assert round(top["lift"], 2) == 0.5 and top["advice"] == "Works for you: worth more points"
    assert "Type: dentist" in [s["signal"] for s in r["signals"]]  # on every lead: no lift
    assert {s["signal"] for s in r["signals"]} >= {"No website", "Found on Google Maps"}


def test_nothing_worked_yet(client):
    r = client.get("/api/quality/insights").json()
    assert (r["worked"], r["baseline"], r["signals"]) == (0, None, [])
