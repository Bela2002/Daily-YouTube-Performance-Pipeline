# Daily YouTube Performance Pipeline

A daily ELT pipeline that collects YouTube publishing and performance data from the
**YouTube Data API v3** with **Python**, stores it in **PostgreSQL**, and feeds a
**Looker Studio** dashboard — a single source of truth for what was published and what is
performing best over the last 24 hours, 7 days and 28 days. Supports multiple channels.

```mermaid
flowchart LR
    A[GitHub Actions<br/>daily 06:00 SLT] --> B[Python: Extract<br/>YouTube Data API v3]
    B --> C[(raw.api_response<br/>JSONB, untouched)]
    C -->|SQL transform| D[(core tables<br/>channel · video · playlist<br/>video_daily_snapshot)]
    D --> E[(reporting views)]
    E --> F[Looker Studio<br/>dashboard]
```

## Why ELT (and three layers)

| Layer | What | Why |
|---|---|---|
| `raw` | every API response page stored as JSONB | full audit trail; transforms can be re-run with **zero API quota** (`retransform`) |
| `core` | typed, de-duplicated tables built in SQL with upserts | idempotent: re-running a day never duplicates rows |
| `reporting` | views shaped for the dashboard | time windows, ranks and playlist logic live in version-controlled SQL, not hidden in the BI tool |

### Data model

* `core.channel`, `core.video`, `core.playlist`, `core.video_playlist` (bridge — a video can be in many playlists)
* `core.video_daily_snapshot` — **one row per video per capture day** (views, likes, comments)
* `core.pipeline_run` — log of every run (status, quota used, errors)

`reporting.video_daily_dataset` contains every field required by the brief:
video ID, title, channel name, channel ID, playlist ID, playlist name, publish date/time,
duration, like/view/comment counts, data capture date and capture timestamp (UTC) — plus
`views_gained_since_prev_capture`, `engagement_rate`, Sri Lanka publish time and a video URL.

### How a daily run works

1. `channels.list` → channel name and its hidden *uploads* playlist (IDs or `@handles`).
2. `playlistItems.list` on the uploads playlist, newest first, **stopping as soon as a page is
   older than 28 days** (cheap on quota).
3. `videos.list` in batches of 50 → metadata + statistics.
4. `playlists.list` + `playlistItems.list` → which playlist each video belongs to.
5. Raw pages are inserted into `raw.api_response`, then `sql/transform.sql` upserts the core
   tables — **in the same transaction** as marking the run successful.

Every call is a `list` call (1 quota unit). A typical channel costs ~5–60 units per run (a very busy news channel a few hundred) out
of the default 10,000/day.

## Reliability

| Failure | What happens |
|---|---|
| Network error, HTTP 429/5xx, 403 rate-limit, invalid JSON | retried with exponential backoff (honours `Retry-After`), up to 4 retries |
| Daily quota exceeded | run stops immediately, nothing saved, status `failed`, exit code 3 — re-run tomorrow |
| One channel broken (bad ID, API error) | other channels are still saved; run status `partial`, exit code 4, reason recorded |
| Channel with no uploads / deleted playlist | treated as "no videos" / warning — not a failure |
| Database unreachable (or serverless DB waking up) | 3 connection attempts, then a clean error message, exit code 1 |
| Crash in the middle of a run | load + transform + status are one transaction, so no half-written day |
| Two runs at the same time | a PostgreSQL advisory lock refuses the second run |
| Ctrl+C, or the process is killed / times out | logged as `failed`; a killed run is closed as "abandoned" by the next run |
| Channel uploads more than `MAX_UPLOAD_PAGES` × 50 videos in the window | run marked `partial` with "raise MAX_UPLOAD_PAGES", never silently truncated |
| Re-running the same day | upserts overwrite that day's snapshot — no duplicates |

Every run is logged in `core.pipeline_run` (status, quota used, errors) and visible on the
dashboard via `reporting.pipeline_runs`. GitHub Actions emails you when a scheduled run fails.

## Security

* **Secrets only in environment variables** — `.env` locally (git-ignored) and GitHub Actions
  secrets for the daily run. Nothing secret is in the code or the repo.
* **The API key never appears in URLs**: it is sent in the `X-Goog-Api-Key` header, so it can't
  leak through error messages, proxy logs or stored request parameters. Every error message
  is additionally redacted before it is logged or saved (covered by tests).
