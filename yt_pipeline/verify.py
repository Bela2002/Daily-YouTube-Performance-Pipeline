"""Live verification against the REAL YouTube API and the REAL database.

    python -m yt_pipeline verify          # ~5-10 quota units
    python -m yt_pipeline verify --deep   # + completeness cross-check (100 units per search page)

Run it after `python -m yt_pipeline run`. It answers four questions:
  1. Does the API key work, and does every configured channel resolve?
  2. Did the pipeline collect EVERY public video in the window?   (--deep)
  3. Is the stored data complete, fresh and consistent with YouTube right now?
  4. Are secrets safe (key not in DB or git history, SSL on, read-only dashboard user)?
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg

from . import db
from .config import Settings
from .extract import chunked
from .youtube_client import YouTubeAPIError, YouTubeClient

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str = ""


class Verifier:
    def __init__(self, settings: Settings, client: YouTubeClient | None = None,
                 deep: bool = False, repo_dir: Path | None = None, max_search_pages: int = 10):
        self.s = settings
        self.client = client or YouTubeClient(settings.youtube_api_key)
        self.deep = deep
        self.repo_dir = repo_dir
        self.max_search_pages = max_search_pages
        self.checks: list[Check] = []
        self.channels: dict[str, dict[str, Any]] = {}   # channel_id -> API item

    def add(self, area: str, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(area, name, status, self.client.redact(detail)))

    # ------------------------------------------------------------------
    def run(self) -> list[Check]:
        self.check_api()
        try:
            conn = db.connect(self.s.database_url, attempts=1)
        except psycopg.Error as exc:
            self.add("Database", "connect", FAIL, str(exc).strip().splitlines()[0])
            self.check_git()
            return self.checks
        with conn:
            if self.check_schema(conn):
                run = self.check_latest_run(conn)
                self.check_channels_in_db(conn)
                if run:
                    if self.deep:
                        self.check_completeness(conn, run)
                    self.check_data_quality(conn, run)
                    self.check_live_values(conn)
                self.check_secrets_in_db(conn)
                self.check_reader_role(conn)
            self.check_ssl(conn)
        self.check_git()
        return self.checks

    # 1. API ---------------------------------------------------------------
    def check_api(self) -> None:
        ids = [c for c in self.s.channels if not c.startswith("@")]
        handles = [c for c in self.s.channels if c.startswith("@")]
        try:
            for batch in chunked(ids):
                items = self.client.get("channels", {"part": "snippet,statistics", "id": ",".join(batch)}).get("items", [])
                found = {i["id"]: i for i in items}
                self.channels.update(found)
                for cid in batch:
                    if cid not in found:
                        self.add("API", f"channel {cid}", FAIL, "not found / not public")
            for handle in handles:
                items = self.client.get("channels", {"part": "snippet,statistics", "forHandle": handle}).get("items", [])
                if items:
                    self.channels[items[0]["id"]] = items[0]
                else:
                    self.add("API", f"channel {handle}", FAIL, "handle not found")
        except YouTubeAPIError as exc:
            self.add("API", "API key works", FAIL, str(exc))
            return
        self.add("API", "API key works", PASS, "authenticated via X-Goog-Api-Key header")
        for cid, item in self.channels.items():
            stats = item.get("statistics", {})
            self.add("API", f"channel {item['snippet']['title']}", PASS,
                     f"{cid}, {stats.get('videoCount', '?')} public videos in total")

    # 2. Database ----------------------------------------------------------
    def check_schema(self, conn: psycopg.Connection) -> bool:
        missing = [t for t in ("core.pipeline_run", "core.video_daily_snapshot", "raw.api_response")
                   if conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is None]
        views = conn.execute("SELECT count(*) FROM information_schema.views WHERE table_schema='reporting'").fetchone()[0]
        if missing or views < 5:
            self.add("Database", "schema + views", FAIL, f"missing {missing or 'reporting views'} - run init-db")
            return False
        self.add("Database", "schema + views", PASS, "raw / core tables and 5 reporting views exist")
        return True

    def check_latest_run(self, conn) -> dict[str, Any] | None:
        row = conn.execute(
            """SELECT run_id, started_at, status, videos_captured, quota_units_used, error_message
                 FROM core.pipeline_run ORDER BY started_at DESC LIMIT 1""").fetchone()
        if not row:
            self.add("Pipeline", "latest run", FAIL, "no runs yet - run `python -m yt_pipeline run`")
            return None
        run_id, started, status, videos, quota, err = row
        age_h = (datetime.now(timezone.utc) - started).total_seconds() / 3600
        detail = f"{status}, {videos} videos, {quota} quota units, {age_h:.1f}h ago"
        level = {"success": PASS, "partial": WARN}.get(status, FAIL)
        self.add("Pipeline", "latest run", level, detail + (f" | {err[:200]}" if err else ""))
        if age_h > 26:
            self.add("Pipeline", "freshness", WARN, f"latest run is {age_h:.0f}h old - is the daily schedule running?")
        good = conn.execute(
            """SELECT run_id, started_at FROM core.pipeline_run WHERE status IN ('success','partial')
               ORDER BY started_at DESC LIMIT 1""").fetchone()
        if not good:
            return None
        return {"run_id": str(good[0]), "started_at": good[1]}

    def check_channels_in_db(self, conn) -> None:
        stored = {r[0] for r in conn.execute("SELECT channel_id FROM core.channel")}
        for cid, item in self.channels.items():
            ok = cid in stored
            self.add("Pipeline", f"stored channel {item['snippet']['title']}", PASS if ok else FAIL,
                     "" if ok else "configured but not in core.channel - did the run include it?")

    def check_completeness(self, conn, run: dict[str, Any]) -> None:
        """Independent cross-check: ask YouTube's *search* endpoint for every
        video a channel published in the window, and compare with what we stored."""
        end = run["started_at"]
        start = end - timedelta(days=self.s.lookback_days)
        for cid, item in self.channels.items():
            name = item["snippet"]["title"]
            searched: set[str] = set()
            params = {"part": "id", "channelId": cid, "type": "video", "order": "date", "maxResults": 50,
                      "publishedAfter": _rfc3339(start), "publishedBefore": _rfc3339(end)}
            try:
                for page in self.client.paginate("search", params, max_pages=self.max_search_pages):
                    searched |= {i["id"]["videoId"] for i in page.get("items", []) if i.get("id", {}).get("videoId")}
            except YouTubeAPIError as exc:
                self.add("Completeness", name, WARN, f"cross-check skipped: {exc}")
                continue
            stored = {r[0] for r in conn.execute(
                "SELECT video_id FROM core.video WHERE channel_id=%s AND published_at >= %s AND published_at < %s",
                (cid, start, end))}
            missing = searched - stored
            if missing:   # ignore scheduled premieres/streams: they are not published yet
                info = self.client.get("videos", {"part": "snippet", "id": ",".join(sorted(missing)[:50])})
                upcoming = {v["id"] for v in info.get("items", [])
                            if v["snippet"].get("liveBroadcastContent") == "upcoming"}
                missing -= upcoming
            if missing:
                self.add("Completeness", name, FAIL,
                         f"{len(missing)} video(s) found by YouTube search but NOT stored: {', '.join(sorted(missing)[:5])}")
            else:
                note = f"all {len(searched)} videos YouTube search found are stored ({len(stored)} stored)"
                if len(stored) > len(searched):
                    note += " - search returns fewer; this is normal, it is known to omit some videos"
                self.add("Completeness", name, PASS, note)

    def check_data_quality(self, conn, run: dict[str, Any]) -> None:
        latest = conn.execute("SELECT max(data_capture_date) FROM reporting.video_daily_dataset").fetchone()[0]
        if latest is None:
            self.add("Data", "rows captured", WARN, "no videos in the window (channels may not have uploaded)")
            return
        r = conn.execute("""
            SELECT count(*),
                   count(*) FILTER (WHERE video_id IS NULL OR video_title IS NULL OR channel_name IS NULL
                                    OR channel_id IS NULL OR published_at_utc IS NULL
                                    OR data_capture_ts_utc IS NULL),
                   count(*) FILTER (WHERE video_duration IS NULL),
                   count(*) FILTER (WHERE view_count IS NULL),
                   count(*) FILTER (WHERE like_count IS NULL),
                   count(*) FILTER (WHERE comment_count IS NULL),
                   count(*) FILTER (WHERE playlist_id IS NOT NULL),
                   count(*) FILTER (WHERE views_gained_since_prev_capture < 0)
              FROM reporting.video_daily_dataset WHERE data_capture_date = %s""", (latest,)).fetchone()
        total, bad_required, no_duration, no_views, no_likes, no_comments, with_pl, views_down = r
        self.add("Data", f"rows captured on {latest}", PASS, f"{total} videos")
        self.add("Data", "required fields filled", FAIL if bad_required else PASS,
                 f"{bad_required} rows missing ID/title/channel/publish time/capture time" if bad_required else
                 "video ID, title, channel, publish time, capture date+timestamp present in every row")
        self.add("Data", "duration + view count", WARN if (no_duration or no_views) else PASS,
                 f"{no_duration} without duration, {no_views} without views")
        self.add("Data", "hidden likes / disabled comments", INFO,
                 f"{no_likes} rows with likes hidden, {no_comments} with comments off (stored as NULL)")
        self.add("Data", "playlist coverage", INFO, f"{with_pl} of {total} videos are in a public playlist")
        if views_down:
            self.add("Data", "view counts went down", WARN,
                     f"{views_down} videos have fewer views than yesterday (YouTube removes spam views - usually fine)")
        dup = conn.execute("""SELECT count(*) FROM (SELECT video_id, capture_date FROM core.video_daily_snapshot
                              GROUP BY 1,2 HAVING count(*) > 1) d""").fetchone()[0]
        self.add("Data", "one row per video per day", FAIL if dup else PASS, f"{dup} duplicates" if dup else "")
        for window, n in conn.execute("""SELECT time_window, sum(videos_uploaded) FROM reporting.upload_summary
                                         GROUP BY time_window, window_order ORDER BY window_order"""):
            self.add("Data", f"uploads {window.lower()}", INFO, f"{n} videos")

    def check_live_values(self, conn) -> None:
        """Compare 5 stored videos with what YouTube says right now."""
        rows = conn.execute("""SELECT video_id, video_title, channel_id, view_count FROM reporting.video_latest
                               ORDER BY view_count DESC NULLS LAST LIMIT 5""").fetchall()
        if not rows:
            return
        try:
            live = {v["id"]: v for v in self.client.get(
                "videos", {"part": "snippet,statistics", "id": ",".join(r[0] for r in rows)}).get("items", [])}
        except YouTubeAPIError as exc:
            self.add("Data", "matches YouTube right now", WARN, str(exc))
            return
        problems = []
        for vid, title, cid, views in rows:
            v = live.get(vid)
            if not v:
                problems.append(f"{vid} no longer public")
                continue
            if v["snippet"]["channelId"] != cid:
                problems.append(f"{vid} channel mismatch")
            now_views = int(v.get("statistics", {}).get("viewCount", 0) or 0)
            if views is not None and now_views < views * 0.9:
                problems.append(f"{vid} views {views} stored vs {now_views} now")
        self.add("Data", "matches YouTube right now", WARN if problems else PASS,
                 "; ".join(problems) if problems else "top 5 videos: IDs, channels and view counts agree with the live API")

    # 4. Security ----------------------------------------------------------
    def check_secrets_in_db(self, conn) -> None:
        key = self.s.youtube_api_key
        hits = conn.execute("""
            SELECT (SELECT count(*) FROM raw.api_response
                     WHERE strpos(request_params::text || context::text || payload::text, %s) > 0)
                 + (SELECT count(*) FROM core.pipeline_run WHERE strpos(coalesce(error_message, ''), %s) > 0)""",
            (key, key)).fetchone()[0]
        self.add("Security", "API key not stored in database", FAIL if hits else PASS,
                 f"found in {hits} rows - rotate the key" if hits else "checked raw responses and run log")

    def check_reader_role(self, conn) -> None:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='looker_reader'").fetchone()
        if not exists:
            self.add("Security", "read-only dashboard user", WARN,
                     "looker_reader not created - run `python -m yt_pipeline create-reader`")
            return
        r = conn.execute("""SELECT has_schema_privilege('looker_reader','reporting','USAGE'),
                                   has_schema_privilege('looker_reader','core','USAGE'),
                                   has_schema_privilege('looker_reader','raw','USAGE'),
                                   (SELECT bool_and(has_table_privilege('looker_reader', format('%I.%I', table_schema, table_name), 'SELECT'))
                                      FROM information_schema.views WHERE table_schema='reporting'),
                                   (SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname='looker_reader')""").fetchone()
        reporting, core, raw, views, powerful = r
        ok = reporting and views and not core and not raw and not powerful
        self.add("Security", "read-only dashboard user", PASS if ok else FAIL,
                 "looker_reader can read reporting views only" if ok else
                 f"reporting={reporting} views={views} core={core} raw={raw} elevated={powerful}")

    def check_ssl(self, conn) -> None:
        host = conn.info.host or ""
        if db.is_transaction_pooler(host, conn.info.port):
            self.add("Database", "direct connection", FAIL,
                     "DATABASE_URL uses a pooled host - use Neon's direct connection (no '-pooler')")
        ssl = bool(getattr(conn.pgconn, "ssl_in_use", False))
        if host in LOCAL_HOSTS or host.startswith("/"):
            self.add("Security", "database SSL", INFO, f"local database ({host or 'socket'}) - SSL not needed")
        else:
            self.add("Security", "database SSL", PASS if ssl else FAIL,
                     "connection is encrypted" if ssl else "NOT encrypted - add ?sslmode=require to DATABASE_URL")

    def check_git(self) -> None:
        if self.repo_dir is None or not (self.repo_dir / ".git").exists():
            self.add("Security", "git checks", INFO, "not a git repository - skipped")
            return

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], cwd=self.repo_dir, capture_output=True, text=True)

        try:
            tracked = git("ls-files", "--error-unmatch", ".env").returncode == 0
            in_history = bool(git("log", "--all", "--oneline", "-S", self.s.youtube_api_key).stdout.strip())
        except FileNotFoundError:
            self.add("Security", "git checks", INFO, "git not installed - skipped")
            return
        self.add("Security", ".env not committed", FAIL if tracked else PASS,
                 "run `git rm --cached .env` and rotate your secrets" if tracked else "")
        self.add("Security", "API key not in git history", FAIL if in_history else PASS,
                 "key appears in a commit - rotate it in Google Cloud now" if in_history else "")


def _rfc3339(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def print_report(checks: list[Check], client: YouTubeClient) -> int:
    area = None
    for c in checks:
        if c.area != area:
            area = c.area
            print(f"\n== {area} ==")
        print(f"  [{c.status}] {c.name}" + (f" - {c.detail}" if c.detail else ""))
    counts = {s: sum(c.status == s for c in checks) for s in (PASS, WARN, FAIL)}
    print(f"\n{counts[PASS]} passed, {counts[WARN]} warnings, {counts[FAIL]} failed "
          f"(used {client.quota_units_used} quota units)")
    return 1 if counts[FAIL] else 0
