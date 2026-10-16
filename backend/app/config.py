"""App settings. Put secrets (API keys) in backend/.env (see .env.example).
Any value can also be overridden with an environment variable of the same name."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# SQLite for now; later switch to Postgres (Supabase) by changing this one value.
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'leads.db'}")

# Address(es) of the dashboard. Requests that change data from any other website are refused.
# On the server: FRONTEND_ORIGINS=https://leadgen.yourdomain.com
FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "http://localhost:3000").split(",")]

# --- Login & security ---
# 1 when the site is served over https (the server). The login cookie is then sent over https only.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "7"))  # log in again at least this often
SESSION_IDLE_HOURS = int(os.getenv("SESSION_IDLE_HOURS", "12"))  # logged out after this long unused
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))  # wrong passwords before the account locks
LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "15"))
LOGIN_IP_MAX_FAILURES = int(os.getenv("LOGIN_IP_MAX_FAILURES", "20"))  # per address, per LOGIN_LOCK_MINUTES
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))
# The API documentation pages (/docs). Handy on your own computer; set API_DOCS=0 on the server.
API_DOCS = os.getenv("API_DOCS", "1") == "1"

# OpenStreetMap services ask every app to identify itself.
# Optionally set CONTACT_EMAIL so they can reach you if something goes wrong.
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")
USER_AGENT = f"LeadGenSoft/0.1 (internal tool{'; ' + CONTACT_EMAIL if CONTACT_EMAIL else ''})"

NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
# Free public Overpass servers get overloaded; we try each in turn and retry.
OVERPASS_URLS = os.getenv(
    "OVERPASS_URLS",
    "https://overpass-api.de/api/interpreter,"
    "https://overpass.kumi.systems/api/interpreter,"
    "https://overpass.private.coffee/api/interpreter,"
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
).split(",")
OVERPASS_ROUNDS = int(os.getenv("OVERPASS_ROUNDS", "3"))

# --- Google Places (Step 2) ---
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
# Hard cap on Places API requests per calendar month, to stay inside Google's free usage.
GOOGLE_PLACES_MONTHLY_LIMIT = int(os.getenv("GOOGLE_PLACES_MONTHLY_LIMIT", "900"))

# --- Website crawler (Step 3) ---
CRAWL_CONCURRENCY = int(os.getenv("CRAWL_CONCURRENCY", "5"))  # websites crawled at the same time
CRAWL_MAX_PAGES = int(os.getenv("CRAWL_MAX_PAGES", "6"))  # pages per website
CRAWL_TIMEOUT = float(os.getenv("CRAWL_TIMEOUT", "15"))  # seconds per page
# --- Email verification (Step 4) ---
# Ask the mail server (SMTP, port 25) whether an address exists. Free, but needs port 25 open
# (most cloud servers block it). Set SMTP_VERIFY=0 to use only the format + domain (MX) checks.
SMTP_VERIFY = os.getenv("SMTP_VERIFY", "1") == "1"
SMTP_HELO_HOST = os.getenv("SMTP_HELO_HOST", "").strip()  # empty = this computer's name
SMTP_MAIL_FROM = os.getenv("SMTP_MAIL_FROM", "").strip()  # empty = verify@<SMTP_HELO_HOST>
SMTP_TIMEOUT = float(os.getenv("SMTP_TIMEOUT", "10"))
EMAIL_CHECK_MAX_AGE_DAYS = int(os.getenv("EMAIL_CHECK_MAX_AGE_DAYS", "30"))  # re-check older results

# Hunter.io (optional backup for finding emails; free plan = 25 searches/month).
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "").strip()
HUNTER_MONTHLY_LIMIT = int(os.getenv("HUNTER_MONTHLY_LIMIT", "25"))

# --- Google Sheets export (Step 6, optional) ---
# Path to a Google service-account JSON key file. Share your sheet with that account's email.
GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "").strip()

# --- US PPP loan data (company size, optional) ---
# A separate database made by "python -m app.ppp download" (several GB, so it is kept out of
# leads.db and its daily backup). When the file doesn't exist, PPP matching is simply skipped.
PPP_DB_PATH = Path(os.getenv("PPP_DB_PATH", str(DATA_DIR / "ppp.db")))

# Browser-like user agent: many small-business sites block obvious bots.
CRAWL_USER_AGENT = os.getenv(
    "CRAWL_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 LeadGenSoft/0.1",
)
