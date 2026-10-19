-- =====================================================================
-- Daily YouTube Performance Pipeline - schema
-- Layers:
--   raw        -> untouched JSON responses from YouTube Data API v3 (the "L" in ELT)
--   core       -> cleaned, typed, de-duplicated tables built in SQL (the "T")
--   reporting  -> views shaped for Looker Studio (see 002_views.sql)
-- Safe to run repeatedly (idempotent).
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS reporting;

-- ---------------------------------------------------------------------
-- Pipeline run log: one row per execution, used for auditing and to
-- anchor the "last 24h / 7d / 28d" windows to the latest capture time.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.pipeline_run (
    run_id            UUID PRIMARY KEY,
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    capture_date      DATE        NOT NULL,           -- UTC date of the run
    status            TEXT        NOT NULL,
    channels_input    TEXT[]      NOT NULL,
    lookback_days     INTEGER     NOT NULL,
    api_requests      INTEGER     NOT NULL DEFAULT 0,
    quota_units_used  INTEGER     NOT NULL DEFAULT 0,
    videos_captured   INTEGER,
    error_message     TEXT                            -- failure reason, or warnings for partial runs
);
-- (re)defined separately so re-running this file upgrades older databases
ALTER TABLE core.pipeline_run DROP CONSTRAINT IF EXISTS pipeline_run_status_check;
ALTER TABLE core.pipeline_run ADD CONSTRAINT pipeline_run_status_check
    CHECK (status IN ('running', 'success', 'partial', 'failed'));

-- ---------------------------------------------------------------------
-- RAW: every API response page is stored as-is (JSONB).
-- Lets us re-run transformations without spending API quota again.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.api_response (
    response_id     BIGSERIAL   PRIMARY KEY,
    run_id          UUID        NOT NULL REFERENCES core.pipeline_run (run_id) ON DELETE CASCADE,
    endpoint        TEXT        NOT NULL,             -- channels | playlistItems | videos | playlists
    request_params  JSONB       NOT NULL,             -- API key is never stored
    context         JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"playlist_id": "..."}
    payload         JSONB       NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_api_response_run_endpoint ON raw.api_response (run_id, endpoint);

-- ---------------------------------------------------------------------
-- CORE dimensions
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.channel (
    channel_id           TEXT PRIMARY KEY,
    channel_name         TEXT        NOT NULL,
    custom_url           TEXT,
    uploads_playlist_id  TEXT,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.playlist (
    playlist_id    TEXT PRIMARY KEY,
    channel_id     TEXT        NOT NULL REFERENCES core.channel (channel_id),
    playlist_name  TEXT        NOT NULL,
    item_count     INTEGER,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.video (
    video_id          TEXT PRIMARY KEY,
    channel_id        TEXT        NOT NULL REFERENCES core.channel (channel_id),
    video_title       TEXT        NOT NULL,
    published_at      TIMESTAMPTZ NOT NULL,
    duration_iso      TEXT,                           -- e.g. PT12M34S (as returned by the API)
    duration_seconds  INTEGER,
    live_broadcast    TEXT,                           -- none | live | upcoming
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_video_channel_published ON core.video (channel_id, published_at DESC);

-- A video can sit in zero, one or many playlists -> bridge table.
CREATE TABLE IF NOT EXISTS core.video_playlist (
    video_id       TEXT        NOT NULL REFERENCES core.video (video_id),
    playlist_id    TEXT        NOT NULL REFERENCES core.playlist (playlist_id),
    added_at       TIMESTAMPTZ,                       -- when the video was added to the playlist
    last_seen_run  UUID        NOT NULL,
    PRIMARY KEY (video_id, playlist_id)
);

-- ---------------------------------------------------------------------
-- CORE fact: one row per video per capture day.
-- Re-running on the same UTC day overwrites that day's row (idempotent).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.video_daily_snapshot (
    video_id        TEXT        NOT NULL REFERENCES core.video (video_id),
    capture_date    DATE        NOT NULL,
    capture_ts_utc  TIMESTAMPTZ NOT NULL,
    view_count      BIGINT,                           -- NULL only if the API hides it
    like_count      BIGINT,                           -- NULL when likes are hidden
    comment_count   BIGINT,                           -- NULL when comments are disabled
    run_id          UUID        NOT NULL REFERENCES core.pipeline_run (run_id),
    PRIMARY KEY (video_id, capture_date)
);
CREATE INDEX IF NOT EXISTS ix_snapshot_capture_date ON core.video_daily_snapshot (capture_date);

-- ---------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------
-- YouTube returns durations as ISO-8601 (PT1H2M3S). PostgreSQL parses that
-- natively; this wrapper returns NULL instead of failing on odd values.
CREATE OR REPLACE FUNCTION core.iso8601_to_seconds(d TEXT)
RETURNS INTEGER
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF d IS NULL OR d = '' THEN
        RETURN NULL;
    END IF;
    RETURN EXTRACT(EPOCH FROM d::interval)::INTEGER;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$;

-- Formats seconds as H:MM:SS (or M:SS) for display in the dashboard.
CREATE OR REPLACE FUNCTION core.format_duration(s INTEGER)
RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN s IS NULL THEN NULL
        WHEN s >= 3600 THEN format('%s:%s:%s', s / 3600, lpad(((s % 3600) / 60)::text, 2, '0'), lpad((s % 60)::text, 2, '0'))
        ELSE format('%s:%s', s / 60, lpad((s % 60)::text, 2, '0'))
    END
$$;
