# Building the Looker Studio dashboard

The pipeline exposes four ready-made views in the `reporting` schema. The dashboard
only reads these views, so all business logic (time windows, ranks, playlist
selection) lives in SQL and is version-controlled.

| View | Grain | Used for |
|---|---|---|
| `reporting.video_by_window` | one row per video **per time window** it falls in | Top-videos tables, "uploaded in last 24h" list |
| `reporting.upload_summary` | channel × time window (zero rows filled in) | Upload count scorecards |
| `reporting.video_daily_dataset` | video × capture date | Full required dataset, trends, export |
| `reporting.pipeline_runs` | one row per pipeline run | "Data as of" / freshness |

> Time windows are measured back from the start of the **latest successful run**
> (`reference_ts_utc`), not from the moment someone opens the report. This keeps the
> numbers consistent with the data that was actually collected.

---

## 1. Make the database reachable from Looker Studio

Looker Studio runs in Google's cloud, so it cannot see `localhost`. Use a hosted
PostgreSQL. This project uses **Neon** (free tier).

If you followed the README, this is already done: the pipeline writes to Neon, and
`python -m yt_pipeline create-reader` created the read-only `looker_reader` login. It printed
the host, port and database name you need below.

Download Let's Encrypt's root certificate **`isrgrootx1.pem`**
(https://letsencrypt.org/certs/isrgrootx1.pem). Looker Studio needs it to use SSL with Neon.

## 2. Add the data sources

For each of the four views:

1. Looker Studio → **Create → Data source → PostgreSQL**.
2. **Basic**: host (e.g. `ep-xxxx-xxxx.ap-southeast-1.aws.neon.tech`), port `5432`,
   database (`neondb`), username `looker_reader`, and the password you chose. Never use the
   owner login here.
   *If Looker says "endpoint ID is not specified"*, use the pooled host instead (the same host
   with `-pooler` after the first part). Pooling is fine for Looker, because it only reads.
3. Tick **Enable SSL** and upload `isrgrootx1.pem` as the server certificate.
4. Choose **Custom query** and enter, e.g. `SELECT * FROM reporting.video_by_window`
   (custom query avoids any schema-listing issues).
5. **Connect**, then check field types:
   * `published_at_utc`, `data_capture_ts_utc`, `reference_ts_utc` → *Date & Time*
   * `published_date_utc`, `data_capture_date` → *Date*
   * `video_url` → *URL*; `video_title` can be a *Hyperlink* calculated field:
     `HYPERLINK(video_url, video_title)`
   * `engagement_rate` → *Percent*
6. For `video_by_window`, set **Date range dimension** = `published_date_utc`.
7. Set **Data freshness** to 12 hours (data changes once a day).

## 3. Page layout (one page is enough)

```
┌───────────────────────────────────────────────────────────────────────────┐
│ YouTube Daily Performance                      Data as of: 2026-10-18 00:30│
│ [Channel ▼]   [Date range: published ▼]                                    │
├──────────────┬──────────────┬─────────────────────────────────────────────┤
│ Uploads 24h  │ Uploads 7d   │ Uploads 28d                                  │
│     3        │     16       │     58                                       │
├──────────────┴──────────────┴─────────────────────────────────────────────┤
│ Uploaded in the last 24 hours (early performance)                          │
│ Title | Channel | Published (SL) | Hours live | Duration | Views | Likes | Comments│
├───────────────────────────────────────────────────────────────────────────┤
│ Top performing videos            [Time window: Last 24h ▼]                 │
│ Rank | Title | Channel | Playlist | Published | Views | Likes | Comments    │
├───────────────────────────────────────────────────────────────────────────┤
│ Uploads per day (last 28 days) – column chart by channel                   │
└───────────────────────────────────────────────────────────────────────────┘
```

### Header
* **Text**: "YouTube Daily Performance".
* **Scorecard** (`video_by_window`): metric `MAX(reference_ts_utc)`, label "Data as of (UTC)".

### Controls (page-wide)
* **Drop-down list**: control field `channel_name` → filter by channel.
* **Date range control**: set the default to **Advanced → start: Today − 90 days,
  end: Today**. (Looker's "Auto"/"Last 28 days" presets exclude *today*, which would
  hide the newest uploads.)

### Upload counts (requirement: 24h / 7d / 28d)
Three **Scorecards** on `upload_summary`, metric `SUM(videos_uploaded)`, each with a chart
filter: `time_window = Last 24 hours` / `Last 7 days` / `Last 28 days`.
`upload_summary` returns 0 instead of "No data" when nothing was published.

For the channel drop-down to filter these too, both data sources must use the same field
ID for `channel_name` (they do by default with PostgreSQL; if not, use **Resource →
Manage field names and IDs** to give them the same ID).

### "What was uploaded in the last 24 hours"
**Table** on `video_by_window`, chart filter `time_window = Last 24 hours`.
Dimensions: `video_title` (hyperlink), `channel_name`, `playlist_name`,
`published_at_colombo`, `hours_since_publish`, `video_duration`.
Metrics: `view_count`, `like_count`, `comment_count`, `engagement_rate`.
Sort: `published_at_utc` descending.

### Top performing videos per window (sortable)
1. **Table** on `video_by_window`: dimensions `video_title`, `channel_name`,
   `playlist_name`, `published_date_utc`, `video_duration`; metrics `view_count`,
   `like_count`, `comment_count`, `views_gained_since_prev_capture`.
   Default sort `view_count` descending, rows per page 25.
   Users click any column header to re-sort by views, likes or comments.
2. **Drop-down list** with control field `time_window`, single select, default
   selection "Last 24 hours".
3. Select the table **and** this drop-down → right-click → **Group**. Grouped controls only
   affect charts in their group, so the scorecards above are not filtered by it.

### Trend (nice to have)
**Column chart** on `video_by_window` with filter `time_window = Last 28 days`:
dimension `published_date_utc`, breakdown `channel_name`, metric `COUNT_DISTINCT(video_id)`.

## 4. Share
**Share → Manage access** (viewer), or **File → Download → PDF** for a screenshot to
attach to the submission.
