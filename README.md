# LeadGen

Internal lead generation tool. It finds local businesses (OpenStreetMap or Google Maps),
crawls their websites for emails, phones and team members, guesses and verifies people's work
emails, and lets you sort the best leads into lists and export them.

## Run it

1. First time only: double-click **`setup.bat`** (installs Python and Node packages), then
   **`create-user.bat`** to make your admin login (you type the password; it is never shown).
2. Every time: double-click **`start-all.bat`**. It opens the dashboard at http://localhost:3000
   and asks you to log in.

Or start the parts separately with `start-backend.bat` and `start-frontend.bat`.
API documentation (auto-generated) is at http://localhost:8000/docs (log in to the dashboard
first; the login works there too).

Settings and API keys live in **`backend/.env`**. Create it once by copying
`backend/.env.example` to `backend/.env`, then fill in what you need. Real keys go **only** in
`.env` (it is never committed to git), never in `.env.example`. After changing `.env`, restart
the backend (close its window and run `start-all.bat` again).

The dashboard has three pages (top bar):

- **Search**: find businesses, see their emails and website facts, open a business for details.
- **People**: find the owners / managers of businesses by job title, with verified emails.
- **Lists**: your saved lists of businesses and people.

## Project layout

```
backend/                   Python + FastAPI
  app/main.py              API entry point (login check, request-forgery check, security headers)
  app/auth.py              Passwords, sessions, lockouts;  app/create_user.py: make a login
  app/models.py            Database tables (companies, contacts, jobs, lists, email checks...)
  app/worker.py            Runs searches, website crawls and email checks in the background
  app/sources/             Business sources: osm.py (OpenStreetMap), google_places.py
  app/enrich/              Website crawler (emails, phones, socials, team members)
  app/emails/              Email checker (format, domain, mailbox) and email-pattern finder
  app/providers/hunter.py  Optional Hunter.io backup
  app/people/              Job title -> seniority / decision-maker
  app/scoring.py           Lead score (0-100) and its reasons
  app/phones.py            Phone numbers: standard format, mobile/landline, impossible numbers
  app/dedupe.py            Merges the same business found twice (OSM + Google, or twice in OSM)
  app/sizing.py            Company size estimate (Solo/Small/Medium/Large) and its reasons
  app/ppp.py               US PPP loan data: download, import, match (headcount reported 2020-21)
  app/intent.py            Buying intent per service (Website / SEO / Software / Marketing)
  app/reviews.py           Complaints in Google reviews;  app/pagespeed.py: Google PageSpeed checks
  app/exporting.py         CSV / Excel files;  app/sheets.py: Google Sheets export
  app/routers/             API endpoints
  data/leads.db            SQLite database (created automatically)
frontend/                  Next.js dashboard
  src/app/                 Pages: / (Search), /people, /lists, /login, /account, /users
  src/proxy.ts             Sends visitors without a login to /login
  src/components/          Search form, job list, results table, company panel, lists...
  src/lib/api.ts           Talks to the backend
```

## Roadmap

- [x] **Step 1:** Setup + local search (OpenStreetMap), results table, filters, CSV export
- [x] **Step 2:** Google Places source (better coverage in Pakistan/India)
- [x] **Step 3:** Website crawler: emails, phones, socials, team members
- [x] **Step 4:** Email pattern finder + SMTP verification
- [x] **Step 5:** B2B people/company search + "Find decision-makers"
- [x] **Step 6:** Lists, statuses, scoring, Google Sheets export
- [x] Logins, users and security hardening
- [ ] Later: outreach sequences, then SaaS (teams, billing)

## Logins & security

Everyone needs a login. The first admin is made with **`create-user.bat`** (on the server:
`python -m app.create_user`); after that, admins add people on the **Users** page (top bar).
Members use the app; admins can also add users, set new passwords and deactivate users.
Users are deactivated, never deleted. Click your name (top right) to change your password.

What protects the app:

- Passwords are stored as **argon2id** hashes, never as text. At least 12 characters.
- **5 wrong passwords lock the account for 15 minutes**; one address trying many accounts is
  slowed down too. A wrong email and a wrong password give the same message.
- A login lasts until 12 hours of inactivity (7 days at most). Changing a password, a new
  password set by an admin, or deactivation logs that user out everywhere.
