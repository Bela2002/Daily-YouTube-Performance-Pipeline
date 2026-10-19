-- =====================================================================
-- TRANSFORM step (the "T" in ELT).
-- Reads the raw JSON stored for one pipeline run and upserts it into the
-- typed core tables. The run is selected with:
--     SELECT set_config('pipeline.run_id', '<uuid>', true);
-- which the Python code sets in the same transaction before running this.
-- Everything here is idempotent: re-running a run gives the same result.
-- =====================================================================

-- 1) Channels ---------------------------------------------------------
INSERT INTO core.channel AS c (channel_id, channel_name, custom_url, uploads_playlist_id, updated_at)
SELECT DISTINCT ON (item ->> 'id')
       item ->> 'id',
       item -> 'snippet' ->> 'title',
       item -> 'snippet' ->> 'customUrl',
       item -> 'contentDetails' -> 'relatedPlaylists' ->> 'uploads',
       now()
FROM raw.api_response r
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.payload -> 'items', '[]'::jsonb)) AS item
WHERE r.run_id = current_setting('pipeline.run_id')::uuid
  AND r.endpoint = 'channels'
ORDER BY item ->> 'id', r.fetched_at DESC
ON CONFLICT (channel_id) DO UPDATE
SET channel_name        = EXCLUDED.channel_name,
    custom_url          = EXCLUDED.custom_url,
    uploads_playlist_id = EXCLUDED.uploads_playlist_id,
    updated_at          = now();

-- 2) Playlists (excluding the hidden "uploads" playlist) --------------
INSERT INTO core.playlist AS p (playlist_id, channel_id, playlist_name, item_count, updated_at)
SELECT DISTINCT ON (item ->> 'id')
       item ->> 'id',
       item -> 'snippet' ->> 'channelId',
       item -> 'snippet' ->> 'title',
       NULLIF(item -> 'contentDetails' ->> 'itemCount', '')::int,
       now()
FROM raw.api_response r
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.payload -> 'items', '[]'::jsonb)) AS item
WHERE r.run_id = current_setting('pipeline.run_id')::uuid
  AND r.endpoint = 'playlists'
  AND EXISTS (SELECT 1 FROM core.channel ch WHERE ch.channel_id = item -> 'snippet' ->> 'channelId')
ORDER BY item ->> 'id', r.fetched_at DESC
ON CONFLICT (playlist_id) DO UPDATE
SET playlist_name = EXCLUDED.playlist_name,
    item_count    = EXCLUDED.item_count,
    updated_at    = now();

-- 3) Videos (metadata) -------------------------------------------------
INSERT INTO core.video AS v (video_id, channel_id, video_title, published_at,
                             duration_iso, duration_seconds, live_broadcast, updated_at)
SELECT DISTINCT ON (item ->> 'id')
       item ->> 'id',
       item -> 'snippet' ->> 'channelId',
       item -> 'snippet' ->> 'title',
       (item -> 'snippet' ->> 'publishedAt')::timestamptz,
       item -> 'contentDetails' ->> 'duration',
       core.iso8601_to_seconds(item -> 'contentDetails' ->> 'duration'),
       item -> 'snippet' ->> 'liveBroadcastContent',
       now()
FROM raw.api_response r
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.payload -> 'items', '[]'::jsonb)) AS item
WHERE r.run_id = current_setting('pipeline.run_id')::uuid
  AND r.endpoint = 'videos'
  AND EXISTS (SELECT 1 FROM core.channel ch WHERE ch.channel_id = item -> 'snippet' ->> 'channelId')
ORDER BY item ->> 'id', r.fetched_at DESC
ON CONFLICT (video_id) DO UPDATE
SET video_title      = EXCLUDED.video_title,
    published_at     = EXCLUDED.published_at,
    duration_iso     = EXCLUDED.duration_iso,
    duration_seconds = EXCLUDED.duration_seconds,
    live_broadcast   = EXCLUDED.live_broadcast,
    updated_at       = now();

-- 4) Video <-> playlist membership --------------------------------------
INSERT INTO core.video_playlist AS vp (video_id, playlist_id, added_at, last_seen_run)
SELECT DISTINCT ON (item -> 'contentDetails' ->> 'videoId', r.context ->> 'playlist_id')
       item -> 'contentDetails' ->> 'videoId',
       r.context ->> 'playlist_id',
       (item -> 'snippet' ->> 'publishedAt')::timestamptz,
       r.run_id
FROM raw.api_response r
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.payload -> 'items', '[]'::jsonb)) AS item
WHERE r.run_id = current_setting('pipeline.run_id')::uuid
  AND r.endpoint = 'playlistItems'
  AND r.context ->> 'kind' = 'playlist'
  AND EXISTS (SELECT 1 FROM core.video v  WHERE v.video_id = item -> 'contentDetails' ->> 'videoId')
  AND EXISTS (SELECT 1 FROM core.playlist p WHERE p.playlist_id = r.context ->> 'playlist_id')
ORDER BY item -> 'contentDetails' ->> 'videoId', r.context ->> 'playlist_id', r.fetched_at DESC
ON CONFLICT (video_id, playlist_id) DO UPDATE
SET added_at      = EXCLUDED.added_at,
    last_seen_run = EXCLUDED.last_seen_run;

-- 5) Daily performance snapshot ------------------------------------------
INSERT INTO core.video_daily_snapshot AS s (video_id, capture_date, capture_ts_utc,
                                            view_count, like_count, comment_count, run_id)
SELECT DISTINCT ON (item ->> 'id')
       item ->> 'id',
       pr.capture_date,
       r.fetched_at,
       NULLIF(item -> 'statistics' ->> 'viewCount', '')::bigint,
       NULLIF(item -> 'statistics' ->> 'likeCount', '')::bigint,
       NULLIF(item -> 'statistics' ->> 'commentCount', '')::bigint,
       r.run_id
FROM raw.api_response r
JOIN core.pipeline_run pr ON pr.run_id = r.run_id
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.payload -> 'items', '[]'::jsonb)) AS item
WHERE r.run_id = current_setting('pipeline.run_id')::uuid
  AND r.endpoint = 'videos'
  AND EXISTS (SELECT 1 FROM core.video v WHERE v.video_id = item ->> 'id')
ORDER BY item ->> 'id', r.fetched_at DESC
ON CONFLICT (video_id, capture_date) DO UPDATE
SET capture_ts_utc = EXCLUDED.capture_ts_utc,
    view_count     = EXCLUDED.view_count,
    like_count     = EXCLUDED.like_count,
    comment_count  = EXCLUDED.comment_count,
    run_id         = EXCLUDED.run_id;
