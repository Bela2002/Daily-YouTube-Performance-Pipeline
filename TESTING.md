# Testing with the real YouTube API

The automated tests (`pytest`) use a fake YouTube API. This guide tests the **real** setup:
your API key, your database, real channels. Work through it top to bottom. Every step says
what you should see. If you see something else, the step has found a problem.

> **Before you start.** Never paste your API key into chat, code, screenshots or commits.
> It lives only in `.env` (local) and GitHub Secrets (CI). In Google Cloud Console, restrict the
> key: **API restrictions → YouTube Data API v3 only**. **Application restrictions → None**,
> because GitHub Actions has no fixed IP address.

Commands are written for macOS/Linux. On Windows PowerShell, set a variable for one command
with `$env:NAME="value"` on its own line before the command.

---

## 1. Automated tests (no API key needed)

Follow README **Step 9**: set `TEST_DATABASE_URL` to the empty `yt_test` database and run `pytest`.
**Expect:** `40 passed`. They refuse to run against a database whose name does not contain
`test`, because they delete its tables.

## 2. First real run (the "happy path")

Pick **one or two medium-size channels** that upload regularly, for example an agency client
or a news channel. Put them in `YOUTUBE_CHANNELS`.

```bash
python -m yt_pipeline init-db
python -m yt_pipeline run
python -m yt_pipeline verify --deep
```

**Expect from `run`:** `SUCCESS run_id=... channels=2 videos=N ... quota_units=...`.

**Expect from `verify --deep`:** every line `PASS` or `INFO`. Each line answers one question:

| Section | What it proves |
|---|---|
| API | your key works with the header method, and every channel resolves |
| Completeness | **every video YouTube's search finds for the 28-day window is in your DB.** This is an independent cross-check using a different API endpoint |
| Data | required fields are filled; one row per video per day; top 5 videos agree with YouTube *right now* |
| Security | key not stored in DB or git history; SSL on; read-only dashboard user is really read-only |

`--deep` costs **100 quota units per 50 videos per channel**, out of your 10,000 per day.
Plain `verify` costs about 10 units. Use `--deep` once after setup and after any code change,
not every day.

> **Does it get ALL videos from the channel?** No, by design. It gets every public video
> **published in the last 28 days** (`LOOKBACK_DAYS`), because the brief asks about the
> 24h / 7d / 28d windows. Each daily run adds that day's snapshot, so history builds up from
> your first run. The Completeness check is what proves nothing *inside* the window is missed.
> For a very busy channel (more than 5,000 uploads in 28 days), the run is marked `PARTIAL`
> with "raise MAX_UPLOAD_PAGES". It never drops videos silently.

## 3. Check the numbers by eye against YouTube

Run these in the **Neon SQL Editor**, or in `psql "$DATABASE_URL"`.

```sql
-- What was uploaded in the last 24 hours?
SELECT channel_name, video_title, published_at_colombo, video_duration,
       view_count, like_count, comment_count
FROM reporting.video_by_window
WHERE time_window = 'Last 24 hours'
ORDER BY published_at_utc DESC;
```
Open each channel on youtube.com → **Videos** tab (and **Shorts** / **Live** tabs). The videos
from the last 24 hours should be the same list.

| Check | How | Expect |
|---|---|---|
| Titles, durations | compare 3 videos | identical |
| Views / likes / comments | compare 3 videos | close, not identical: counts change every minute, and the run was earlier |
| Publish time | YouTube shows local time | matches `published_at_colombo` |
| Playlist | open a video that is in one of the channel's playlists | its `playlist_name` matches; videos in no playlist have it empty |
| Hidden likes / comments off | find such a video | `like_count` / `comment_count` is empty (NULL), not 0 |

> Shorts and finished live streams **are included**, because they are uploads too.
> Scheduled premieres appear once they go public.

## 4. Second day

