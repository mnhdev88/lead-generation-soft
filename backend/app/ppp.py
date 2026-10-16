"""US PPP loans (2020-21): how many people a US business said it employed.

During Covid the US government lent money to ~11.5 million small businesses (the Paycheck
Protection Program). The SBA publishes every loan, and each one has "JobsReported": the
headcount the business itself wrote on its application (capped at 500). That is the best
free size figure there is for small US businesses, so we look our US leads up in it.

The data is 14 CSV files (~5 GB) at data.sba.gov. We read them straight from the internet
into a separate SQLite file, config.PPP_DB_PATH (backend/data/ppp.db), keeping only the
columns we need. The CSVs are never saved to disk. leads.db is not touched.

    python -m app.ppp download                 get all files (resumes where it stopped)
    python -m app.ppp download --states TX,CA  only keep loans of these states (smaller)
    python -m app.ppp import FILE.csv ...      use CSV files you downloaded yourself
    python -m app.ppp match                    look up saved companies not checked yet
    python -m app.ppp match --all              look up every saved company again
    python -m app.ppp stats                    what is in the PPP database

Matching (precision first: a wrong match shows a wrong headcount, and no number is better
than a wrong one). Only loans in the company's 5-digit ZIP are considered (and the state
must agree). "Same street address" means the same house number and street, and the
suite/unit doesn't differ (two tenants of one building are two businesses). A loan matches when:
  a) the same name (small spelling differences allowed) at the same street address
     -> confidence "high", or
  b) the same street address, and every word of the company's name is in the loan's name
     ("Joe's Pizza" = "JOES PIZZA & GRILL LLC"), with at least one unusual word -> "medium", or
  c) exactly the same words, the street address unknown on one side (or only the house
     number / suite differs, on the same street), and the name is unusual enough to trust
     on its own: not short, not only trade words and the town ("Austin Dental"), not a
     chain or franchise -> "medium".
Chains and franchise loans ("SUBWAY", FranchiseName set) only match at the same street
address: every store of a brand has the same name. Legal words (LLC, INC, PC, PA...) are
ignored; titles like DDS or MD are kept, because "JOHN SMITH DDS" and "JOHN SMITH CPA" are
two different businesses. The best match wins. When two different borrowers are equally
good we don't guess. One borrower = the same name at the same house number (its first and
second draw), so two stores of one name are two borrowers.
"""
import argparse
import csv
import io
import math
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from . import config
from .dedupe import _name_words
from .models import Company
from .phones import country_code

# Where the SBA lists the files (a catalog of all its datasets) and our dataset's id there.
CATALOG_URL = "https://data.sba.gov/data.json"
DATASET_ID = "SBA-OCA-2022-07-001"
FILES_BASE = "https://data.sba.gov/sites/default/files/distribution/SBA-OCA-2022-07-001/"
# Used when the catalog can't be read. The date in the names changes when SBA re-issues data.
KNOWN_FILES = ["public_150k_plus_240930.csv"] + [f"public_up_to_150k_{n}_240930.csv" for n in range(1, 13)]
KNOWN_URLS = [FILES_BASE + name for name in KNOWN_FILES]

DOWNLOAD_RETRIES = 3  # attempts per file
RETRY_WAIT = 30  # seconds before trying a failed file again
INSERT_BATCH = 50_000  # loans per database transaction
PROGRESS_EVERY = 100_000  # print progress every this many CSV rows
CSV_FIELD_LIMIT = 10_000_000  # characters; Python's default (131,072) stops on one broken row
MAX_JOBS = 100_000  # a JobsReported above this is a typo, not a headcount
MAX_AMOUNT = 1e9  # dollars; PPP loans were at most $10 million
JOURNAL_RETRIES = 5  # attempts to switch the PPP file back to one plain file at the end
JOURNAL_WAIT = 2  # seconds between those attempts

# Values that mean "nothing here" in the CSVs.
PLACEHOLDERS = {"", "N/A", "NA", "NOT AVAILABLE", "EXEMPTION 6", "EXEMPTION 4", "UNANSWERED"}