* `Settings` hides secrets when printed; the stored request parameters never contain the key.
* All SQL uses bound parameters (no string-built queries → no SQL injection).
* **Least privilege**: the dashboard uses a read-only `looker_reader` role that can only read
  the `reporting` views, not `raw`/`core`; the database connection uses SSL (`sslmode=require`);
  workflows run with read-only repo permissions.

---

## Setup — step by step

You need **no Docker and no local PostgreSQL**: the database is a free hosted Neon project, which
Looker Studio needs anyway. Commands are shown for **Windows (PowerShell)** and
**macOS/Linux** where they differ. Run every command **inside the project folder**.

### Step 1 — Install the tools
* **Python 3.10 or newer** (python.org). On Windows, tick *"Add python.exe to PATH"* in the installer.
* **Git** (git-scm.com) and a free **GitHub** account.
* A **Google account** (for the API key and Looker Studio).

Check: `python --version` and `git --version` both print a version.

### Step 2 — Get a YouTube Data API v3 key
1. Open https://console.cloud.google.com → project picker → **New project** (e.g. `yt-pipeline`).
2. **APIs & Services → Library** → search *YouTube Data API v3* → **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key** → copy it.
4. Click the key → **API restrictions → Restrict key → YouTube Data API v3** → Save.
   Leave *Application restrictions* on **None** (GitHub Actions has no fixed IP).

Keep the key secret: never paste it into code, chats, screenshots or commits.

### Step 3 — Create the database (Neon, free)
1. Sign up at https://neon.tech → **New project** → region **AWS Asia Pacific (Singapore)**
   (closest to Sri Lanka).
