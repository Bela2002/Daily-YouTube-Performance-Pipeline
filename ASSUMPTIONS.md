# Assumptions and Limitations

This document records the design decisions behind the pipeline and the constraints it
operates under.

## Assumptions

### Time windows and collection

1. **Reference time.** The 24-hour, 7-day and 28-day windows are measured back from the start
   of the latest successful or partial pipeline run (`reference_ts_utc`), not from the moment
   the dashboard is opened. This keeps the reported figures consistent with the data that was
   actually collected.
2. **Daily collection scope.** Each run captures every video published in the last 24 hours
   **and** refreshes the statistics of every video published within `LOOKBACK_DAYS`
   (default 28). The 7-day and 28-day top-performer rankings therefore use current figures.
3. **Batch latency.** "Uploaded in the last 24 hours" reflects the state at the time of the
   daily run (06:00 Sri Lanka time). A video published later that day appears in the next
   run's data.

### Metrics

4. **"Top performing by views"** ranks videos *published within a window* by their total
   (lifetime) view count at capture time, because the Data API v3 exposes lifetime counters
   only. `views_gained_since_prev_capture`, the day-over-day difference between snapshots,
   is provided as a secondary momentum metric.
5. **Engagement rate** is defined as `(likes + comments) / views`. Hidden counters count as zero
   in this calculation.

### Data model

6. **Grain.** One snapshot row per video per UTC capture date. Re-running on the same day
   overwrites that day's row (idempotent), and each new day adds a row, building history.
7. **Playlists.** A video can belong to several playlists. `playlist_id` and `playlist_name`
   refer to the playlist the video was added to first, `all_playlist_names` lists every
   playlist, and `playlist_count` gives the total. These fields are empty for videos in no
   public playlist.
8. **Video types.** Shorts, completed live streams and premieres are all included, because
   they appear in a channel's uploads. Scheduled videos appear once they become public.
9. **Time zones.** All timestamps are stored in UTC. A Sri Lanka (`Asia/Colombo`) publish
   time is provided for readability.

### Access

10. **Public data only.** Data is accessed with an API key (no OAuth). Channels are configured
    by ID (`UC…`) or `@handle`, and the dashboard can be filtered by channel.

## Limitations

### Data availability

1. **No true "views in the last 24 hours".** That metric requires the YouTube *Analytics* API
   with channel-owner OAuth. Day-over-day deltas are available from the second daily run onward.
2. **History starts at the first run.** The API provides no historical counters, so earlier
   days cannot be backfilled. The first run does collect all uploads from the lookback window.
3. **Hidden counters.** `like_count` is `NULL` when likes are hidden and `comment_count` is
   `NULL` when comments are disabled. Private, unlisted, deleted and scheduled videos are not
   visible to an API key.
4. **Live streams** report a duration of 0 while live. The duration updates once the stream ends.

### Scale and quota

5. **Quota.** The default allowance is 10,000 units per day. A typical channel uses roughly
   5–60 units per run, and a high-volume news channel a few hundred. Dozens of channels fit
   within the default quota, although channels with many playlists reduce that number.
6. **High-volume channels.** Up to `MAX_UPLOAD_PAGES` × 50 uploads (5,000 by default) are
   collected per channel per window. Beyond that, the run is marked `partial` with an explicit
   message and is never silently truncated.
7. **Playlist mapping is capped** by `MAX_PLAYLISTS_PER_CHANNEL` and `MAX_PAGES_PER_PLAYLIST`
   to protect quota. Videos deep inside very large playlists may show no playlist. Mapping
   can be disabled with `FETCH_PLAYLISTS=false`.

### Operations

8. **Run outcomes.** Load, transform and run status are committed in a single transaction. A
   `failed` run (quota exceeded, database unavailable) writes only its `failed` entry in
   `core.pipeline_run` and can simply be re-run. If some channels fail and others succeed,
   the successful channels are saved, the run is marked `partial` with the reason, and the
   CLI exits with code 4 so the scheduler flags it. A deleted playlist produces a warning only.
9. **Raw retention.** Raw API JSON is purged after `RAW_RETENTION_DAYS` (default 90) to stay
   within free-tier storage limits. Daily history in `core` is kept indefinitely.
   `retransform` therefore works only for runs within the retention period.
10. **Scheduling.** GitHub Actions cron jobs can start a few minutes late. GitHub also disables
    scheduled workflows in public repositories after 60 days without repository activity;
    they can be re-enabled from the Actions tab.

### Infrastructure

11. **Database connection.** The pipeline requires a direct (non-pooled) PostgreSQL
    connection, because transaction-mode poolers such as Neon's `-pooler` host break the run
    lock. It refuses to start on a pooled connection and explains why.
12. **Internet-reachable database.** Looker Studio connects from Google's cloud, so a
    `localhost` database cannot be used directly. A hosted database such as Neon or Supabase
    is required. The Looker Studio PostgreSQL connector returns at most 150,000 rows per query,
    well above this dataset's size.
13. **Hosting.** The pipeline runs on GitHub Actions and stores data in Neon PostgreSQL. A
    move to Google Cloud Run and Cloud Scheduler would need only a small container wrapper,
    because all configuration comes from environment variables.

### Testing

14. **Fake API in automated tests.** The automated tests replace the YouTube API with an
    in-memory fake, so CI needs no key or network access. Behaviour against the live API is
    checked with `python -m yt_pipeline verify --deep` and the procedures in
    [TESTING.md](TESTING.md). A change in the API's response format would surface as a failed
    run rather than a failing test.
