# Testing and Validation

This document describes how the pipeline is tested at four levels:

1. **Automated tests**: unit and end-to-end tests against a fake YouTube API.
2. **Live verification**: the built-in `verify` command, run against the real API and database.
3. **Manual validation**: SQL checks against YouTube, multi-day behaviour and failure scenarios.
4. **Security, scheduling and dashboard checks.**

Each check lists its **expected result**. Any other outcome indicates a defect.

> **API key handling.** The API key belongs only in `.env` (local) and GitHub Actions secrets
> (CI). It should never appear in code, commits, screenshots or chat. Restrict the key to
> **YouTube Data API v3** in the Google Cloud Console, with **Application restrictions: None**,
> because GitHub Actions has no fixed IP address.

Commands are shown in Bash syntax. In PowerShell, set a variable for a single command with
`$env:NAME="value"` on its own line, then run the command.

---

## 1. Automated tests

**40 tests**: 22 unit tests (`tests/test_unit.py`) and 18 end-to-end tests
(`tests/test_integration.py`).

The end-to-end tests run the complete pipeline against a real PostgreSQL database. They
replace the YouTube API with an in-memory fake (`tests/fake_youtube.py`), so no API key or
network access is needed. The fake also asserts that the API key is only ever sent as a
request header.

### Running the tests

```bash
pytest                                                                  # unit tests only
TEST_DATABASE_URL="postgresql://...@.../yt_test?sslmode=require" pytest  # all 40 tests
```

**Expected:** `40 passed` with `TEST_DATABASE_URL` set; `22 passed, 18 skipped` without it.

![pytest: 40 passed](docs/images/pytest-results.png)

### Safety guards

- The integration tests **drop and recreate** the `raw`, `core` and `reporting` schemas. They
  refuse to run unless the database name contains `test` and differs from `DATABASE_URL`.
- PostgreSQL roles are shared by every database on a server. The tests therefore use a
  dedicated `looker_reader_test` role, remove it afterwards, and never modify the production
  `looker_reader` login.

### Coverage

| Area | Covered behaviour |
|---|---|
| **Data** | Required columns, playlist selection, duration parsing, `NULL` likes/comments, 24h/7d/28d counts and ranks, day-over-day view gains, same-day idempotency, retransform |
| **Failures** | Retries (5xx, 429 with `Retry-After`, 403 rate limit, invalid JSON), retry exhaustion, quota stop, permission errors, 404s, one failing channel → `partial`, broken playlist → warning, failed run → rollback and clean re-run, overlapping runs blocked, raw purge, interrupted and killed runs closed, high-volume channels flagged instead of truncated |
| **Verify command** | Detects a missing video, a leaked key, an invalid key and a missing schema |
| **Security** | API key never in URLs, error messages, stored raw data or the run log; secrets masked in `Settings`; clear errors for missing or invalid configuration |

The **Tests** workflow runs the full suite on every push and pull request, against a
PostgreSQL 16 service container.

## 2. Live verification

```bash
python -m yt_pipeline init-db
python -m yt_pipeline run
python -m yt_pipeline verify --deep
```

**Expected from `run`:** `SUCCESS run_id=... channels=N videos=N ... quota_units=...`

**Expected from `verify --deep`:** every line is `PASS` or `INFO`.

![verify --deep output](docs/images/verify-deep.png)

| Section | What it checks |
|---|---|
| **API** | The key authenticates via the request header, and every configured channel resolves |
| **Database** | Schemas, tables and reporting views exist |
| **Pipeline** | The latest run succeeded and is recent (warns if older than 26 hours); every channel is stored |
| **Completeness** *(`--deep`)* | Every video that YouTube search returns for the lookback window is in the database. This is an independent cross-check using a different API endpoint |
| **Data** | Required fields are filled; exactly one row per video per day; the top 5 videos match the live API |
| **Security** | Key not stored in the database or git history; SSL in use; `.env` not committed; `looker_reader` is read-only |

**Quota cost:** plain `verify` uses about 5–10 units. `--deep` adds 100 units per search page
(up to 50 videos) per channel. Use `--deep` after setup and after code changes, not daily.

### Collection scope

The pipeline collects every public video **published within the lookback window** (default
28 days), not a channel's full upload history. This matches the 24h / 7d / 28d reporting
windows. History accumulates through one snapshot per day from the first run onward. The
completeness check confirms that nothing inside the window is missed.

## 3. Manual data validation

The following queries can be run in the Neon SQL Editor or with `psql "$DATABASE_URL"`.

### 3.1 Uploads in the last 24 hours

```sql
SELECT channel_name, video_title, published_at_colombo, video_duration,
       view_count, like_count, comment_count
FROM reporting.video_by_window
WHERE time_window = 'Last 24 hours'
ORDER BY published_at_utc DESC;
```

Compare the result with each channel's **Videos**, **Shorts** and **Live** tabs on youtube.com.

| Check | Method | Expected |
|---|---|---|
| Video list | Compare with the channel pages | Same videos |
| Titles and durations | Compare 3 videos | Identical |
| Views / likes / comments | Compare 3 videos | Close but not identical, because counters change continuously after capture |
| Publish time | Compare with YouTube (local time) | Matches `published_at_colombo` |
| Playlist | A video that belongs to a playlist | `playlist_name` matches; empty for videos in no public playlist |
| Hidden likes / comments disabled | A video with either setting | `like_count` / `comment_count` is `NULL`, not `0` |

Shorts and completed live streams are included, because they are part of a channel's uploads.
Scheduled premieres appear once they become public.

### 3.2 Multi-day history