2. On the project dashboard click **Connect**, **turn OFF "Connection pooling"**, and copy the
   connection string. It looks like
   `postgresql://neondb_owner:...@ep-xxxx-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
   The host must **not** contain `-pooler`; the pipeline refuses pooled connections, because
   they break its run lock.
3. *(Only for the automated tests, Step 9)* **Databases → New database** → name it `yt_test`.

### Step 4 — Install the project
```powershell
# Windows (PowerShell)
cd youtube-performance-pipeline
python -m venv .venv
.venv\Scripts\Activate.ps1     # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-dev.txt
copy .env.example .env
```
```bash
# macOS / Linux
cd youtube-performance-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```
Your prompt now starts with `(.venv)`. Activate it again each time you open a new terminal.

### Step 5 — Fill in `.env`
Open `.env` in any editor and set the three required values:
```ini
YOUTUBE_API_KEY=AIza...your key...
DATABASE_URL=postgresql://neondb_owner:...@ep-xxxx-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
YOUTUBE_CHANNELS=@SomeChannel,@AnotherChannel
```
For channels, use the **@handle** from the channel's URL (`youtube.com/@SomeChannel`) or the
channel ID (`UC...`), separated by commas. The other settings have sensible defaults.
`.env` is in `.gitignore`, so it will never be committed.

### Step 6 — Create the tables and run the pipeline
```bash
python -m yt_pipeline init-db          # creates tables + reporting views (safe to re-run)
python -m yt_pipeline run              # first run collects all uploads from the last 28 days
python -m yt_pipeline status           # shows the run and row counts
python -m yt_pipeline verify --deep    # live checks: API, completeness, data, security
```
`run` should print `SUCCESS ... videos=N`, and `verify` should show no `FAIL`. If something
fails, the message says what to fix (wrong key, wrong channel, wrong database URL...).

Other commands: `run --lookback-days 7`, `run --channels @a,@b`, `retransform <run_id>`.
Exit codes: `0` ok · `1` error · `2` bad configuration · `3` quota exceeded · `4` partial run.

### Step 7 — Create the read-only login for the dashboard
```bash
python -m yt_pipeline create-reader
```
Type a strong password twice (at least 12 characters). It prints the **host, port, database and
username** you will type into Looker Studio. This login can only read the reporting views.

### Step 8 — Put the code on GitHub and switch on the daily run
1. On GitHub: **New repository** → name `youtube-performance-pipeline` → **Public** (so the
   reviewer can open it) → do **not** add a README → Create.
2. Push the code (the folder is already a git repository):
   ```bash
   git status                     # .env must NOT appear in the list
   git remote add origin https://github.com/YOUR-USERNAME/youtube-performance-pipeline.git
   git push -u origin main
   ```
3. In the repo: **Settings → Secrets and variables → Actions**
   * **Secrets** tab → *New repository secret*: `YOUTUBE_API_KEY` and `DATABASE_URL` (same values as `.env`)
   * **Variables** tab → *New repository variable*: `YOUTUBE_CHANNELS` (same value as `.env`)
4. **Actions** tab → **Daily YouTube pipeline** → **Run workflow**. It should turn green
   within a few minutes. After that it runs **every day at 06:00 Sri Lanka time**
   (00:30 UTC) by itself, and GitHub emails you if a run fails.
5. The **Tests** workflow also runs on every push and should be green.

### Step 9 — (Recommended) Run the automated tests
They need an **empty** database whose name contains `test`, the `yt_test` one from Step 3.
They delete all pipeline tables in it, so they refuse to run against any other database.
```powershell
# Windows (PowerShell): same connection string as DATABASE_URL, but ending /yt_test?sslmode=require
$env:TEST_DATABASE_URL="postgresql://neondb_owner:...@ep-xxxx-xxxx.ap-southeast-1.aws.neon.tech/yt_test?sslmode=require"
pytest
```
```bash
# macOS / Linux
TEST_DATABASE_URL="postgresql://neondb_owner:...@ep-xxxx-xxxx.ap-southeast-1.aws.neon.tech/yt_test?sslmode=require" pytest
```
Expect `40 passed`. They take longer against Neon than against a local database, because every query
crosses the internet. Without `TEST_DATABASE_URL` only the 22 unit tests run.

### Step 10 — Build the dashboard
Follow **[docs/LOOKER_STUDIO.md](docs/LOOKER_STUDIO.md)**: data sources, every chart, and the
controls (channel filter, date range, time-window selector, sortable tables). Then
**Share → Manage access** (anyone with the link can view), or download it as a PDF for a screenshot.

### Step 11 — Test it like a real system, then submit
* Work through **[TESTING.md](TESTING.md)**: failure drills, security checks, dashboard checks.
* Wait for at least one automatic daily run, so the dashboard shows two days of data.
* Reply to the email with: the **GitHub link**, the **dashboard link or screenshot**, a pointer
  to this README for **setup instructions**, and **[ASSUMPTIONS.md](ASSUMPTIONS.md)** as the
  assumptions and limitations note.

---

## Tests

**Real-world testing with your own API key, database and channels: see
[TESTING.md](TESTING.md)**. It covers the happy path, a completeness cross-check, failure drills
and security checks, with the expected result for each.

Automated tests: see Step 9 above for how to run them. 40 tests. The end-to-end tests run the whole pipeline against a real PostgreSQL using an
in-memory fake of the YouTube API (`tests/fake_youtube.py`, which also asserts the key is only
ever sent as a header). They cover:

* **Data**: required columns, playlist selection, duration parsing, NULL likes/comments,
  24h/7d/28d counts and ranks, day-over-day view gains, same-day idempotency, retransform.
* **Failures**: retries (5xx, 429 + `Retry-After`, 403 rate limit, bad JSON), retry exhaustion,
  quota stop, permission errors, 404s, one failing channel → `partial`, broken playlist →
  warning, failed run → rollback and clean re-run, overlapping runs blocked, raw purge,
  Ctrl+C / killed runs closed, high-volume channels flagged instead of silently truncated.
* **Verify command**: detects a missing video, a leaked key, a bad key and a missing schema.
* **Security**: API key never in URLs, error messages, stored raw data or run log; secrets
  hidden in `Settings`; missing/invalid configuration gives clear errors.

CI runs them on every push (`.github/workflows/tests.yml`).

## Project structure
```
yt_pipeline/
  config.py          settings from env / .env
  youtube_client.py  API client: retries, pagination, quota counting
  extract.py         API call plan (channels → uploads → videos → playlists)
  db.py              migrations, raw load, SQL transform
  pipeline.py        run orchestration + run log
  verify.py          live checks against the real API + DB (`verify`)
  __main__.py        CLI
sql/
  001_schema.sql     raw / core tables, helper functions
  002_views.sql      reporting views for Looker Studio
  transform.sql      raw JSON → core tables (upserts)
  003_looker_readonly_user.sql
tests/               unit + end-to-end tests, fake YouTube API
docs/LOOKER_STUDIO.md
TESTING.md           real-world test plan (live API, failure drills, security)
ASSUMPTIONS.md       assumptions and limitations
```

See **[ASSUMPTIONS.md](ASSUMPTIONS.md)** for assumptions and limitations.
