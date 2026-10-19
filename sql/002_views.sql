-- =====================================================================
-- REPORTING views for Looker Studio.
-- Timestamps are exposed as plain TIMESTAMP in UTC (Looker Studio reads
-- these cleanly), plus a Sri Lanka local-time column for convenience.
-- Flags are 0/1 integers so they can be SUMmed in scorecards.
-- =====================================================================

DROP VIEW IF EXISTS reporting.pipeline_runs        CASCADE;
DROP VIEW IF EXISTS reporting.upload_summary       CASCADE;
DROP VIEW IF EXISTS reporting.video_by_window      CASCADE;
DROP VIEW IF EXISTS reporting.video_latest         CASCADE;
DROP VIEW IF EXISTS reporting.video_daily_dataset  CASCADE;

-- ---------------------------------------------------------------------
-- 1) The required daily dataset: one row per video per capture day,
--    with every field listed in the brief.
-- ---------------------------------------------------------------------
CREATE VIEW reporting.video_daily_dataset AS
WITH playlist_info AS (
    SELECT vp.video_id,
           -- "primary" playlist = the first playlist the video was added to
           (array_agg(p.playlist_id   ORDER BY vp.added_at NULLS LAST, p.playlist_name))[1] AS playlist_id,
           (array_agg(p.playlist_name ORDER BY vp.added_at NULLS LAST, p.playlist_name))[1] AS playlist_name,
           count(*)                                                           AS playlist_count,
           string_agg(p.playlist_name, ' | ' ORDER BY p.playlist_name)        AS all_playlist_names
    FROM core.video_playlist vp
    JOIN core.playlist p ON p.playlist_id = vp.playlist_id
    GROUP BY vp.video_id
)
SELECT
    v.video_id,
    v.video_title,
    c.channel_name,
    c.channel_id,
    pl.playlist_id,                                             -- NULL when not in any playlist
    pl.playlist_name,
    COALESCE(pl.playlist_count, 0)                              AS playlist_count,
    pl.all_playlist_names,
    (v.published_at AT TIME ZONE 'UTC')                         AS published_at_utc,
    (v.published_at AT TIME ZONE 'UTC')::date                   AS published_date_utc,
    (v.published_at AT TIME ZONE 'Asia/Colombo')                AS published_at_colombo,
    v.duration_seconds                                          AS video_duration_seconds,
    core.format_duration(v.duration_seconds)                    AS video_duration,
    v.duration_iso                                              AS video_duration_iso,
    s.like_count,
    s.view_count,
    s.comment_count,
    s.capture_date                                              AS data_capture_date,
    (s.capture_ts_utc AT TIME ZONE 'UTC')                       AS data_capture_ts_utc,
    -- extras that help analysis
    s.view_count - LAG(s.view_count) OVER w                     AS views_gained_since_prev_capture,
    CASE WHEN s.view_count > 0
         THEN round((COALESCE(s.like_count, 0) + COALESCE(s.comment_count, 0))::numeric / s.view_count, 4)
    END                                                         AS engagement_rate,
    'https://www.youtube.com/watch?v=' || v.video_id            AS video_url
FROM core.video_daily_snapshot s
JOIN core.video   v ON v.video_id   = s.video_id
JOIN core.channel c ON c.channel_id = v.channel_id
LEFT JOIN playlist_info pl ON pl.video_id = v.video_id
WINDOW w AS (PARTITION BY s.video_id ORDER BY s.capture_date);

