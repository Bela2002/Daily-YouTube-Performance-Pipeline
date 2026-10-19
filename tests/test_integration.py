"""End-to-end tests against a real PostgreSQL database.

Set TEST_DATABASE_URL to an EMPTY throwaway database, e.g.
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/yt_test pytest
The tests drop and recreate the raw/core/reporting schemas.
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from tests.fake_youtube import FakeResponse, FakeYouTube, api_error
from yt_pipeline import db
from yt_pipeline.config import Settings
from yt_pipeline.pipeline import retransform, run_pipeline
from yt_pipeline.youtube_client import QuotaExceededError, YouTubeClient

DB_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

DAY1 = datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc)
DAY2 = DAY1 + timedelta(days=1)
KEY = "AIzaSy-INTEGRATION-SECRET"


@pytest.fixture()
def conn():
    # Safety: these tests DROP the schemas. Never run them against real data.
    dbname = psycopg.conninfo.conninfo_to_dict(DB_URL).get("dbname", "")
    if "test" not in dbname or DB_URL == os.getenv("DATABASE_URL"):
        pytest.exit(f"Refusing to run: TEST_DATABASE_URL must be a separate database whose name "
                    f"contains 'test' (got {dbname!r}). These tests delete all pipeline tables.", returncode=2)
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS reporting, core, raw CASCADE")
    with db.connect(DB_URL) as c:
        db.init_db(c)
        yield c


def settings(**kw):
    base = dict(youtube_api_key=KEY, database_url=DB_URL, channels=["UC_ALPHA", "@betahandle"])
    base.update(kw)
    return Settings(**base)


def run(now, view_boost=0, overrides=(), **kw):
    fake = FakeYouTube(now, view_boost=view_boost)
    fake.overrides.extend(overrides)
    client = YouTubeClient(KEY, session=fake, backoff_seconds=0, max_retries=1)
    return run_pipeline(settings(**kw), client=client, now=now)


def q(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


def test_full_run_builds_required_dataset(conn):
    s = run(DAY1)
    assert s.channels == 2
    assert s.videos_captured == 56 + 2  # alpha in-window + beta b0,b1

    cols = [d.name for d in conn.execute("SELECT * FROM reporting.video_daily_dataset LIMIT 0").description]
    for required in ["video_id", "video_title", "channel_name", "playlist_id", "playlist_name", "channel_id",
                     "published_at_utc", "video_duration", "like_count", "view_count", "comment_count",
                     "data_capture_date", "data_capture_ts_utc"]:
        assert required in cols

    # a1 is in two playlists -> primary = the one it was added to first (Headlines, 10h ago)
    row = q(conn, "SELECT playlist_id, playlist_name, playlist_count, all_playlist_names "
                  "FROM reporting.video_daily_dataset WHERE video_id='a1'")[0]
    assert row == ("PL_ALPHA_HEADLINES", "Headlines", 2, "Headlines | Sports")
    # videos with no playlist have empty playlist fields
    assert q(conn, "SELECT playlist_id, playlist_count FROM reporting.video_daily_dataset WHERE video_id='b0'")[0] == (None, 0)
    # duration parsing + formatting, hidden comments -> NULL
    assert q(conn, "SELECT video_duration, video_duration_seconds, comment_count "
                   "FROM reporting.video_daily_dataset WHERE video_id='b0'")[0] == ("1:02:05", 3725, None)
    assert q(conn, "SELECT video_duration FROM reporting.video_daily_dataset WHERE video_id='b1'")[0][0] == "0:59"
    # capture date / timestamp
    assert q(conn, "SELECT DISTINCT data_capture_date FROM reporting.video_daily_dataset")[0][0] == DAY1.date()


def test_time_windows_and_counts(conn):
    run(DAY1)
    counts = dict(q(conn, "SELECT time_window, count(*) FROM reporting.video_by_window GROUP BY 1"))
    # alpha: published 2h,14h ago (<24h); beta: b0 5h ago
    assert counts["Last 24 hours"] == 3
    assert counts["Last 7 days"] == 14 + 2      # alpha: 2h + 12h*k <= 168h -> 14 ; beta b0,b1
    assert counts["Last 28 days"] == 56 + 2

    summary = {(r[0], r[1]): r[2] for r in q(conn, "SELECT time_window, channel_name, videos_uploaded FROM reporting.upload_summary")}
    assert summary[("Last 24 hours", "Alpha News LK")] == 2
    assert summary[("Last 24 hours", "Beta Cooking")] == 1

    top = q(conn, "SELECT video_id FROM reporting.video_by_window WHERE time_window='Last 24 hours' "
                  "ORDER BY views_rank_all_channels LIMIT 1")
    assert top[0][0] == "a0"   # 1000 views


def test_second_day_adds_snapshots_and_view_gains(conn):
    run(DAY1)
    run(DAY2, view_boost=250)
    per_day = dict(q(conn, "SELECT data_capture_date, count(*) FROM reporting.video_daily_dataset GROUP BY 1"))
    assert per_day[DAY1.date()] == 58
    assert len(per_day) == 2
    gain = q(conn, "SELECT views_gained_since_prev_capture FROM reporting.video_latest WHERE video_id='a3'")[0][0]
    assert gain == 250
    # latest view = one row per video, using the newest capture
    assert q(conn, "SELECT count(*), count(DISTINCT video_id) FROM reporting.video_latest")[0][0] == \
        q(conn, "SELECT count(DISTINCT video_id) FROM core.video")[0][0]


def test_same_day_rerun_is_idempotent(conn):
    run(DAY1)
    run(DAY1 + timedelta(hours=3), view_boost=10)
    rows = q(conn, "SELECT count(*) FROM core.video_daily_snapshot WHERE capture_date=%s", DAY1.date())
    assert rows[0][0] == 58  # overwritten, not duplicated
    assert q(conn, "SELECT view_count FROM core.video_daily_snapshot WHERE video_id='a0'")[0][0] == 1010


def test_retransform_from_raw_without_api(conn):
    s = run(DAY1)
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DELETE FROM core.video_daily_snapshot")
    assert retransform(settings(), s.run_id) == 58


def test_failed_run_is_logged_and_rolled_back(conn):
    with pytest.raises(RuntimeError):
        run(DAY1, channels=["UC_DOES_NOT_EXIST"])
    status = q(conn, "SELECT status, error_message FROM core.pipeline_run")[0]
    assert status[0] == "failed" and "UC_DOES_NOT_EXIST" in status[1]
    assert q(conn, "SELECT count(*) FROM raw.api_response")[0][0] == 0


def test_api_key_is_never_stored(conn):
    leaky = __import__("requests").ConnectionError(f"url: /videos?key={KEY}")

    def boom(ep, p):
        if ep == "videos":
            raise leaky
    run(DAY1)
    with pytest.raises(Exception):
        run(DAY2, overrides=[boom])
    dump = q(conn, "SELECT string_agg(request_params::text || context::text, ' ') FROM raw.api_response")[0][0]
    errors = q(conn, "SELECT string_agg(coalesce(error_message, ''), ' ') FROM core.pipeline_run")[0][0]
    assert KEY not in dump and KEY not in errors
    assert "REDACTED" in errors


def test_partial_run_saves_good_channels(conn):
    s = run(DAY1, overrides=[lambda ep, p: FakeResponse(500, {}) if ep == "videos" and "b0" in p["id"] else None])
    assert s.status == "partial" and s.channels == 1
    row = q(conn, "SELECT status, error_message FROM core.pipeline_run")[0]
    assert row[0] == "partial" and "UC_BETA" in row[1]
    assert q(conn, "SELECT DISTINCT channel_name FROM reporting.video_latest") == [("Alpha News LK",)]
    # the dashboard still uses a partial run as its reference time
    assert q(conn, "SELECT count(*) FROM reporting.video_by_window")[0][0] > 0


def test_quota_exceeded_fails_cleanly_and_can_rerun(conn):
    with pytest.raises(QuotaExceededError):
        run(DAY1, overrides=[lambda ep, p: api_error(403, "quotaExceeded") if ep == "videos" else None])
    assert q(conn, "SELECT status FROM core.pipeline_run") == [("failed",)]
    assert q(conn, "SELECT count(*) FROM core.video")[0][0] == 0
    assert run(DAY1 + timedelta(hours=1)).status == "success"   # re-run the same day works


def test_two_runs_cannot_overlap(conn):
    with db.connect(DB_URL) as holder:
        db.acquire_run_lock(holder)
        with pytest.raises(db.RunAlreadyInProgress):
            run(DAY1)
    assert run(DAY1).status == "success"   # lock released when the first connection closed


def test_old_raw_pages_are_purged(conn):
    s = run(DAY1)
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE raw.api_response SET fetched_at = now() - interval '120 days' WHERE run_id = %s", (s.run_id,))
    run(DAY2)
    assert q(conn, "SELECT count(*) FROM raw.api_response WHERE run_id = %s", s.run_id)[0][0] == 0
    # history in core is kept
    assert q(conn, "SELECT count(*) FROM core.video_daily_snapshot WHERE capture_date = %s", DAY1.date())[0][0] == 58


def test_retransform_unknown_run_is_a_clear_error(conn):
    with pytest.raises(RuntimeError, match="No raw data"):
        retransform(settings(), "00000000-0000-0000-0000-000000000000")


# --- live `verify` command (run here against the fake API) -------------------
from yt_pipeline.verify import FAIL, PASS, Verifier  # noqa: E402


def verify(now, deep=True, overrides=()):
    fake = FakeYouTube(now)
    fake.overrides.extend(overrides)
    client = YouTubeClient(KEY, session=fake, backoff_seconds=0, max_retries=1)
    checks = Verifier(settings(), client=client, deep=deep).run()
    return {c.name: c for c in checks}, client


TEST_READER = "looker_reader_test"


def test_verify_all_good(conn, monkeypatch):
    # Roles are shared by every database on the server, so never touch the real looker_reader.
    monkeypatch.setattr(db, "READER_ROLE", TEST_READER)
    run(DAY1)
    db.create_reader(conn, "a-strong-test-password", role=TEST_READER)
    try:
        checks, client = verify(DAY1)
        fails = [f"{c.name}: {c.detail}" for c in checks.values() if c.status == FAIL]
        assert fails == []
        assert checks["API key works"].status == PASS
        assert checks["Alpha News LK"].status == PASS          # completeness cross-check
        assert checks["read-only dashboard user"].status == PASS
        assert checks["API key not stored in database"].status == PASS
        assert checks["matches YouTube right now"].status == PASS
        assert client.quota_units_used >= 200                   # 2 channels x search (100 units)
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as c:
            # non-superusers (e.g. Neon's owner) need membership in a role to DROP OWNED BY it
            c.execute(f"GRANT {TEST_READER} TO CURRENT_USER")
            c.execute(f"DROP OWNED BY {TEST_READER}")
            c.execute(f"DROP ROLE {TEST_READER}")


def test_verify_detects_missing_video(conn):
    run(DAY1)
    with psycopg.connect(DB_URL, autocommit=True) as c:
        for t in ("core.video_daily_snapshot", "core.video_playlist", "core.video"):
            c.execute(f"DELETE FROM {t} WHERE video_id = 'a3'")
    checks, _ = verify(DAY1)
    assert checks["Alpha News LK"].status == FAIL and "a3" in checks["Alpha News LK"].detail
    assert checks["Beta Cooking"].status == PASS


def test_verify_detects_leaked_key_and_bad_key(conn):
    run(DAY1)
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE core.pipeline_run SET error_message = %s", (f"oops {KEY}",))
    checks, _ = verify(DAY1, deep=False)
    assert checks["API key not stored in database"].status == FAIL
    bad, _ = verify(DAY1, deep=False, overrides=[lambda ep, p: api_error(400, "badRequest", "API key not valid")])
    assert bad["API key works"].status == FAIL


def test_verify_reports_missing_schema(conn):
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS reporting, core, raw CASCADE")
    checks, _ = verify(DAY1, deep=False)
    assert checks["schema + views"].status == FAIL


def test_create_reader_rejects_short_password(conn):
    with pytest.raises(ValueError):
        db.create_reader(conn, "short")


def test_interrupted_and_killed_runs_are_closed(conn):
    def ctrl_c(ep, p):
        if ep == "videos":
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(DAY1, overrides=[ctrl_c])
    assert q(conn, "SELECT status FROM core.pipeline_run") == [("failed",)]
    # simulate a hard kill (kill -9): the row is left as 'running'
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE core.pipeline_run SET status = 'running', error_message = NULL")
    run(DAY1 + timedelta(hours=1))
    rows = dict(q(conn, "SELECT status, error_message FROM core.pipeline_run ORDER BY started_at"))
    assert "abandoned" in rows["failed"] and "success" in rows
