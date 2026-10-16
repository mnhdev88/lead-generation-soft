"""Google Sheets export (app/sheets.py and /api/export/sheets*). No real Google requests:
the token function is replaced and HTTP goes through httpx.MockTransport."""
import json

import httpx
import pytest

from app import config, sheets
from app.models import Company, Contact

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789"
ACCOUNT = "leadgen@my-project.iam.gserviceaccount.com"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "key.json"
    path.write_text(json.dumps({"type": "service_account", "client_email": ACCOUNT, "private_key": "x"}))
    monkeypatch.setattr(config, "GOOGLE_SHEETS_CREDENTIALS_FILE", str(path))
    monkeypatch.setattr(sheets, "_access_token", lambda path: "fake-token")
    return path


class FakeSheets:
    """Pretends to be the Sheets REST API and records every request."""

    def __init__(self, tabs=("Sheet1",), fail: dict | None = None):
        self.tabs = list(tabs)
        self.fail = fail or {}  # step name -> (status, body)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer fake-token"
        path = request.url.path
        step = "values" if "/values/" in path else "batch" if path.endswith(":batchUpdate") else "meta"
        if step in self.fail:
            status, body = self.fail[step]
            return httpx.Response(status, json=body)
        if step == "meta":
            return httpx.Response(200, json={"sheets": [{"properties": {"title": t}} for t in self.tabs]})
        if step == "batch":
            title = json.loads(request.content)["requests"][0]["addSheet"]["properties"]["title"]
            self.tabs.append(title)
            return httpx.Response(200, json={"replies": [{"addSheet": {"properties": {"sheetId": 777, "title": title}}}]})
        return httpx.Response(200, json={"updatedRows": 3})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


# ----- status ---------------------------------------------------------------------