-- ---------------------------------------------------------------------
-- 2) Latest known stats per video + time-window flags.
--    Windows are measured back from the start of the latest successful
--    (or partial) run (the same "as of" time the extractor used for its cutoff), so the
--    dashboard always agrees with the data that was actually collected,
--    no matter when someone opens it.
-- ---------------------------------------------------------------------
CREATE VIEW reporting.video_latest AS
WITH ref AS (
    SELECT max(started_at) AT TIME ZONE 'UTC' AS reference_ts_utc
    FROM core.pipeline_run
    WHERE status IN ('success', 'partial')
),
latest AS (
    SELECT DISTINCT ON (d.video_id) d.*
    FROM reporting.video_daily_dataset d
    ORDER BY d.video_id, d.data_capture_date DESC
)
SELECT
    l.*,
    ref.reference_ts_utc,
    round(EXTRACT(EPOCH FROM (ref.reference_ts_utc - l.published_at_utc)) / 3600.0, 1) AS hours_since_publish,
    (l.published_at_utc >= ref.reference_ts_utc - INTERVAL '24 hours')::int AS is_last_24h,
    (l.published_at_utc >= ref.reference_ts_utc - INTERVAL '7 days')::int   AS is_last_7d,
    (l.published_at_utc >= ref.reference_ts_utc - INTERVAL '28 days')::int  AS is_last_28d
FROM latest l
CROSS JOIN ref;

-- ---------------------------------------------------------------------
-- 3) Long format: one row per video per time window it falls into.
--    In Looker Studio a single "Time window" dropdown then drives every
--    table/chart, and you can sort by views, likes or comments.
-- ---------------------------------------------------------------------
CREATE VIEW reporting.video_by_window AS
WITH windows (time_window, window_order, window_hours) AS (
    VALUES ('Last 24 hours', 1, 24),
           ('Last 7 days',   2, 168),
           ('Last 28 days',  3, 672)
)
SELECT
    w.time_window,
    w.window_order,
    l.video_id,
    l.video_title,
    l.channel_name,
    l.channel_id,
    l.playlist_id,
    l.playlist_name,
    l.all_playlist_names,
    l.published_at_utc,
    l.published_date_utc,
    l.published_at_colombo,
    l.video_duration,
    l.video_duration_seconds,
    l.view_count,
    l.like_count,
    l.comment_count,
    l.engagement_rate,
    l.views_gained_since_prev_capture,
    l.hours_since_publish,
    l.data_capture_date,
    l.data_capture_ts_utc,
    l.reference_ts_utc,
    l.video_url,
    RANK() OVER (PARTITION BY w.time_window ORDER BY l.view_count DESC NULLS LAST)                 AS views_rank_all_channels,
    RANK() OVER (PARTITION BY w.time_window, l.channel_id ORDER BY l.view_count DESC NULLS LAST)   AS views_rank_in_channel
FROM reporting.video_latest l
JOIN windows w
  ON l.published_at_utc >= l.reference_ts_utc - make_interval(hours => w.window_hours);

-- ---------------------------------------------------------------------
-- 4) Upload counts per channel per window (scorecards / summary table).
--    Windows with zero uploads still appear (count = 0).
-- ---------------------------------------------------------------------
CREATE VIEW reporting.upload_summary AS
WITH windows (time_window, window_order) AS (
    VALUES ('Last 24 hours', 1), ('Last 7 days', 2), ('Last 28 days', 3)
)
SELECT
    w.time_window,
    w.window_order,
    c.channel_id,
    c.channel_name,
    COUNT(b.video_id)                    AS videos_uploaded,
    COALESCE(SUM(b.view_count), 0)       AS total_views,
    COALESCE(SUM(b.like_count), 0)       AS total_likes,
    COALESCE(SUM(b.comment_count), 0)    AS total_comments
FROM windows w
CROSS JOIN core.channel c
LEFT JOIN reporting.video_by_window b
       ON b.time_window = w.time_window AND b.channel_id = c.channel_id
GROUP BY w.time_window, w.window_order, c.channel_id, c.channel_name;

-- ---------------------------------------------------------------------
-- 5) Pipeline run log (data freshness panel in the dashboard).
-- ---------------------------------------------------------------------
CREATE VIEW reporting.pipeline_runs AS
SELECT
    run_id::text                                   AS run_id,
    capture_date,
    started_at  AT TIME ZONE 'UTC'                 AS started_at_utc,
    finished_at AT TIME ZONE 'UTC'                 AS finished_at_utc,
    status,
    array_to_string(channels_input, ', ')          AS channels_input,
    lookback_days,
    api_requests,
    quota_units_used,
    videos_captured,
    error_message
FROM core.pipeline_run;