# Legal-form words, ignored when comparing names (on top of dedupe.NAME_NOISE: llc, inc, pc...).
LEGAL_WORDS = {"pa", "lp", "lc", "lllp", "incorporated", "apc", "apmc", "psc", "sc", "chartered", "chtd"}
# Words a legal name often adds or leaves out: "ACME PLUMBING SERVICES LLC" = "Acme Plumbing".
FILLER_WORDS = {"group", "holdings", "holding", "enterprises", "enterprise", "services", "service"}
# Trade words that thousands of businesses share. A name made only of these (and the town)
# doesn't tell one business from another: "Austin Family Dentistry", "Main Street Dental".
GENERIC_WORDS = {
    "dental", "dentist", "dentistry", "dentists", "orthodontics", "family", "medicine", "medical",
    "health", "care", "clinic", "center", "centre", "law", "legal", "firm", "office", "offices",
    "associates", "partners", "plumbing", "plumbers", "plumber", "electric", "electrical",
    "roofing", "construction", "contractors", "auto", "automotive", "repair", "repairs", "body",
    "shop", "store", "market", "restaurant", "cafe", "bistro", "grill", "bar", "kitchen", "pizza",
    "tacos", "taco", "bakery", "salon", "spa", "beauty", "hair", "nails", "barber", "fitness",
    "gym", "insurance", "agency", "realty", "real", "estate", "properties", "property",
    "management", "consulting", "solutions", "systems", "technologies", "tech", "home", "homes",
    "cleaning", "landscaping", "lawn", "pest", "control", "heating", "cooling", "air", "hvac",
    "pediatric", "pediatrics", "smile", "smiles", "veterinary", "animal", "pet", "hospital",
    "pharmacy", "chiropractic", "therapy", "physical", "accounting", "tax", "financial",
    "american", "national", "united", "city", "county", "street", "main", "first",
}
# Titles and directions: "JOHN SMITH DDS" is not "JOHN SMITH CPA", "SERVPRO OF NORTH AUSTIN"
# is not "SERVPRO OF SOUTH AUSTIN".
TITLE_WORDS = {"dds", "dmd", "md", "do", "od", "dvm", "cpa", "esq", "jd", "phd", "rn", "np", "dc", "ea", "pe"}
DIRECTION_WORDS = {
    "n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west", "northeast",
    "northwest", "southeast", "southwest", "central", "downtown", "midtown", "uptown",
}
# Chains and franchises (written as name_key writes them): every store has the same name,
# so these only match a loan at the same street address.
CHAIN_NAMES = {
    "subway", "mcdonalds", "servpro", "great clips", "supercuts", "sport clips", "dominos",
    "pizza hut", "papa johns", "little caesars", "burger king", "wendys", "taco bell", "kfc",
    "popeyes", "arbys", "sonic", "dairy queen", "chick fil a", "jimmy johns", "jersey mikes",
    "firehouse subs", "wingstop", "dunkin", "starbucks", "smoothie king", "tropical smoothie",
    "culvers", "kumon", "mathnasium", "jiffy lube", "midas", "meineke", "h r block",
    "edward jones", "state farm", "allstate", "farmers insurance", "re max", "remax",
    "keller williams", "coldwell banker", "century 21", "ups store", "anytime fitness",
    "planet fitness", "orangetheory", "massage envy", "european wax center", "molly maid",
    "merry maids", "two men a truck", "mr rooter", "roto rooter", "mr electric", "goddard school",
    "primrose school", "kiddie academy", "minuteman press", "fastsigns", "sir speedy",
}
SAME_NAME_RATIO = 0.92  # "Home Slice Pizza" vs "HOME SLICE PIZZAS": a spelling difference only
MIN_NAME_LETTERS = 10  # a name trusted without an address needs at least this many letters
STEM_LETTERS = 5  # "plumbers" and "plumbing" are one word when their first 5 letters agree
NOT_APPLICABLE = {"not_applicable": True}  # company.ppp of a company outside the US / without a ZIP
DBA_RE = re.compile(r"\b(?:dba|d/b/a|doing business as)\b", re.IGNORECASE)

