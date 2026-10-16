"""Export rows to a Google Sheet (optional, free).

Setup (once):
 1. In Google Cloud console create a project, enable the "Google Sheets API", create a
    service account and download its JSON key.
 2. Put the key file path in backend/.env: GOOGLE_SHEETS_CREDENTIALS_FILE=C:/keys/leadgen.json
 3. Open your Google Sheet -> Share -> add the service account's email as Editor.

Each export adds a new tab (e.g. "Leads 2026-10-16 14.30") to that sheet and fills it.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from . import config
from .schemas import SheetsExportResult, SheetsStatus

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets/"
SETUP_HINT = (
    "Set GOOGLE_SHEETS_CREDENTIALS_FILE in backend/.env to the path of a Google service-account "
    "JSON key (see backend/app/sheets.py), then restart the backend."
)


def _credentials_path() -> Path | None:
    raw = config.GOOGLE_SHEETS_CREDENTIALS_FILE  # read at call time so .env changes/tests apply
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else config.BASE_DIR / path


def _read_key_file() -> tuple[Path, dict]:
    """Return (path, parsed JSON) or raise ValueError with a friendly message."""
    path = _credentials_path()
    if path is None:
        raise ValueError(f"Google Sheets export is not set up. {SETUP_HINT}")
    if not path.is_file():
        raise ValueError(f"Google service-account key file not found: {path}")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"Could not read the Google service-account key file {path} (is it valid JSON?)")
    if not isinstance(info, dict) or info.get("type") != "service_account" or not info.get("client_email"):
        raise ValueError(f"{path.name} is not a service-account key (it needs \"type\": \"service_account\").")
    return path, info


def status() -> SheetsStatus:
    try:
        _, info = _read_key_file()
    except ValueError as e:
        return SheetsStatus(configured=False, reason=str(e))
    return SheetsStatus(configured=True, service_account_email=info["client_email"])


def parse_spreadsheet_id(text: str) -> str:
    """Accept a full Google Sheets URL or just the spreadsheet id."""
    text = text.strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    raise ValueError("That doesn't look like a Google Sheets link. Paste the address of the sheet from your browser.")


def _access_token(key_path: Path) -> str:
    """Sign in as the service account and return an OAuth access token.
    (Tests replace this function so no real Google request is made.)"""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(str(key_path), scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


def _unique_title(wanted: str, existing: set[str]) -> str:
    title, n = wanted, 2
    lowered = {t.lower() for t in existing}  # Sheets tab names are case-insensitive
    while title.lower() in lowered:
        title = f"{wanted} ({n})"
        n += 1
    return title


def _a1_range(tab: str) -> str:
    """"Leads 1" -> "'Leads 1'!A1" (quotes inside the name are doubled)."""
    return "'" + tab.replace("'", "''") + "'!A1"


def _check(resp: httpx.Response, account_email: str) -> dict:
    """Raise ValueError with a clear message for Google API errors; return the JSON body."""
    if resp.is_success:
        return resp.json() if resp.content else {}
    try:
        message = resp.json().get("error", {}).get("message", "")
    except ValueError:
        message = ""
    if resp.status_code == 403 and ("has not been used" in message or "disabled" in message):
        raise ValueError(
            "The Google Sheets API is not enabled for your service account's Google Cloud project. "
            "Enable it in console.cloud.google.com (APIs & Services), wait a minute, and try again."
        )
    if resp.status_code == 403:
        raise ValueError(f"No access to that sheet. Share the sheet with {account_email} as Editor, then try again.")
    if resp.status_code == 404:
        raise ValueError(f"Spreadsheet not found. Check the link, and share the sheet with {account_email} as Editor.")
    if resp.status_code == 429:
        raise ValueError("Google Sheets is busy (too many requests). Wait a minute and try again.")
    raise ValueError(f"Google Sheets error ({resp.status_code}): {message or resp.reason_phrase}")


def export_rows(
    headers: list[str],
    rows: list[list],
    spreadsheet: str,
    tab_name: str | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> SheetsExportResult:
    """Add a new tab to the spreadsheet and write headers + rows into it.
    Raises ValueError with a friendly message on any problem."""
    spreadsheet_id = parse_spreadsheet_id(spreadsheet)
    key_path, info = _read_key_file()
    account_email = info["client_email"]
    try:
        token = _access_token(key_path)
    except Exception as e:  # google-auth raises many kinds of errors
        raise ValueError(f"Could not sign in to Google with the service account: {e}")

    wanted = (tab_name or "").strip() or f"Leads {datetime.now():%Y-%m-%d %H.%M}"
    values = [list(headers)] + [["" if v is None else v for v in row] for row in rows]

    try:
        base = SHEETS_API + spreadsheet_id  # full URLs, so "<id>:batchUpdate" is never read as "scheme:path"
        with httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            transport=transport,
        ) as client:
            # 1. Existing tab names (also checks that we can open the sheet).
            meta = _check(client.get(base, params={"fields": "sheets.properties.title"}), account_email)
            existing = {s.get("properties", {}).get("title", "") for s in meta.get("sheets", [])}
            title = _unique_title(wanted[:100], existing)

            # 2. Add the tab, sized to fit (a new tab only has 26 columns by default).
            add = {
                "addSheet": {
                    "properties": {
                        "title": title,
                        "gridProperties": {
                            # +1: Google refuses to freeze ALL rows (an export with no rows
                            # would only have the frozen header row).
                            "rowCount": len(values) + 1,
                            "columnCount": max(len(headers), 1),
                            "frozenRowCount": 1,
                        },
                    }
                }
            }
            reply = _check(client.post(f"{base}:batchUpdate", json={"requests": [add]}), account_email)
            sheet_id = reply.get("replies", [{}])[0].get("addSheet", {}).get("properties", {}).get("sheetId", 0)

            # 3. Write the values. RAW = stored as typed, so nothing is run as a formula.
            _check(
                client.put(
                    f"{base}/values/{quote(_a1_range(title), safe='')}",
                    params={"valueInputOption": "RAW"},
                    json={"range": _a1_range(title), "majorDimension": "ROWS", "values": values},
                ),
                account_email,
            )
    except httpx.HTTPError as e:
        raise ValueError(f"Could not reach Google Sheets: {e}")

    return SheetsExportResult(
        url=f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_id}",
        tab_name=title,
        rows=len(rows),
    )