After a second daily run:

```sql
SELECT data_capture_date, count(*)
FROM reporting.video_daily_dataset
GROUP BY 1 ORDER BY 1;

SELECT video_title, view_count, views_gained_since_prev_capture
FROM reporting.video_latest
ORDER BY views_gained_since_prev_capture DESC NULLS LAST
LIMIT 10;
```

**Expected:** two or more capture dates, and `views_gained_since_prev_capture` populated.

## 4. Failure scenarios

Each scenario is run on its own, with the setting restored afterwards. The run log is
inspected with `python -m yt_pipeline status`, and exit codes with `echo $?` (Bash) or
`echo $LASTEXITCODE` (PowerShell).

| # | Scenario | Expected behaviour |
|---|---|---|
| a | Invalid API key: `YOUTUBE_API_KEY=wrong python -m yt_pipeline run` | `FAILED: YouTube API error ... API key not valid`, exit code `1`, run logged as `failed`, existing data untouched |
| b | Non-existent channel: `run --channels "@realchannel,UCxxxxxxxxxxxxxxxxxxxxxx"` | `PARTIAL`, exit code `4`, valid channel saved, `WARNING: channel not found: UCxxx…` |
| c | Quota exhausted: Cloud Console → IAM & Admin → **Quotas** → YouTube Data API v3 → *Queries per day* lowered to 5 | `FAILED: quota exceeded`, exit code `3`, nothing partially saved. The quota is restored to 10,000 afterwards; changes may take a few minutes to apply |
| d | No network connection | Retry warnings, then `FAILED: ... network error`. The key never appears in output (any URL shows `***REDACTED***`) |
| e | Wrong database password in `DATABASE_URL` | 3 connection attempts, then `FAILED: database error: ... password authentication failed`, without a Python traceback |
| f | Two runs on the same day | Today's row count in `core.video_daily_snapshot` is unchanged; rows are updated, not duplicated |
| g | **Ctrl+C** during a run | Run logged as `failed` (`KeyboardInterrupt`), nothing partially saved; the next run succeeds |
| h | Two simultaneous runs (several channels, so the run lasts a few seconds) | The second run reports `Another pipeline run is already in progress` |

Example: scenario **b** (partial run with one invalid channel).

![Partial run with an invalid channel](docs/images/failure-partial-run.png)

Run log after the failure scenarios (`python -m yt_pipeline status`):

![Run log after failure scenarios](docs/images/failure-status-log.png)

## 5. Security checks

```bash
python -m yt_pipeline verify      # Security section: all PASS
git status                        # .env is not listed
git grep -n "AIza"                # no output (Google API keys start with "AIza")
```

| Check | Expected |
|---|---|
| Query a core table as the reader: `psql "postgresql://looker_reader:PASSWORD@HOST/DB?sslmode=require" -c "select * from core.video"` | `permission denied for schema core` |
| GitHub Actions run logs | Secrets are shown as `***` |

### Reader access check without `psql`

If `psql` is not installed, the same check can be run from the project's virtual environment.
The command takes the host and database from `DATABASE_URL`, connects as `looker_reader`,
and prompts for the password without displaying it:

```bash
python -c "import os, getpass, psycopg; from yt_pipeline.config import Settings; from psycopg.conninfo import conninfo_to_dict; info = conninfo_to_dict(os.environ['DATABASE_URL']); info.update(user='looker_reader', password=getpass.getpass('looker_reader password: ')); conn = psycopg.connect(**info); print('Connected as', conn.info.user); print('reporting.video_latest rows:', conn.execute('SELECT count(*) FROM reporting.video_latest').fetchone()[0]); conn.execute('SELECT * FROM core.video LIMIT 1')"
```

**Expected:** the login succeeds, the `reporting` view returns a row count, and the query on
`core.video` fails with `InsufficientPrivilege: permission denied for schema core`.

![Read-only login denied access to core tables](docs/images/security-reader-denied.png)

If a key is ever exposed (committed, pasted or captured in a screenshot), it must be revoked in
the Google Cloud Console and replaced. Removing it from git history is not sufficient.

## 6. Scheduled runs

| Check | Expected |
|---|---|
| **Actions → Daily YouTube pipeline → Run workflow** | The run completes successfully |
| The morning after setup | A scheduled run started shortly after 06:00 Sri Lanka time (00:30 UTC). GitHub may start scheduled jobs a few minutes late |
| A deliberately failing run (for example, an invalid secret) | GitHub sends a failure notification email |
| `verify` when the latest run is older than 26 hours | Freshness warning |

![Scheduled workflow run](docs/images/scheduled-run.png)

## 7. Dashboard checks

| Check | Expected |
|---|---|
| "Uploads last 24h / 7d / 28d" scorecards | Match the `uploads last ...` lines in `verify` |
| "Uploaded in the last 24 hours" table | Matches the query in section 3.1 |
| Time-window drop-down | Changes only the top-videos table, not the scorecards |
| Views / Likes / Comments column headers | Clicking re-sorts the table |
| Channel filter | Filters every chart |
| Date range control | Today's uploads remain visible (see [docs/LOOKER_STUDIO.md](docs/LOOKER_STUDIO.md)) |

## Validation checklist

- [x] `pytest`: 40 passed
- [x] `verify --deep`: no `FAIL`
- [x] Failure scenarios a–h behave as described
- [x] At least two capture dates, with `views_gained_since_prev_capture` populated
- [x] At least one scheduled run completed on its own
- [x] Dashboard figures match `verify`
- [x] `git grep AIza` returns nothing and `.env` is not tracked