Run the pipeline again the next day (or let the schedule do it), then:
```sql
SELECT data_capture_date, count(*) FROM reporting.video_daily_dataset GROUP BY 1 ORDER BY 1;
SELECT video_title, view_count, views_gained_since_prev_capture
FROM reporting.video_latest ORDER BY views_gained_since_prev_capture DESC NULLS LAST LIMIT 10;
```
**Expect:** two capture dates, and `views_gained_since_prev_capture` filled in.

## 5. Failure drills: break it on purpose

Do these one at a time and set things back afterwards. Then check the run log:
`python -m yt_pipeline status`.

| # | How to break it | Expected result |
|---|---|---|
| a | Wrong API key: `YOUTUBE_API_KEY=wrong python -m yt_pipeline run` | `FAILED: YouTube API error ... API key not valid`, exit code 1, run logged `failed`, existing data untouched |
| b | Add a fake channel: `run --channels "@yourchannel,UCxxxxxxxxxxxxxxxxxxxxxx"` | `PARTIAL`, exit code 4, the real channel is saved, `WARNING: channel not found: UCxxx…` |
| c | **Real quota error**: Cloud Console → IAM & Admin → **Quotas** → YouTube Data API v3 → *Queries per day* → edit it down to **5**, then run | `FAILED: quota exceeded`, exit code 3, nothing half-saved. **Set the quota back to 10,000 afterwards.** The change can take a few minutes to apply. |
| d | Turn Wi-Fi off and run | retry warnings, then `FAILED: ... network error`. The error must **not** show your key (look for `***REDACTED***` if a URL appears) |
| e | Wrong DB password in `DATABASE_URL` | 3 connection attempts, then `FAILED: database error: ... password authentication failed`, with no Python traceback |
| f | Run twice on the same day | today's row count in `core.video_daily_snapshot` stays the same (rows are updated, not duplicated) |
| g | Start a run, press **Ctrl+C** halfway | run logged `failed` (`KeyboardInterrupt`), nothing half-saved; the next run works normally |
| h | Start two runs at the same time in two terminals (use several channels so it takes a few seconds) | the second says `Another pipeline run is already in progress` |

To see the exit code: macOS/Linux `echo $?`, PowerShell `echo $LASTEXITCODE`.

## 6. Security checks

```bash
python -m yt_pipeline create-reader        # read-only login for Looker Studio
python -m yt_pipeline verify               # Security section must be all PASS
git status                                 # .env must NOT be listed
git grep -n "AIza"                         # must print nothing (Google API keys start with AIza)
```
* Connect once as the reader and try a forbidden table:
  `psql "postgresql://looker_reader:PASSWORD@HOST/DB?sslmode=require" -c "select * from core.video"`
  → `permission denied for schema core`.
* After the GitHub Action runs, open its log. Secrets must appear as `***`.
* If a key ever leaks (committed, pasted, screenshotted): **delete it in Cloud Console and create
  a new one.** Removing it from git is not enough.

## 7. The daily schedule

1. Push to GitHub and add the secrets and the variable (see README).
2. **Actions → Daily YouTube pipeline → Run workflow** → it should go green.
3. The next morning, check that it ran **by itself** after 06:00 Sri Lanka time
   (GitHub can be a few minutes late). `verify` warns if the latest run is older than 26 hours.
4. Make it fail once on purpose (e.g. a wrong secret) and confirm GitHub emails you.

## 8. Dashboard checks

| Check | Expect |
|---|---|
| "Uploads last 24h / 7d / 28d" scorecards | same numbers as the `uploads last ...` lines in `verify` |
| Last-24-hours table | same list as the SQL in step 3 |
| Time-window dropdown | changes only the top-videos table, not the scorecards |
| Click the Views / Likes / Comments headers | the table re-sorts |
| Channel filter | every chart changes |
| Date range | today's uploads still visible (see the date-range note in docs/LOOKER_STUDIO.md) |

## Before you submit

- [ ] `pytest` passes (40)
- [ ] `verify --deep` has no FAIL
- [ ] failure drills a–h behave as described
- [ ] at least 2 days of data (so `views gained` shows)
- [ ] scheduled run worked by itself at least once
- [ ] dashboard numbers match `verify`
- [ ] no key in git (`git grep AIza` is empty), `.env` not committed