- The login cookie is **HttpOnly** (page scripts can't read it), **SameSite**, and https-only on
  the server. The database stores only a hash of it.
- Every `/api` endpoint except login and health needs a login (a test checks new endpoints too).
  Changes must come from the dashboard itself, which blocks cross-site request forgery.
- Browser security headers (Content-Security-Policy, no framing, no sniffing), and the website
  crawler refuses private/internal addresses.

On the server, set in `backend/.env`: `COOKIE_SECURE=1`,
`FRONTEND_ORIGINS=https://your-domain`, `API_DOCS=0`.

Forgot the only admin password? On the computer running the backend, run `create-user.bat`
(or `python -m app.create_user`) with the same email: it sets a new password.

## Google Maps setup

OpenStreetMap is free and needs no key. Google Maps usually finds more businesses (and gives
ratings), but needs an API key:

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create a project
   (top bar → project picker → **New project**).
2. Google asks for a billing account even for the free usage. Add one under **Billing**.
3. **APIs & Services → Library**, search for **Places API (New)** and click **Enable**.
   (Not the old "Places API".)
4. **APIs & Services → Credentials → Create credentials → API key**.
5. Click the new key → **API restrictions → Restrict key** → tick only **Places API (New)** →
   Save. A restricted key is useless to anyone who sees it by accident.
6. Put it in `backend/.env` (**never** in `.env.example`):

   ```
   GOOGLE_PLACES_API_KEY=your-key-here
   ```

7. Restart the backend. "Google Maps" can now be picked in the search form.

`GOOGLE_PLACES_MONTHLY_LIMIT` (default 900) caps the requests per calendar month so you stay
inside Google's free allowance. The search form shows how many you have used. When the cap is
reached, a search keeps the businesses found so far and says so.

## Finding & verifying emails

Tick **"Find & verify emails"** in the search form (or click **Verify emails** on a finished
search, or on a business in its details panel). For each business:

1. **Website crawl**: up to 6 pages (home, contact, team, about...) are read for emails, phone
   numbers, social links and team members with their job titles.
2. **Pattern guess**: when we know how the company writes its addresses (e.g.
   `john.smith@acme.com` means the pattern is `{first}.{last}`), we guess the address of each
   person whose email we don't have yet.
3. **Mailbox check (SMTP)**: we ask the company's mail server whether each address exists.
   **No email is ever sent**: the conversation stops before any message would be delivered.

What each status means:

| Status | Plain English |
|---|---|
| **Verified** (valid) | The mail server confirmed this mailbox exists. Safe to email. |
| **Catch-all** | The server says "yes" to every address, even made-up ones, so we can't confirm this one. Usually fine for addresses listed on the website; less sure for guesses. |
| **Risky** | A throw-away (disposable) address, a Yahoo/AOL address (they can't be checked), or Microsoft 365 refused it in a way that usually means "no such mailbox". Double-check before using. |
| **Invalid** | The address can't work: bad format, the domain doesn't exist or has no mail server, or the server said the mailbox doesn't exist. Don't use it. |
| **Unknown** | We couldn't decide: mailbox checking is off, port 25 is blocked, the server said "try later" or refused to talk to us. Try again later. |

People emails also get a **confidence** (0-100%) and a **source**: *website* (listed on their
own site), *pattern* (our guess) or *hunter* (Hunter.io).

**Port 25 note.** Mailbox checks need outgoing port 25. Many home internet providers and
almost all cloud servers block it. Then every mailbox check ends as "unknown" (the format and
domain checks still work). After 3 mail servers in a row can't be reached, checks pause for 15
minutes. If port 25 is blocked for you, set `SMTP_VERIFY=0` in `backend/.env`. Setting
`SMTP_HELO_HOST` (and `SMTP_MAIL_FROM`) to a domain you own makes mail servers more willing to
answer. Results are remembered for 30 days (`EMAIL_CHECK_MAX_AGE_DAYS`).

**Hunter.io (optional).** Add `HUNTER_API_KEY=...` to `backend/.env` (free plan: 25 searches a
month, capped by `HUNTER_MONTHLY_LIMIT`). Hunter is only a backup: it is asked only about
businesses where we found no email ourselves (or whose website couldn't be read), and never
about the same domain twice within 30 days. The search form shows the monthly meter once a key
is set.

## People search & decision-makers

The **People** page finds people instead of businesses:

1. Pick job titles (quick picks like Owner, Founder, CEO, Manager, or type your own), a business
   type and a location, then **Find people**.
2. LeadGen searches the businesses, crawls their websites and finds + verifies the emails of
   the people working there. The three steps show live progress.
3. The table lists the people, most senior first. Filter by job title, seniority, decision-makers
   only, email status, lead status, city, category or minimum score.

Every job title is sorted into a seniority level: **Owner / Founder**, **C-level**,
**Director**, **Manager**, **Professional** (dentist, lawyer, engineer...) or **Staff**.
Owners, C-level, directors and most managers count as **decision-makers** (marked with a star).
An "Assistant to the CEO" is staff, not C-level.

On the **Search** page, click a business name to open its details panel. **Find
decision-makers** crawls the website (if needed) and finds the emails of its owners and
managers.

## Lists, statuses & scores

- **Lists**: tick rows (businesses or people) and use **Add to list**. The **Lists** page shows
  each list with Businesses and People tabs. Deleting a list never deletes the leads.
- **Lead status**: New, Contacted, Replied, Interested, Won, Lost, Not interested. Change it in
  the table, in bulk (tick rows → Set status) or in the details panel. Every change is written
  to the business's timeline, where you can also add notes.
- **Score (0-100)**: how promising a lead is. Hover the score to see why, e.g.
  "Verified email +25", "Phone +10", "Decision-maker with email +20", "No SSL (http only) +8".

  Businesses: verified email +25 (catch-all +15, unchecked +8), phone +10, decision-maker with
  email +20 (without +8), website crawled +5, rating 4.5+ with 50+ reviews +10 (4.0+ +5),
  website-sales opportunities (no website +10, no SSL +8, outdated copyright +5, no contact
  form +3), closed business -30.

  People: verified email +40 (catch-all +25, unchecked +10), decision-maker +30, owner/C-level
  +15 (director +10, manager +5), phone +5, LinkedIn +5, plus a tenth of their business's score.

  **Lost** and **Not interested** leads always score 0. The weights are at the top of
  `backend/app/scoring.py`. After changing them, open http://localhost:8000/docs and run
  `POST /api/scores/recompute`. Leads saved before scoring existed are scored automatically
  when the backend starts.

## Export

Every table has an **Export ▾** button. It exports what the table shows (same filters, and for
a list only that list):

- **CSV**: opens in Excel with accents intact (UTF-8).
- **Excel (.xlsx)**.
- **Google Sheets**: adds a new tab to a Google Sheet you choose (needs the one-time setup below).

### Google Sheets export

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project (or reuse
   one) and enable the **Google Sheets API** (APIs & Services → Library).
2. **IAM & Admin → Service accounts → Create service account** (any name, no roles needed).
3. Open the service account → **Keys → Add key → Create new key → JSON**. A key file downloads.
   Keep it private: it works like a password.
4. Put the file in the `backend` folder and add to `backend/.env`:

   ```
   GOOGLE_SHEETS_CREDENTIALS_FILE=your-key-file.json
   ```

   (A full path such as `C:\keys\leadgen.json` works too.)
5. Restart the backend.
6. Open your Google Sheet → **Share** → add the service account's email (shown in the Export
   dialog, ends with `iam.gserviceaccount.com`) as **Editor**.
7. In LeadGen: **Export ▾ → Google Sheets…**, paste the sheet's link, optionally name the tab.

If something is wrong, the dialog says what (sheet not shared, Sheets API not enabled, wrong
link...).

## Troubleshooting

- **A change (or a new `.env` value) has no effect / the backend didn't reload.** On Windows the
  automatic reload sometimes misses changes. Close the backend and frontend windows and run
  `start-all.bat` again.
- **"Request failed" or "Could not reach the backend" in the dashboard.** The backend window is
  closed or showed an error. Run `start-all.bat` again and read the backend window.
- **Every mailbox check says "unknown".** Port 25 is probably blocked on your network (see the
  port 25 note above). Format and domain checks still work.
- **Google Maps is greyed out.** `GOOGLE_PLACES_API_KEY` is missing in `backend/.env`, or the
  backend wasn't restarted after adding it.
- **A website shows "Blocked (HTTP 403)".** The site uses bot protection (e.g. Cloudflare). LeadGen
  doesn't try to get around it. Hunter.io (if set up) is asked instead.
- **After moving to a new computer**: run `setup.bat` once, and copy `backend/.env` and
  `backend/data/leads.db` (your data) over by hand.

## Adding a new lead source

Create `backend/app/sources/<name>.py` with a class extending `Source` (see `base.py`),
implement `search()`, and register it in `backend/app/sources/__init__.py`.

## Duplicate businesses

The same business is often listed by both OpenStreetMap and Google. New search results are
merged automatically with a business you already have when they share a phone number and a
similar name, the same website at the same spot, or the same name at the same spot. The merge
keeps your status, notes and lists, and writes a "Merged" entry in the business's timeline.

To clean up businesses saved before this existed, double-click **`merge-duplicates.bat`**:
it lists the duplicates, asks before merging, and saves a backup copy of the database first.
On the server: `cd backend && .venv/bin/python -m app.dedupe` to preview, then add `--apply`
(back up `data/leads.db` first).

## Company size

Every business gets a size estimate: **Solo (1)**, **Small (2-10)**, **Medium (11-50)** or
**Large (51+)**, with the reasons shown (hover the badge, or open the business). It uses:

- what the website says ("our team of 25", "founded in 1998", "3 locations", a careers page),
- people listed on the website (a minimum), branches (other saved listings with the same website),
  and Google review counts (a weak hint),
- for US businesses, the **PPP loan data** (US government, 2020-21): the number of jobs the
  business reported when it got its loan. It is self-reported and a few years old, so it is a
  strong hint, not today's exact headcount.

No information means "Unknown", never "Solo". Businesses crawled before this feature get their
website size facts on their next crawl (done automatically).

**PPP data (optional, US only).** It is about 5 GB to download and becomes a ~2 GB file
`backend/data/ppp.db` (kept apart from `leads.db`, so backups stay small). Download it once:

    cd backend
    .venv\Scripts\python -m app.ppp download            (Windows)
    sudo -u leadgen .venv/bin/python -m app.ppp download  (server, in /opt/leadgen/app/backend)

It streams the files straight into the database (no 5 GB of CSV files on disk) and can be run
again to resume. `python -m app.ppp stats` shows what is there. While the app runs, it then looks
up your US leads a few hundred at a time when it isn't busy.

## Buying intent ("Looking for")

Every business gets an **intent score (0-100) per service you sell**: Website, SEO, Software and
Marketing, with the reasons shown (like the lead score). 60+ is marked **Hot**. In the Leads table,
pick what you are selling in **Looking for**, tick **Hot only**, or sort by **Intent**.

The scores come from:

- the website crawl: SSL, outdated or DIY site, mobile-friendly, page title/description, Google
  business info (schema), Google Analytics, ad pixels (Google/Facebook/TikTok...), online
  booking / ordering / shop / payment, live chat, PDF forms, job titles on the careers page,
- Google Maps: review count and rating, photos, position in the search, up to 5 reviews
  (complaints like "hard to book" or "nobody answers the phone"), "possibly a new business",
- Google PageSpeed: mobile speed and SEO score (one website at a time while the app is idle),
- company size (bigger businesses need more software).

Precision first: something only counts as "missing" when the crawler could really see it. Sites
that load widgets with JavaScript or Google Tag Manager show "can't tell" instead. Leads crawled
before this feature get the new website facts when they are crawled again.

Settings in `backend/.env` (see `.env.example`): `GOOGLE_PLACES_REVIEWS=1` (reviews + photos;
Google bills $40 instead of $35 per 1,000 searches, the first 1,000 a month are free either way)
and `PAGESPEED_API_KEY=` (free key, 25,000 tests a day; without one Google mostly refuses).

## Find owners (LinkedIn search + Apollo)

**Find owner** (in a business's panel) or **Find owners** (for the ticked rows) looks up the owner /
decision-maker of a business, then finds and checks their email with the normal email finder.
We never scrape LinkedIn (its terms forbid it, accounts get banned):

1. **Web search** for LinkedIn profiles, e.g. `site:linkedin.com/in "owner" "Smile Dental" Austin`.
   A person is only kept when the result clearly names this business as their current employer
   and their title is a decision-maker (owner, founder, president, CEO, partner...).
   Needs a key in `backend/.env`: `BRAVE_API_KEY` (Brave Search API, about $5 of free credit a
   month = ~1,000 searches) or `SERPER_API_KEY` with `WEB_SEARCH_PROVIDER=serper` (Serper.dev,
   2,500 free searches once). Capped by `WEB_SEARCH_MONTHLY_LIMIT` (900). One search per business.
2. **Apollo.io (optional backup)** when the search finds no one: `APOLLO_API_KEY` from your Apollo
   account. Finding people is free; getting a person's name, email and LinkedIn costs 1 credit
   (max 2 people per business, `APOLLO_MONTHLY_LIMIT` credits a month). Phone numbers are never
   requested (8 credits each).

A business is not looked up again for 90 days (`PEOPLE_RECHECK_DAYS`). Every person and business
also has a **Find on LinkedIn** link, which opens LinkedIn's own search in your browser.
