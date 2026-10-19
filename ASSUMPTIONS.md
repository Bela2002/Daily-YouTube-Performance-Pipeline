# Assumptions and limitations

## Assumptions

1. **"Previous 24 hours" and the 7/28-day windows** are measured back from the start of the
   latest successful pipeline run (`reference_ts_utc`), not from the moment the dashboard is
   opened. This keeps the numbers consistent with what was collected.
2. **Daily collection = new uploads + refreshed stats.** Each run captures every video published
   in the last 24 hours *and* refreshes statistics for all videos published in the last 28 days
   (`LOOKBACK_DAYS`), so the 7- and 28-day "top performers" use current numbers.
3. **"Top performing by views"** ranks videos *published in the window* by their **total
   (lifetime) views** at capture time — the Data API v3 only exposes lifetime counters.
   `views_gained_since_prev_capture` (day-over-day difference between snapshots) is added as a
   secondary "momentum" metric.
4. **Playlist fields**: a video can be in several playlists. `playlist_id`/`playlist_name` hold
   the playlist it was added to first; `all_playlist_names` lists all of them; `playlist_count`
   gives the number. Fields are empty when the video is in no public playlist.
5. **Grain**: one snapshot row per video per UTC capture date. Re-running on the same day
   overwrites that day's row (idempotent); a new day adds a new row, building history.
6. **Public data only**, accessed with an API key (no OAuth). Channels are configured by ID
   (`UC…`) or @handle and the dashboard is filterable by channel.
7. **All uploads count as videos**: Shorts, finished live streams and premieres are included,
   because they are all on the channel's uploads list. Scheduled (not yet public) videos appear
   once they go public.
8. **"Uploaded in the last 24 hours"** is as of the daily run (06:00 Sri Lanka time). A video
   uploaded at 10:00 appears in the next morning's data. That is the nature of a daily batch.
9. All timestamps are stored in UTC; a Sri Lanka (`Asia/Colombo`) publish time is added for
   readability.

## Limitations

1. **No true "views in the last 24 hours".** That metric needs the YouTube *Analytics* API with
   channel-owner OAuth. Day-over-day deltas only become available from the second daily run.
2. **History starts at the first run.** The API has no historical counters, so earlier days
   cannot be backfilled (the first run does pick up all uploads from the last 28 days).
3. **Hidden counters**: `like_count` is NULL when likes are hidden and `comment_count` is NULL
   when comments are disabled. Private, unlisted, deleted and scheduled (not yet public) videos
   are not visible to an API key.
4. **High-volume channels**: up to `MAX_UPLOAD_PAGES` × 50 = 5,000 uploads per channel per
   28-day window are collected. Beyond that the run is marked `partial` with a clear message,
   never silently truncated.
5. **Playlist mapping is capped** (`MAX_PLAYLISTS_PER_CHANNEL`, `MAX_PAGES_PER_PLAYLIST`) to
   protect quota; videos deep inside very large playlists may show no playlist. It can be turned
   off with `FETCH_PLAYLISTS=false`.
6. **Quota**: 10,000 units/day by default. A typical channel uses roughly 5–60 units per run (a very busy news channel a few hundred), so
   dozens of channels fit, but very large playlist counts reduce that.
7. **Run outcomes**: load, transform and the run status are committed in one transaction.
   A `failed` run (quota exceeded, database down) writes nothing except its `failed` row in
   `core.pipeline_run` and can simply be re-run. If one channel fails but others succeed, the
   good channels are saved, the run is marked `partial` with the reason, and the CLI exits with
   code 4 so the scheduler still flags it. A deleted playlist only produces a warning.
8. **Raw retention**: raw API JSON is purged after `RAW_RETENTION_DAYS` (default 90) to stay
   within free-tier storage; the daily history in `core` is kept. `retransform` therefore only
   works for runs inside that window.
9. **Scheduling**: GitHub Actions cron can start a few minutes late, and GitHub pauses scheduled
   workflows in public repos after 60 days without commits (one click re-enables them).
10. **Looker Studio needs an internet-reachable database** (e.g. Neon/Supabase free tier); a
   localhost PostgreSQL cannot be connected directly. The PostgreSQL connector returns at most
   150k rows per query, far above this dataset's size.
11. Live streams report a duration of 0 while live; durations update once the stream ends.
12. **Testing**: the YouTube API is replaced by a faithful fake in the automated tests (no key
    or network needed in CI). Behaviour against the live API is checked with
    `python -m yt_pipeline verify --deep` and the manual drills in TESTING.md. A change in the
    API's response format would show up as a failed run, not as a failing test.
13. **Nice-to-have GCP parts not implemented**: the pipeline runs on GitHub Actions (free,
    no server to manage) and stores data in Neon PostgreSQL, which the brief allows. Moving to
    Cloud Run + Cloud Scheduler would need only a small container wrapper and no code changes,
    because all configuration comes from environment variables.
14. **Database connection**: the pipeline needs a direct (non-pooled) PostgreSQL connection,
    because transaction-mode poolers such as Neon's `-pooler` host break the run lock. It
    refuses to start on a pooled connection and says why.