# Street words that are written in many ways ("South", "S", "Street", "St"): left out when
# comparing street names, so "1415 S Congress Ave" = "1415 SOUTH CONGRESS AVENUE".
STREET_NOISE = {
    "n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west", "northeast",
    "northwest", "southeast", "southwest", "st", "street", "ave", "av", "avenue", "rd", "road",
    "blvd", "boulevard", "dr", "drive", "ln", "lane", "way", "ct", "court", "pl", "place", "pkwy",
    "parkway", "hwy", "highway", "cir", "circle", "ter", "terrace", "trl", "trail", "sq", "square",
    "fwy", "freeway", "expy", "expressway", "the",
}
UNIT_RE = re.compile(r"(?:\b(?:ste|suite|unit|apt|bldg|building|fl|floor|rm|room)\b|#).*$", re.IGNORECASE)
UNIT_START_RE = re.compile(r"\s*(?:(?:ste|suite|unit|apt|bldg|building|fl|floor|rm|room)\b|#)", re.IGNORECASE)
UNIT_WORDS = {"ste", "suite", "unit", "apt", "bldg", "building", "fl", "floor", "rm", "room", "no"}
LEADING_NUMBER_RE = re.compile(r"\s*(\d+)")
HOUSE_RE = re.compile(r"^\s*(\d+)[a-z]?(?:-\w+)?\s+([a-z].*)$", re.IGNORECASE)

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "VI", "GU", "AS", "MP", "AA", "AE", "AP",
}
# OpenStreetMap sometimes writes the state in full.
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "puerto rico": "PR",
}
# ", TX 78701" or ", TX 78701-1234" in an address
STATE_ZIP_RE = re.compile(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")
SOURCE_ZIP_RE = re.compile(r"^\s*(\d{5})(?:-?\d{4})?\s*$")  # a ZIP in the source data: "78701", "78701-1234"

# The CSV columns we read (all 14 files share one 53-column header).
NEEDED_COLUMNS = (
    "LoanNumber", "DateApproved", "ProcessingMethod", "BorrowerName", "BorrowerAddress",
    "BorrowerCity", "BorrowerState", "BorrowerZip", "InitialApprovalAmount", "CurrentApprovalAmount",
    "FranchiseName", "ProjectState", "ProjectZip", "JobsReported", "NAICSCode", "BusinessType",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS loans (
    loan_number TEXT, file_id INTEGER, name_key TEXT, name TEXT, address TEXT, city TEXT,
    state TEXT, zip5 TEXT, jobs INTEGER, date_approved TEXT, amount REAL, naics TEXT,
    business_type TEXT, draw TEXT, franchise TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE, states TEXT, rows INTEGER, finished_at TEXT
);
-- So "remove this file's rows" (before importing a file again) doesn't read the whole table.
CREATE INDEX IF NOT EXISTS loans_file ON loans (file_id);
"""
LOAN_COLUMNS = (
    "name, address, city, state, zip5, jobs, date_approved, amount, naics, business_type, draw, "
    "loan_number, name_key, franchise"
)


# ----- reading the CSV files -----------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Trimmed text, None for blanks and placeholders like 'N/A'."""
    value = (value or "").strip()
    return None if value.upper() in PLACEHOLDERS else value


def _zip5(value: str | None) -> str | None:
    """'29456-3517' -> '29456'; '2134' (lost its leading zero) -> '02134'."""
    digits = (value or "").strip().split("-")[0]
    if not digits.isdigit() or len(digits) < 3:
        return None
    return digits.zfill(5)[:5]


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value else None
    except (ValueError, OverflowError):  # OverflowError: 'inf'
        return None


def _float(value: str | None) -> float | None:
    try:
        number = float(value) if value else None
    except ValueError:
        return None
    return number if number is not None and math.isfinite(number) else None


def _iso_date(value: str | None) -> str | None:
    """'01/22/2021' -> '2021-01-22' (by hand: strptime is slow for 11 million rows)"""
    parts = (value or "").strip().split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts) or len(parts[2]) != 4:
        return None
    return f"{parts[2]}-{int(parts[0]):02d}-{int(parts[1]):02d}"


def name_key(name: str | None) -> str:
    """The words of a name that matter: 'DAVID R. GORE, O.D. PC' -> 'david r gore od'."""
    text = re.sub(r"[.'’]", "", name or "")  # O.D. -> OD, Joe's -> Joes
    return " ".join(w for w in _name_words(text) if w not in LEGAL_WORDS)


def _loan_row(row: list[str], col: dict[str, int], file_id: int, states: set[str] | None) -> tuple | None:
    """One CSV row -> a row for the loans table, or None to skip it."""
    name = _clean(row[col["BorrowerName"]])
    key = name_key(name)
    zip5 = _zip5(row[col["BorrowerZip"]]) or _zip5(row[col["ProjectZip"]])
    if not key or not zip5:  # redacted names ('Exemption 6') or no ZIP: can't be matched
        return None
    state = (_clean(row[col["BorrowerState"]]) or _clean(row[col["ProjectState"]]) or "").upper() or None
    if states and state not in states:
        return None
    jobs = _int(row[col["JobsReported"]])
    if jobs is not None and not 0 < jobs <= MAX_JOBS:  # 0 = not given; huge = a typo
        jobs = None
    amount = _float(row[col["CurrentApprovalAmount"]]) or _float(row[col["InitialApprovalAmount"]])
    if amount is not None and not 0 <= amount <= MAX_AMOUNT:
        amount = None
    return (
        row[col["LoanNumber"]].strip(), file_id, key, name, _clean(row[col["BorrowerAddress"]]),
        _clean(row[col["BorrowerCity"]]), state, zip5, jobs,
        _iso_date(row[col["DateApproved"]]), amount, _clean(row[col["NAICSCode"]]),
        _clean(row[col["BusinessType"]]),
        "second" if row[col["ProcessingMethod"]].strip().upper() == "PPS" else "first",
        _clean(row[col["FranchiseName"]]),
    )


class _ChunkReader(io.RawIOBase):
    """Makes a stream of byte chunks (a download, or a file read in pieces) readable like a
    file, so the csv module can read it line by line without holding it all in memory."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._left = memoryview(b"")
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while not self._left:
            try:
                self._left = memoryview(next(self._chunks))
            except StopIteration:
                return 0
        n = min(len(buffer), len(self._left))
        buffer[:n] = self._left[:n]
        self._left = self._left[n:]
        self.bytes_read += n
        return n


def _connect_rw() -> sqlite3.Connection:
    """Open (or create) the PPP database for importing: fast settings for bulk loading."""
    config.PPP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.PPP_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")  # an app crash is safe; a power cut may need a re-import
    conn.execute("PRAGMA cache_size=-65536")  # 64 MB
    conn.executescript(SCHEMA)
    return conn


def _start_import(conn: sqlite3.Connection) -> None:
    """Before the first file of a run is imported: remove the ZIP index. Loading is much
    faster without it, and without it the app doesn't match against half-loaded data
    (available() needs the index). _finish_import builds it again."""
    conn.execute("DROP INDEX IF EXISTS loans_zip")
    conn.commit()


def _finish_import(conn: sqlite3.Connection) -> None:
    """Indexes are made once at the end: much faster than keeping them up to date while loading."""
    print("Building the ZIP index (a few minutes for the full data)...", flush=True)
    conn.execute("CREATE INDEX IF NOT EXISTS loans_zip ON loans (zip5)")
    conn.commit()
    # Back to one plain file, which the app opens read-only. This needs the file to itself,
    # so it fails while the app is reading it: try a few times, then explain.
    for attempt in range(1, JOURNAL_RETRIES + 1):
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            return
        except sqlite3.OperationalError:
            if attempt < JOURNAL_RETRIES:
                time.sleep(JOURNAL_WAIT)
    print("The loans are saved, but the app is reading the PPP file right now, so it couldn't be "
          "tidied up. Stop the app and run:  python -m app.ppp stats   (or run the download again).")


def _unfinished_files(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT name FROM files WHERE finished_at IS NULL ORDER BY id")]


def _print_failed(failed: list[str]) -> None:
    if failed:
        print(f"\nThese files failed: {', '.join(failed)}. Run the same command again to retry them.")
        print("Looking companies up in the PPP data is paused until every file has finished.")


def _parse_states(text: str | None) -> set[str] | None:
    """'tx, ca' -> {'TX', 'CA'}; empty -> None (all states)."""
    states = {s.strip().upper() for s in (text or "").split(",") if s.strip()}
    unknown = states - US_STATES
    if unknown:
        raise SystemExit(f"Unknown state code(s): {', '.join(sorted(unknown))}. Use 2-letter codes like TX,CA.")
    return states or None


def _states_text(states: set[str] | None) -> str:
    return ",".join(sorted(states)) if states else ""  # "" = all states


def is_imported(conn: sqlite3.Connection, file_name: str, states: set[str] | None) -> bool:
    """Was this file fully imported before, with (at least) these states?"""
    row = conn.execute("SELECT states, finished_at FROM files WHERE name = ?", (file_name,)).fetchone()
    if not row or not row[1]:
        return False
    done = set(row[0].split(",")) if row[0] else None
    return done is None or (states is not None and states <= done)


def _states_for_reimport(conn: sqlite3.Connection, file_name: str, states: set[str] | None) -> set[str] | None:
    """Importing a file again for more states keeps the states it already had."""
    row = conn.execute("SELECT states, finished_at FROM files WHERE name = ?", (file_name,)).fetchone()
    if states is None or not row or not row[1] or not row[0]:
        return states
    return states | set(row[0].split(","))


def import_stream(conn, chunks, file_name: str, states: set[str] | None = None,
                  total_bytes: int | None = None, progress=print) -> int:
    """Read one PPP CSV (given as byte chunks) into the loans table. Returns loans saved.
    The file's rows from an earlier, unfinished attempt are removed first."""
    conn.execute("INSERT OR IGNORE INTO files (name) VALUES (?)", (file_name,))
    file_id = conn.execute("SELECT id FROM files WHERE name = ?", (file_name,)).fetchone()[0]
    conn.execute("UPDATE files SET finished_at = NULL, rows = NULL WHERE id = ?", (file_id,))
    conn.execute("DELETE FROM loans WHERE file_id = ?", (file_id,))
    conn.commit()

    raw = _ChunkReader(chunks)
    # The files are Latin-1 / Windows-1252 (not UTF-8), with Windows line endings.
    text = io.TextIOWrapper(io.BufferedReader(raw, 1 << 20), encoding="cp1252", errors="replace", newline="")
    csv.field_size_limit(CSV_FIELD_LIMIT)  # a stray quote can swallow many lines into one field
    reader = csv.reader(text)
    header = next(reader, None) or []
    col = {name.strip(): i for i, name in enumerate(header)}
    missing = [c for c in NEEDED_COLUMNS if c not in col]
    if missing:
        raise ValueError(f"{file_name} doesn't look like a PPP file (missing columns: {', '.join(missing)})")
    width = len(header)

    batch, saved, seen, skipped = [], 0, 0, 0
    insert = "INSERT INTO loans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    for row in reader:
        seen += 1
        if len(row) >= width:
            try:
                loan = _loan_row(row, col, file_id, states)
            except (ValueError, OverflowError):  # one odd row must not stop the whole file
                skipped += 1
                loan = None
            if loan:
                batch.append(loan)
        if len(batch) >= INSERT_BATCH:
            conn.executemany(insert, batch)
            conn.commit()
            saved += len(batch)
            batch = []
        if seen % PROGRESS_EVERY == 0:
            done = f"{raw.bytes_read * 100 // total_bytes}%, " if total_bytes else ""
            progress(f"  {file_name}: {done}{seen:,} rows read, {saved + len(batch):,} loans kept")
    conn.executemany(insert, batch)
    saved += len(batch)
    if total_bytes and raw.bytes_read < total_bytes:  # the connection dropped before the end
        conn.commit()
        raise IOError(f"{file_name}: download stopped early ({raw.bytes_read:,} of {total_bytes:,} bytes)")
    if skipped:
        progress(f"  {file_name}: {skipped:,} unreadable rows skipped")
    conn.execute(
        "UPDATE files SET states = ?, rows = ?, finished_at = ? WHERE id = ?",
        (_states_text(states), saved, datetime.now(timezone.utc).isoformat(timespec="seconds"), file_id),
    )
    conn.commit()
    return saved


def import_files(paths: list[str], states: set[str] | None = None, force: bool = False) -> int:
    """`python -m app.ppp import`: CSV files already on disk. Returns loans saved."""
    conn = _connect_rw()
    total, failed, started = 0, [], False
    try:
        for path in map(Path, paths):
            if not force and is_imported(conn, path.name, states):
                print(f"{path.name}: already imported, skipped (use --force to import it again)")
                continue
            if not started:
                _start_import(conn)
                started = True
            file_states = _states_for_reimport(conn, path.name, states)
            print(f"Importing {path.name}...", flush=True)
            try:
                with open(path, "rb") as f:
                    chunks = iter(lambda: f.read(1 << 20), b"")
                    saved = import_stream(conn, chunks, path.name, file_states, path.stat().st_size)
            except (OSError, ValueError, csv.Error) as e:  # one bad file: go on with the others
                print(f"{path.name}: failed ({e})")
                failed.append(path.name)
                continue
            print(f"{path.name}: {saved:,} loans saved")
            total += saved
        _finish_import(conn)
    finally:
        conn.close()
    _print_failed(failed)
    return total


# ----- downloading -----------------------------------------------------------------------


def discover_urls(client: httpx.Client) -> list[str]:
    """The CSV links from SBA's catalog (so new file names are picked up), else KNOWN_URLS."""
    try:
        r = client.get(CATALOG_URL, timeout=60)
        r.raise_for_status()
        for dataset in r.json().get("dataset") or []:
            if dataset.get("identifier") == DATASET_ID:
                urls = [
                    d.get("downloadURL") for d in dataset.get("distribution") or []
                    if (d.get("downloadURL") or "").lower().endswith(".csv")
                ]
                if urls:
                    return urls
    except (httpx.HTTPError, ValueError) as e:
        print(f"Couldn't read the SBA catalog ({e}); using the known file list.")
    return KNOWN_URLS


def download(states: set[str] | None = None, force: bool = False, client: httpx.Client | None = None) -> int:
    """`python -m app.ppp download`: stream every PPP file into the database.
    Finished files are skipped, so running it again resumes. Returns loans saved."""
    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": config.USER_AGENT}, follow_redirects=True)
    conn = _connect_rw()
    total, failed, started = 0, [], False
    try:
        urls = discover_urls(client)
        print(f"{len(urls)} PPP files to get" + (f" (keeping {_states_text(states)})" if states else ""))
        for url in urls:
            file_name = url.rstrip("/").rsplit("/", 1)[-1]
            if not force and is_imported(conn, file_name, states):
                print(f"{file_name}: already imported, skipped")
                continue
            if not started:
                _start_import(conn)
                started = True
            file_states = _states_for_reimport(conn, file_name, states)
            for attempt in range(1, DOWNLOAD_RETRIES + 1):
                try:
                    print(f"Downloading {file_name}...", flush=True)
                    with client.stream("GET", url, timeout=httpx.Timeout(60, read=300)) as r:
                        r.raise_for_status()
                        size = int(r.headers.get("content-length") or 0) or None
                        saved = import_stream(conn, r.iter_bytes(1 << 20), file_name, file_states, size)
                    print(f"{file_name}: {saved:,} loans saved")
                    total += saved
                    break
                except (httpx.HTTPError, OSError, ValueError, csv.Error) as e:
                    print(f"{file_name}: failed ({e})")
                    if attempt == DOWNLOAD_RETRIES:
                        failed.append(file_name)
                    else:
                        print(f"Trying again in {RETRY_WAIT} seconds ({attempt + 1} of {DOWNLOAD_RETRIES})...")
                        time.sleep(RETRY_WAIT)
        _finish_import(conn)
    finally:
        conn.close()
        if own_client:
            client.close()
    _print_failed(failed)
    return total


# ----- where a company is ----------------------------------------------------------------


def _state_code(value: str | None) -> str | None:
    """'TX' / 'Texas' -> 'TX'; None when it isn't a US state."""
    text = (value or "").strip()
    if text.upper() in US_STATES:
        return text.upper()
    return STATE_NAMES.get(text.lower())


def _google_part(raw: dict, wanted: str) -> str | None:
    """shortText of one of Google's addressComponents ('postal_code', 'country'...)."""
    for comp in raw.get("addressComponents") or []:
        if isinstance(comp, dict) and wanted in (comp.get("types") or []):
            return comp.get("shortText") or comp.get("longText")
    return None


def us_location(company) -> tuple[str | None, str | None]:
    """(state, 5-digit ZIP) of a US company, e.g. ('TX', '78704'). (None, None) when the
    company isn't in the US or we can't tell. From the source data, else the address text."""
    raw = company.raw if isinstance(company.raw, dict) else {}
    country = (
        country_code(company.country) or country_code(_google_part(raw, "country"))
        or country_code(raw.get("addr:country"))
    )
    if country and country != "US":
        return None, None

    state = _state_code(_google_part(raw, "administrative_area_level_1") or raw.get("addr:state"))
    zip_match = SOURCE_ZIP_RE.match(_google_part(raw, "postal_code") or raw.get("addr:postcode") or "")
    zip5 = zip_match.group(1) if zip_match else None
    if not zip5 or not state:
        # ", TX 78701" in the address (the last one: a street can contain capitals and digits)
        found = [(s, z) for s, z in STATE_ZIP_RE.findall(company.address or "") if s in US_STATES]
        if found:
            state, zip5 = state or found[-1][0], zip5 or found[-1][1]
    if country == "US":
        return state, zip5
    return (state, zip5) if state and zip5 else (None, None)


def _unit(text: str) -> str | None:
    """'Ste. 150' -> '150', '#B' -> 'b', 'Bldg 2 Suite 100' -> '2 100'. None when there is no
    suite/unit, or when it is really a state + ZIP (', FL 33101')."""
    found = UNIT_RE.search(text)
    if not found or STATE_ZIP_RE.search(found.group(0).upper()):
        return None
    words = [w for w in re.split(r"[^a-z0-9]+", found.group(0).lower()) if w and w not in UNIT_WORDS]
    return " ".join(words) or None


def address_parts(address: str | None) -> tuple[str | None, str | None, str | None]:
    """'1415 S Congress Ave Ste 100, Austin, TX' -> ('1415', 'congress', '100'): house number,
    street and suite/unit. (None, None, None) for addresses without a house number (PO boxes, 'N/A')."""
    parts = (address or "").split(",")
    for i, part in enumerate(parts):
        m = HOUSE_RE.match(UNIT_RE.sub("", part).strip())
        if not m:
            continue
        words = [w for w in re.split(r"[^a-z0-9]+", m.group(2).lower()) if w]
        core = [w for w in words if w not in STREET_NOISE] or words
        unit = _unit(part)
        # "1200 Main St, Suite 150, Austin": the unit in the next part
        if not unit and i + 1 < len(parts) and UNIT_START_RE.match(parts[i + 1]):
            unit = _unit(parts[i + 1])
        return m.group(1).lstrip("0") or "0", " ".join(core) or None, unit
    return None, None, None


def street_parts(address: str | None) -> tuple[str | None, str | None]:
    """'1415 S Congress Ave Ste 100, Austin, TX' -> ('1415', 'congress'): house number and street."""
    return address_parts(address)[:2]


def _company_address(company) -> tuple[str | None, str | None, str | None]:
    """House number, street and unit of a company: the address text, else the source data."""
    parts = address_parts(company.address)
    if parts[0]:
        return parts
    raw = company.raw if isinstance(company.raw, dict) else {}
    house = raw.get("addr:housenumber") or _google_part(raw, "street_number")
    road = raw.get("addr:street") or _google_part(raw, "route")
    unit = raw.get("addr:unit") or _google_part(raw, "subpremise")
    if not (house and road):
        return None, None, None
    return address_parts(f"{house} {road}" + (f" #{unit}" if unit else ""))


# ----- matching --------------------------------------------------------------------------


def _name_keys(name: str | None, stored_key: str | None = None) -> list[str]:
    """The name, and for 'ACME LLC DBA HOME SLICE PIZZA' also each part on its own.
    stored_key = name_key(name) when we have it already (it is saved with each loan)."""
    keys = [stored_key or name_key(name)]
    if name and DBA_RE.search(name):
        keys += [name_key(part) for part in DBA_RE.split(name)]
    return [k for k in keys if k]


def _core_words(key: str) -> list[str]:
    """The words of a name_key without filler like 'services' or 'group'."""
    words = key.split()
    return [w for w in words if w not in FILLER_WORDS] or words


def _is_chain(words: list[str]) -> bool:
    text = f" {' '.join(words)} "
    return any(f" {chain} " in text for chain in CHAIN_NAMES)


def _unusual(word: str, places: set[str]) -> bool:
    """A word that helps tell one business from another (not 'dental', 'north', 'dds' or the town)."""
    return not (word in GENERIC_WORDS or word in TITLE_WORDS or word in DIRECTION_WORDS
                or word in places or word.isdigit())


def _distinctive(words: list[str], places: set[str]) -> bool:
    """Is this name unusual enough to trust without a matching street address? Two or more
    words, MIN_NAME_LETTERS letters, one unusual word, and not a chain."""
    return (len(words) >= 2 and len("".join(words)) >= MIN_NAME_LETTERS
            and any(_unusual(w, places) for w in words) and not _is_chain(words))


def _same_word(a: str, b: str) -> bool:
    """'plumbers' = 'plumbing' (the same first STEM_LETTERS letters), 'joes' = 'joes'."""
    if a == b:
        return True
    return len(a) >= STEM_LETTERS and len(b) >= STEM_LETTERS and a[:STEM_LETTERS] == b[:STEM_LETTERS]


def _name_inside(company_words: list[str], loan_words: list[str], places: set[str]) -> bool:
    """Every word of the company's name is in the loan's name, at least one of them is an
    unusual word, and the loan doesn't add a direction ('NORTH') the company doesn't have."""
    if not all(any(_same_word(c, w) for w in loan_words) for c in company_words):
        return False
    if not any(_unusual(c, places) for c in company_words):
        return False
    extra = set(loan_words) - set(company_words)
    return not (extra & DIRECTION_WORDS)


def _judge(company: dict, loan: dict) -> tuple[float, str, float, str] | None:
    """(score, reason, name similarity, confidence) when this loan is the company's, else None.
    company: key, words, number, street, unit, places (see _match). loan: a loans row."""
    loan_number, loan_street, loan_unit = address_parts(loan["address"])
    number, street, unit = company["number"], company["street"], company["unit"]
    places = company["places"] | set(name_key(loan["city"]).split())
    keys = _name_keys(loan["name"], loan["name_key"])

    both_numbers = bool(number and loan_number)
    units_differ = bool(unit and loan_unit and unit != loan_unit)  # another tenant of the building
    same_street = (both_numbers and number == loan_number and bool(street) and street == loan_street
                   and not units_differ)
    same_street_name = both_numbers and bool(street) and street == loan_street
    # Every store of a chain or franchise has the same name: only the street address can tell.
    chain = bool(loan["franchise"]) or _is_chain(company["words"]) or any(_is_chain(k.split()) for k in keys)

    best = None
    for key in keys:
        loan_words = _core_words(key)
        exact = loan_words == company["words"]
        # SequenceMatcher is slow: only run it when it can change the verdict (at the same street
        # address; elsewhere only exactly the same words count)
        ratio = 1.0 if exact else 0.0
        if same_street and not exact:
            ratio = SequenceMatcher(None, " ".join(company["words"]), " ".join(loan_words)).ratio()
        verdict = None
        if same_street and (exact or ratio >= SAME_NAME_RATIO):
            verdict = (1.05, "same name, street address and ZIP", ratio, "high")
        elif same_street and not chain and _name_inside(company["words"], loan_words, places):
            verdict = (0.95, "same street address and similar name", ratio, "medium")
        elif exact and not chain and _distinctive(company["words"], places):
            if not both_numbers:  # one side has no street address: the name and ZIP decide
                verdict = (1.0, "same name and ZIP", ratio, "medium")
            elif same_street_name:  # moved along the street, or to another suite
                verdict = (0.9, "same name and ZIP (different house number or suite)", ratio, "medium")
        if verdict and (best is None or verdict[0] > best[0]):
            best = verdict
    return best


def _could_match(company_words: set[str], company_key: str, number, loan_key: str, loan_address) -> bool:
    """A quick check before the slower comparison (a busy ZIP has thousands of loans):
    a word in common, the same house number, or the same letters ("smiledental")."""
    if not company_words.isdisjoint(loan_key.split()):
        return True
    if number and loan_address:
        found = LEADING_NUMBER_RE.match(loan_address)
        if found and found.group(1).lstrip("0") == number:
            return True
    return company_key.replace(" ", "") == loan_key.replace(" ", "")


def _connect_ro() -> sqlite3.Connection:
    """Open the PPP database read-only (never creates the file)."""
    conn = sqlite3.connect(config.PPP_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _available(conn: sqlite3.Connection) -> bool:
    try:
        finished = conn.execute("SELECT 1 FROM files WHERE finished_at IS NOT NULL LIMIT 1").fetchone()
        # A file that failed halfway, or is being imported again, has only some of its loans:
        # "no loan found" would then be wrong, and saved on the company for good.
        unfinished = conn.execute("SELECT 1 FROM files WHERE finished_at IS NULL LIMIT 1").fetchone()
        index = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'loans_zip'").fetchone()
        loans = conn.execute("SELECT 1 FROM loans LIMIT 1").fetchone()
    except sqlite3.Error:
        return False
    return bool(finished and not unfinished and index and loans)


def available() -> bool:
    """True when the PPP database exists, has loans, and every file in it has finished
    importing (no download running or broken off halfway)."""
    if not config.PPP_DB_PATH.exists():
        return False
    try:
        conn = _connect_ro()
    except sqlite3.Error:
        return False
    try:
        return _available(conn)
    finally:
        conn.close()


def _match(conn: sqlite3.Connection, company) -> dict | None:
    state, zip5 = us_location(company)
    company_key = name_key(company.name)
    if not zip5 or not company_key:
        return None
    number, street, unit = _company_address(company)
    info = {
        "key": company_key, "words": _core_words(company_key), "number": number, "street": street,
        "unit": unit, "places": set(name_key(company.city).split()),
    }
    words = set(company_key.split())

    # Score each loan in the ZIP. One borrower = the same name_key at the same house number
    # (e.g. its first and second draw); it keeps its best score. Two stores with one name
    # are two borrowers.
    best: dict[tuple, tuple[float, str, float, str]] = {}
    judged = {}  # verdicts of loans already compared (draws of one borrower look alike)
    for key, name, address, city, loan_state, franchise in conn.execute(
        "SELECT name_key, name, address, city, state, franchise FROM loans WHERE zip5 = ?", (zip5,)
    ):
        if state and loan_state and loan_state != state:
            continue
        if not _could_match(words, company_key, number, key, address):
            continue
        # A DBA name is split into parts, so then the raw name matters, not only its key.
        seen_as = (name if DBA_RE.search(name or "") else key, address, city, bool(franchise))
        if seen_as not in judged:
            loan = {"name": name, "name_key": key, "address": address, "city": city, "franchise": franchise}
            judged[seen_as] = _judge(info, loan)
        verdict = judged[seen_as]
        if not verdict:
            continue
        borrower_id = (key, address_parts(address)[0])
        if verdict[0] > best.get(borrower_id, (0.0,))[0]:
            best[borrower_id] = verdict
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    if len(ranked) > 1 and abs(ranked[0][1][0] - ranked[1][1][0]) < 1e-9:
        return None  # two different borrowers fit equally well: don't guess

    (key, loan_number), (_score, reason, sim, confidence) = ranked[0]
    borrower = [
        dict(row) for row in conn.execute(f"SELECT {LOAN_COLUMNS} FROM loans WHERE zip5 = ? AND name_key = ?", (zip5, key))
        if not (state and row["state"] and row["state"] != state) and address_parts(row["address"])[0] == loan_number
    ]
    # The latest loan that has a headcount (the second draw is usually the more recent figure).
    latest = max(borrower, key=lambda r: (r["jobs"] is not None, r["date_approved"] or ""))
    return {
        "jobs": latest["jobs"],
        "date_approved": latest["date_approved"],
        "name": latest["name"],
        "address": latest["address"],
        "city": latest["city"],
        "state": latest["state"],
        "zip": latest["zip5"],
        "naics": latest["naics"],
        "business_type": latest["business_type"],
        "amount": latest["amount"],
        "loan_number": latest["loan_number"],
        "draw": latest["draw"],
        "loans": len(borrower),
        "match": reason,
        "similarity": round(min(sim, 1.0), 2),
        # "high" = the same name at the same street address; "medium" = any other accepted match
        "confidence": confidence,
    }


def match_company(company) -> dict | None:
    """The company's PPP loan (see the module docstring for the rules), or None.
    Only looks; never changes the company."""
    if not available():
        return None
    conn = _connect_ro()
    try:
        return _match(conn, company)
    finally:
        conn.close()


def _check(conn: sqlite3.Connection, company) -> bool | None:
    """Look the company up and save the result on it. True = found, False = not found,
    None = not a US company or no ZIP: then company.ppp = NOT_APPLICABLE, also with the time
    we looked, so it isn't looked at again on every start."""
    company.ppp_checked_at = datetime.now(timezone.utc)
    if not us_location(company)[1]:
        company.ppp = dict(NOT_APPLICABLE)
        return None
    found = _match(conn, company)
    company.ppp = found or {}
    return found is not None


def apply_ppp(company) -> bool:
    """Save the company's PPP loan on it (company.ppp: the loan, {} when none, NOT_APPLICABLE
    outside the US / without a ZIP) and when we looked. Returns True when a loan was found.
    Does nothing without the PPP database."""
    if not available():
        return False
    conn = _connect_ro()
    try:
        return bool(_check(conn, company))
    finally:
        conn.close()


MATCH_BATCH = 200  # companies per leads.db commit


def match_saved_companies(session: Session, only_unchecked: bool = True,
                          limit: int | None = None) -> tuple[int, int]:
    """Look up saved companies in the PPP data and rescore the ones whose result changed.
    only_unchecked=False looks up every company again (after a new download).
    limit: look at most this many companies in this call (the worker does a few at a time
    between jobs and calls again later); None = all of them.
    Returns (US companies checked, companies with a loan found). Companies outside the US or
    without a ZIP are marked NOT_APPLICABLE but not counted."""
    from .scoring import rescore  # here, not at the top: scoring may import modules that import us

    if not available():
        return 0, 0
    checked = matched = done = 0
    conn = _connect_ro()
    try:
        last_id = 0
        while limit is None or done < limit:
            size = MATCH_BATCH if limit is None else min(MATCH_BATCH, limit - done)
            # First only a few small columns: companies in another country are marked right
            # away, without loading their (big) source data.
            light = select(Company.id, Company.country, Company.ppp).where(Company.id > last_id)
            if only_unchecked:
                light = light.where(Company.ppp_checked_at.is_(None))
            rows = session.execute(light.order_by(Company.id).limit(size)).all()
            if not rows:
                break
            done += len(rows)
            last_id = rows[-1].id
            abroad, to_check = [], []
            for row in rows:
                old = row.ppp if isinstance(row.ppp, dict) else {}
                if country_code(row.country) not in (None, "US") and not old.get("jobs"):
                    abroad.append(row.id)  # nothing to rescore: no PPP figure before or after
                else:
                    to_check.append(row.id)
            if abroad:
                session.execute(
                    update(Company).where(Company.id.in_(abroad))
                    .values(ppp=dict(NOT_APPLICABLE), ppp_checked_at=datetime.now(timezone.utc))
                )
            companies = session.scalars(
                select(Company).options(selectinload(Company.contacts))  # rescore reads the people
                .where(Company.id.in_(to_check)).order_by(Company.id)
            ) if to_check else []
            for company in companies:
                before = dict(company.ppp or {})
                found = _check(conn, company)
                if found is not None:
                    checked += 1
                    matched += found
                # Rescore when the PPP figure may have changed (not for "not applicable" when
                # there was no loan before either).
                if company.ppp != before and (found is not None or before.get("jobs")):
                    rescore(company)
            session.commit()
    finally:
        conn.close()
    return checked, matched


# ----- command line ----------------------------------------------------------------------


def stats() -> None:
    """`python -m app.ppp stats`: what the PPP database holds."""
    if not config.PPP_DB_PATH.exists():
        print(f"No PPP database yet ({config.PPP_DB_PATH}).")
        print("Download it with:  python -m app.ppp download   (or --states TX,CA for some states only)")
        return
    conn = None
    try:
        conn = _connect_ro()
        files = conn.execute("SELECT name, states, rows, finished_at FROM files ORDER BY id").fetchall()
        for f in files:
            status = f"{f['rows']:,} loans, finished {f['finished_at']}" if f["finished_at"] else "NOT finished"
            print(f"  {f['name']}: {status}" + (f" (states {f['states']})" if f["states"] else ""))
        done = sum(f["rows"] or 0 for f in files if f["finished_at"])
        print(f"{done:,} loans from {sum(1 for f in files if f['finished_at'])} of {len(files)} file(s).")
        size_mb = config.PPP_DB_PATH.stat().st_size / 1e6
        print(f"Database: {config.PPP_DB_PATH} ({size_mb:,.0f} MB). Ready for matching: {'yes' if _available(conn) else 'no'}")
        if any(not f["finished_at"] for f in files):
            print("Matching is paused until every file has finished: run the download (or import) again.")
    except sqlite3.Error as e:
        print(f"The PPP database can't be read ({e}). Delete it and download again.")
    finally:
        if conn:
            conn.close()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.ppp", description="US PPP loan data (company size).")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("download", help="download the PPP files into the PPP database")
    p.add_argument("--states", help="only keep these states, e.g. TX,CA")
    p.add_argument("--force", action="store_true", help="import finished files again")
    p = sub.add_parser("import", help="import PPP CSV files you already downloaded")
    p.add_argument("files", nargs="+")
    p.add_argument("--states", help="only keep these states, e.g. TX,CA")
    p.add_argument("--force", action="store_true", help="import finished files again")
    p = sub.add_parser("match", help="look up saved companies in the PPP data")
    p.add_argument("--all", action="store_true", help="also companies checked before")
    sub.add_parser("stats", help="what is in the PPP database")
    args = parser.parse_args(argv)

    if args.command == "download":
        started = time.time()
        saved = download(_parse_states(args.states), force=args.force)
        print(f"\nDone: {saved:,} loans saved in {(time.time() - started) / 60:.0f} minutes.")
        print("Now look up your saved companies:  python -m app.ppp match --all")
    elif args.command == "import":
        saved = import_files(args.files, _parse_states(args.states), force=args.force)
        print(f"\nDone: {saved:,} loans saved.")
    elif args.command == "match":
        from .db import SessionLocal, init_db

        if not available():
            print("No PPP data yet. Download it first:  python -m app.ppp download")
            return
        init_db()
        with SessionLocal() as session:
            checked, matched = match_saved_companies(session, only_unchecked=not args.all)
        print(f"Checked {checked} US companies: {matched} found in the PPP data.")
    else:
        stats()


if __name__ == "__main__":
    main(sys.argv[1:])