def test_status_not_configured(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_CREDENTIALS_FILE", "")
    st = sheets.status()
    assert st.configured is False and "GOOGLE_SHEETS_CREDENTIALS_FILE" in st.reason


def test_status_missing_or_bad_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_CREDENTIALS_FILE", str(tmp_path / "nope.json"))
    assert "not found" in sheets.status().reason
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(config, "GOOGLE_SHEETS_CREDENTIALS_FILE", str(bad))
    assert "valid JSON" in sheets.status().reason
    bad.write_text(json.dumps({"type": "authorized_user"}))
    assert "service-account" in sheets.status().reason


def test_status_configured(key_file):
    st = sheets.status()
    assert st.configured is True and st.service_account_email == ACCOUNT and st.reason is None


def test_status_endpoint(client, key_file):
    r = client.get("/api/export/sheets/status")
    assert r.json() == {"configured": True, "service_account_email": ACCOUNT, "reason": None}


# ----- export_rows ----------------------------------------------------------------


@pytest.mark.parametrize("text", [
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0",
    f"  {SHEET_ID} ",
])
def test_parse_spreadsheet_id(text):
    assert sheets.parse_spreadsheet_id(text) == SHEET_ID


def test_parse_spreadsheet_id_rejects_other_links():
    with pytest.raises(ValueError, match="Google Sheets link"):
        sheets.parse_spreadsheet_id("https://example.com/my-sheet")


def test_export_rows_adds_tab_and_writes_values(key_file):
    api = FakeSheets()
    result = sheets.export_rows(["name", "score"], [["Acme", 5], ["Beta", None]],
                                f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit", "My leads",
                                transport=api.transport)
    assert result.tab_name == "My leads" and result.rows == 2
    assert result.url == f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=777"

    meta, batch, values = api.requests
    assert meta.method == "GET" and meta.url.path == f"/v4/spreadsheets/{SHEET_ID}"
    add = json.loads(batch.content)["requests"][0]["addSheet"]["properties"]
    # one spare row: Google refuses to freeze every row of a sheet
    assert add["gridProperties"] == {"rowCount": 4, "columnCount": 2, "frozenRowCount": 1}
    assert values.method == "PUT" and values.url.params["valueInputOption"] == "RAW"
    body = json.loads(values.content)
    assert body["range"] == "'My leads'!A1"
    assert body["values"] == [["name", "score"], ["Acme", 5], ["Beta", ""]]


def test_default_tab_name_is_unique(key_file):
    api = FakeSheets()
    first = sheets.export_rows(["a"], [], SHEET_ID, None, transport=api.transport)
    assert first.tab_name.startswith("Leads 20")
    api.tabs.append("x's tab")
    second = sheets.export_rows(["a"], [], SHEET_ID, first.tab_name.upper(), transport=api.transport)
    assert second.tab_name == first.tab_name.upper() + " (2)"
    third = sheets.export_rows(["a"], [], SHEET_ID, "x's tab", transport=api.transport)
    assert third.tab_name == "x's tab (2)"
    assert json.loads(api.requests[-1].content)["range"] == "'x''s tab (2)'!A1"


@pytest.mark.parametrize("step, status, body, message", [
    ("meta", 403, {"error": {"message": "The caller does not have permission"}}, f"Share the sheet with {ACCOUNT} as Editor"),
    ("meta", 404, {"error": {"message": "Requested entity was not found."}}, "Spreadsheet not found"),
    ("meta", 403, {"error": {"message": "Google Sheets API has not been used in project 1 before or it is disabled"}},
     "not enabled"),
    ("batch", 400, {"error": {"message": "Invalid sheet name"}}, "Google Sheets error (400): Invalid sheet name"),
    ("values", 429, {"error": {"message": "Quota exceeded"}}, "too many requests"),
])
def test_google_errors_become_friendly_messages(key_file, step, status, body, message):
    api = FakeSheets(fail={step: (status, body)})
    with pytest.raises(ValueError) as err:
        sheets.export_rows(["a"], [["1"]], SHEET_ID, transport=api.transport)
    assert message in str(err.value)


def test_network_error(key_file):
    def boom(request):
        raise httpx.ConnectError("offline")

    with pytest.raises(ValueError, match="Could not reach Google Sheets"):
        sheets.export_rows(["a"], [], SHEET_ID, transport=httpx.MockTransport(boom))


def test_sign_in_failure(key_file, monkeypatch):
    def fail(path):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(sheets, "_access_token", fail)
    with pytest.raises(ValueError, match="Could not sign in to Google.*invalid_grant"):
        sheets.export_rows(["a"], [], SHEET_ID, transport=FakeSheets().transport)


def test_export_not_configured(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_CREDENTIALS_FILE", "")
    with pytest.raises(ValueError, match="not set up"):
        sheets.export_rows(["a"], [], SHEET_ID)


# ----- POST /api/export/sheets ------------------------------------------------------


@pytest.fixture
def captured(monkeypatch, key_file):
    """Replace export_rows in the router path and remember what it received."""
    calls = []

    def fake_export(headers, rows, spreadsheet, tab_name=None, **kw):
        calls.append((headers, rows, spreadsheet, tab_name))
        return sheets.SheetsExportResult(url="https://docs.google.com/x", tab_name=tab_name or "Leads", rows=len(rows))

    monkeypatch.setattr(sheets, "export_rows", fake_export)
    return calls


def test_export_companies_with_filters(client, db, captured):
    db.add_all([
        Company(name="Has Phone", source="osm", source_ref="1", phone="1"),
        Company(name="No Phone", source="osm", source_ref="2"),
    ])
    db.commit()
    r = client.post("/api/export/sheets", json={
        "kind": "companies", "filters": {"has_phone": True, "sort": "score"}, "spreadsheet": SHEET_ID,
        "tab_name": "T"})
    assert r.status_code == 200 and r.json() == {"url": "https://docs.google.com/x", "tab_name": "T", "rows": 1}
    headers, rows, spreadsheet, _ = captured[0]
    assert headers[0] == "name" and rows[0][0] == "Has Phone" and spreadsheet == SHEET_ID


def test_export_people_with_filters(client, db, captured):
    co = Company(name="Acme", source="osm", source_ref="1")
    co.contacts += [Contact(full_name="Boss", seniority="owner", is_decision_maker=True),
                    Contact(full_name="Helper", seniority="staff")]
    db.add(co)
    db.commit()
    r = client.post("/api/export/sheets", json={
        "kind": "people", "filters": {"decision_makers": True}, "spreadsheet": SHEET_ID})
    assert r.status_code == 200
    headers, rows, _, _ = captured[0]
    assert headers[0] == "name" and [row[0] for row in rows] == ["Boss"]
    assert rows[0][headers.index("company")] == "Acme"


def test_export_endpoint_reports_problems_as_400(client, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_CREDENTIALS_FILE", "")
    r = client.post("/api/export/sheets", json={"kind": "people", "filters": {}, "spreadsheet": SHEET_ID})
    assert r.status_code == 400 and "not set up" in r.json()["detail"]
    r = client.post("/api/export/sheets", json={"kind": "nope", "filters": {}, "spreadsheet": SHEET_ID})
    assert r.status_code == 422


def test_export_with_no_rows_still_has_an_unfrozen_row(key_file):
    api = FakeSheets()
    result = sheets.export_rows(["name"], [], f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
                                "Empty", transport=api.transport)
    assert result.rows == 0
    add = json.loads(api.requests[1].content)["requests"][0]["addSheet"]["properties"]
    grid = add["gridProperties"]
    assert grid["rowCount"] > grid["frozenRowCount"]
