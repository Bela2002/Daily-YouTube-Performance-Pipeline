# Daily YouTube Performance Pipeline

A daily ELT pipeline that collects publishing and performance data for one or more YouTube
channels from the **YouTube Data API v3**, stores it in **PostgreSQL**, and serves it to a
**Looker Studio** dashboard. It provides a single source of truth for what each channel
published and which videos performed best over the last **24 hours**, **7 days** and **28 days**.

![Looker Studio dashboard](docs/images/dashboard.png)

```mermaid
flowchart LR
    A[GitHub Actions<br/>daily 06:00 SLT] --> B[Python: Extract<br/>YouTube Data API v3]
    B --> C[(raw.api_response<br/>JSONB, unmodified)]
    C -->|SQL transform| D[(core tables<br/>channel · video · playlist<br/>video_daily_snapshot)]
    D --> E[(reporting views)]
    E --> F[Looker Studio<br/>dashboard]
```

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Data model](#data-model)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Usage](#usage)
- [Scheduling and CI](#scheduling-and-ci)
- [Reliability](#reliability)
- [Security](#security)
- [Testing](#testing)
- [Project structure](#project-structure)
- [Further documentation](#further-documentation)

---

## Features

- **Multi-channel**: channels are configured by channel ID (`UC…`) or `@handle`.
- **Daily history**: one snapshot per video per day (views, likes, comments), building a
  time series from the first run onward.
- **Quota-efficient**: uses only 1-unit `list` calls and stops reading a channel's uploads as
  soon as it passes the 28-day window.
- **Idempotent**: re-running a day updates that day's rows instead of duplicating them.
- **Auditable**: every API response is stored unmodified, so transforms can be rebuilt
  without spending API quota.
- **Observable**: every run is logged with its status, quota usage and errors, and a
  `verify` command checks the live system end to end.
- **Secure by default**: secrets live only in environment variables, the API key is never
  placed in a URL, and the dashboard uses a read-only database login.

## Architecture

The pipeline follows an **Extract → Load → Transform** pattern with three database layers.

| Layer | Contents | Purpose |
|---|---|---|
| `raw` | Every API response page, stored as JSONB | Full audit trail; transforms can be re-run with zero API quota (`retransform`) |
| `core` | Typed, de-duplicated tables built in SQL with upserts | Idempotent storage; re-running a day never duplicates rows |
| `reporting` | Views shaped for the dashboard | Time windows, rankings and playlist logic are kept in version-controlled SQL rather than in the BI tool |

### Run sequence

1. **`channels.list`**: resolves each channel's name and its *uploads* playlist (by ID or `@handle`).
2. **`playlistItems.list`** on the uploads playlist, newest first. Paging stops as soon as a
   page is older than the lookback window (default 28 days).
3. **`videos.list`** in batches of 50: video metadata and statistics.
4. **`playlists.list`** + **`playlistItems.list`**: maps each video to the playlists it belongs to.
5. **Load and transform**: raw pages are inserted into `raw.api_response`, then
   `sql/transform.sql` upserts the core tables. Both steps and the run status are committed
   in **one transaction**.

Every call is a `list` call costing 1 quota unit. A typical channel uses roughly 5–60 units
per run, and a high-volume news channel a few hundred. The default quota is 10,000 units per day.

## Data model

### Core tables

| Table | Grain |
|---|---|
| `core.channel` | One row per channel |
| `core.video` | One row per video (metadata) |
| `core.playlist` | One row per playlist |
| `core.video_playlist` | Bridge table; a video can belong to many playlists |
| `core.video_daily_snapshot` | One row per video per capture day (views, likes, comments) |
| `core.pipeline_run` | One row per pipeline run (status, quota used, errors) |

### Reporting views

| View | Grain | Used for |
|---|---|---|
| `reporting.video_daily_dataset` | Video × capture date | The complete daily dataset; trends and exports |
| `reporting.video_latest` | One row per video (latest snapshot) | Latest stats with 24h / 7d / 28d flags |
| `reporting.video_by_window` | Video × time window | Top-performer tables and "uploaded in the last 24 hours" |
| `reporting.upload_summary` | Channel × time window | Upload-count scorecards (zero-filled) |
| `reporting.pipeline_runs` | One row per run | Data freshness and run history |

### Dataset fields (`reporting.video_daily_dataset`)

| Field | Description |
|---|---|
| `video_id`, `video_title`, `video_url` | Video identifier, title and link |
| `channel_id`, `channel_name` | Channel identifier and name |
| `playlist_id`, `playlist_name` | Primary playlist (the one the video was added to first); empty if none |
| `playlist_count`, `all_playlist_names` | Number of playlists and all playlist names |
| `published_at_utc`, `published_date_utc` | Publish timestamp and date (UTC) |
| `published_at_colombo` | Publish timestamp in Sri Lanka time (`Asia/Colombo`) |
| `video_duration`, `video_duration_seconds`, `video_duration_iso` | Duration as `H:MM:SS`, seconds and ISO 8601 |
| `view_count`, `like_count`, `comment_count` | Counters at capture time (`NULL` when hidden or disabled) |
| `data_capture_date`, `data_capture_ts_utc` | Capture date and timestamp (UTC) |
| `views_gained_since_prev_capture` | Day-over-day view change (from the second capture onward) |
| `engagement_rate` | `(likes + comments) / views` |

---

## Getting started

### Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | CI uses Python 3.12 |
| Git | |
| YouTube Data API v3 key | From a Google Cloud project |
| PostgreSQL database | Must be reachable from the internet for Looker Studio; [Neon](https://neon.tech) (free tier) is used here |
| Google account | For Looker Studio |

Docker and a local PostgreSQL installation are **not** required.

### 1. Create a YouTube Data API key

1. In the [Google Cloud Console](https://console.cloud.google.com), create or select a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → Credentials → Create credentials → API key**.
4. Restrict the key: **API restrictions → YouTube Data API v3**. Leave **Application
   restrictions** set to **None**, because GitHub Actions runners have no fixed IP address.

### 2. Create the database

1. Create a Neon project (for example in **AWS Asia Pacific (Singapore)**).
2. Under **Connect**, turn **off** *Connection pooling* and copy the connection string:
   ```
   postgresql://neondb_owner:...@ep-xxxx-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```
   The host must **not** contain `-pooler`. Transaction-mode poolers break the pipeline's
   run lock, so the pipeline refuses pooled connections.
3. *(Optional, for the integration tests)* create a second, empty database named `yt_test`.

### 3. Install

```bash
git clone <repository-url>
cd Daily-YouTube-Performance-Pipeline
```

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-dev.txt
copy .env.example .env
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

### 4. Configure

Edit `.env` and set the three required values (see [Configuration](#configuration)):

```ini
YOUTUBE_API_KEY=your-api-key
DATABASE_URL=postgresql://neondb_owner:...@ep-xxxx-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
YOUTUBE_CHANNELS=@SomeChannel,UCxxxxxxxxxxxxxxxxxxxxxx
```

`.env` is listed in `.gitignore` and is never committed.

### 5. Initialise and run

```bash
python -m yt_pipeline init-db          # create schemas, tables and views (safe to re-run)
python -m yt_pipeline run              # first run collects all uploads from the last 28 days
python -m yt_pipeline status           # recent runs and row counts
python -m yt_pipeline verify --deep    # live end-to-end checks
```

A successful run prints `SUCCESS run_id=... videos=N ...`, and `verify` reports no `FAIL` lines.

![Pipeline run and status output](docs/images/pipeline-run.png)

### 6. Create the dashboard login

```bash
python -m yt_pipeline create-reader
```

This creates (or resets the password of) the read-only `looker_reader` login and prints the
host, port, database and username for Looker Studio. The login can read the `reporting`
views only. The password must be at least 12 characters. It can also be supplied
non-interactively through the `READER_PASSWORD` environment variable.

### 7. Build the dashboard

See **[docs/LOOKER_STUDIO.md](docs/LOOKER_STUDIO.md)**.

---

## Configuration

All settings are read from environment variables, or from a local `.env` file.

| Variable | Required | Default | Description |
|---|:---:|---|---|
| `YOUTUBE_API_KEY` | ✔ | | YouTube Data API v3 key |
| `DATABASE_URL` | ✔ | | PostgreSQL connection string (direct, non-pooled, `sslmode=require`) |
| `YOUTUBE_CHANNELS` | ✔ | | Comma-separated channel IDs (`UC…`) and/or `@handles` |
| `LOOKBACK_DAYS` | | `28` | Videos published within this many days are collected and refreshed |
| `FETCH_PLAYLISTS` | | `true` | Map videos to playlists (uses extra quota) |
| `MAX_PLAYLISTS_PER_CHANNEL` | | `50` | Cap on playlists read per channel |
| `MAX_PAGES_PER_PLAYLIST` | | `10` | Cap on pages (50 items each) read per playlist |
| `MAX_UPLOAD_PAGES` | | `100` | Cap on upload pages per channel (100 × 50 = 5,000 videos) |
| `RAW_RETENTION_DAYS` | | `90` | Days to keep raw API JSON (`0` = keep forever) |
| `LOG_LEVEL` | | `INFO` | Python logging level |

## Usage

| Command | Description |
|---|---|
| `python -m yt_pipeline init-db` | Create or update schemas, tables and views (idempotent) |
| `python -m yt_pipeline run` | Run the daily extract-load-transform |
| `python -m yt_pipeline run --lookback-days 7` | Override the lookback window for one run |
| `python -m yt_pipeline run --channels "@a,@b"` | Override the channel list for one run |
| `python -m yt_pipeline status` | Show the last 10 runs and table row counts |
| `python -m yt_pipeline verify` | Live checks: API, data, freshness, security (~5–10 quota units) |
| `python -m yt_pipeline verify --deep` | Adds a completeness cross-check via YouTube search (100 units per search page) |
| `python -m yt_pipeline retransform <run_id>` | Rebuild core tables from a run's stored raw JSON (no API quota) |
| `python -m yt_pipeline create-reader` | Create or reset the read-only `looker_reader` login |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Error (API, database or runtime) |
| `2` | Invalid or missing configuration |
| `3` | YouTube API quota exceeded; nothing saved, retry after the daily reset |
| `4` | Partial run; data saved, but one or more channels failed |

## Scheduling and CI

Two GitHub Actions workflows are included in `.github/workflows/`.

| Workflow | Trigger | What it does |
|---|---|---|
| **Daily YouTube pipeline** (`daily-pipeline.yml`) | Daily at 00:30 UTC (06:00 Sri Lanka time), or on demand | Runs `init-db`, `run` and `status` against the production database |
| **Tests** (`tests.yml`) | Every push and pull request | Runs the full test suite against a PostgreSQL 16 service container |

### Repository settings for the daily workflow

Under **Settings → Secrets and variables → Actions**:

| Type | Name | Value |
|---|---|---|
| Secret | `YOUTUBE_API_KEY` | API key |
| Secret | `DATABASE_URL` | Direct database connection string |
| Variable | `YOUTUBE_CHANNELS` | Comma-separated channel list |

The pipeline can be run on demand from **Actions → Daily YouTube pipeline → Run workflow**,
with an optional `lookback_days` input. Runs are serialised through a concurrency group, have
a 20-minute timeout, and use read-only repository permissions. GitHub notifies the
repository owner when a scheduled run fails.

Local and scheduled runs write to the same database, so the dashboard reflects whichever
ran last.

![GitHub Actions workflow runs](docs/images/github-actions.png)

## Reliability

| Scenario | Behaviour |
|---|---|
| Network error, HTTP 429/5xx, 403 rate limit, invalid JSON | Retried with exponential backoff (honours `Retry-After`), up to 4 retries |
| Daily quota exceeded | Run stops immediately, nothing is saved, status `failed`, exit code `3` |
| One channel fails (invalid ID, API error) | Other channels are saved; status `partial`, exit code `4`, reason recorded |
| Channel with no uploads / deleted playlist | Treated as "no videos" or logged as a warning; not a failure |
| Database unreachable (or serverless database waking up) | 3 connection attempts, then a clear error, exit code `1` |
| Crash mid-run | Load, transform and status share one transaction; no partially written day |
| Concurrent runs | A PostgreSQL advisory lock rejects the second run |
| Interrupted run (Ctrl+C, killed, timed out) | Logged as `failed`; a killed run is closed as "abandoned" by the next run |
| More than `MAX_UPLOAD_PAGES` × 50 uploads in the window | Run marked `partial` with a "raise MAX_UPLOAD_PAGES" message; never silently truncated |
| Same day re-run | Upserts overwrite that day's snapshot; no duplicates |

Every run is recorded in `core.pipeline_run` and exposed through `reporting.pipeline_runs`.

## Security

- **Secrets in environment variables only**: `.env` locally (git-ignored) and GitHub Actions
  secrets in CI. No secrets are stored in the code or repository.
- **API key never in URLs**: the key is sent in the `X-Goog-Api-Key` header, so it cannot leak
  through error messages, proxy logs or stored request parameters. All error messages are
  additionally redacted before they are logged or stored.
- **Secret-safe objects**: `Settings` masks secrets when printed, and stored request parameters
  never contain the key.
- **Parameterised SQL**: all queries use bound parameters.
- **Least privilege**: the dashboard connects as `looker_reader`, which can read only the
  `reporting` views (not `raw` or `core`). Database connections require SSL, and the
  workflows run with read-only repository permissions.

If an API key is ever exposed, revoke it in the Google Cloud Console and issue a new one.
Removing it from git history is not sufficient.

## Testing

The project has **40 automated tests**: 22 unit tests and 18 end-to-end tests. The end-to-end
tests run the whole pipeline against a real PostgreSQL database, using an in-memory fake of
the YouTube API (`tests/fake_youtube.py`).

```bash
pytest                                  # unit tests only
```

```powershell
# Windows (PowerShell): include the integration tests
$env:TEST_DATABASE_URL="postgresql://...@.../yt_test?sslmode=require"
pytest
```

```bash
# macOS / Linux: include the integration tests
TEST_DATABASE_URL="postgresql://...@.../yt_test?sslmode=require" pytest
```

The integration tests drop and recreate the pipeline schemas. They refuse to run unless the
database name contains `test` and differs from `DATABASE_URL`.

See **[TESTING.md](TESTING.md)** for test coverage, live validation and failure-scenario testing.

## Project structure

```
yt_pipeline/
  __main__.py          command-line interface
  config.py            settings from environment / .env
  youtube_client.py    API client: retries, pagination, quota accounting
  extract.py           API call plan (channels → uploads → videos → playlists)
  db.py                migrations, raw load, SQL transform, read-only role
  pipeline.py          run orchestration and run log
  verify.py            live checks against the real API and database
sql/
  001_schema.sql       raw and core tables, helper functions
  002_views.sql        reporting views
  transform.sql        raw JSON → core tables (upserts)
  003_looker_readonly_user.sql   read-only role (psql alternative to create-reader)
tests/                 unit and end-to-end tests, fake YouTube API
docs/LOOKER_STUDIO.md  dashboard guide
.github/workflows/     daily pipeline and test workflows
```

## Further documentation

| Document | Contents |
|---|---|
| [docs/LOOKER_STUDIO.md](docs/LOOKER_STUDIO.md) | Dashboard data sources, layout, charts and controls |
| [TESTING.md](TESTING.md) | Automated tests, live validation, failure scenarios and security checks |
| [ASSUMPTIONS.md](ASSUMPTIONS.md) | Design assumptions and known limitations |
