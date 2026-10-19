"""Command line entry point.

    python -m yt_pipeline init-db                 # create tables + views
    python -m yt_pipeline run                     # daily ELT run
    python -m yt_pipeline run --lookback-days 7   # override the refresh window
    python -m yt_pipeline retransform <run_id>    # rebuild core from stored raw JSON
    python -m yt_pipeline status                  # recent runs + row counts
    python -m yt_pipeline verify [--deep]         # live checks: API, completeness, data, security
    python -m yt_pipeline create-reader           # read-only DB login for Looker Studio

Exit codes: 0 ok, 1 error, 2 bad configuration, 3 API quota exceeded,
            4 partial run (data saved, but some channels failed).
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import sys

import psycopg

from . import db
from .config import ConfigError, Settings, parse_channels
from .pipeline import retransform, run_pipeline
from .youtube_client import QuotaExceededError, YouTubeAPIError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yt_pipeline", description="Daily YouTube performance ELT")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create schemas, tables and reporting views")
    run_p = sub.add_parser("run", help="run the daily Extract-Load-Transform")
    run_p.add_argument("--lookback-days", type=int, help="override LOOKBACK_DAYS")
    run_p.add_argument("--channels", help="override YOUTUBE_CHANNELS (comma separated)")
    re_p = sub.add_parser("retransform", help="re-run SQL transform for a stored run")
    re_p.add_argument("run_id")
    sub.add_parser("status", help="show recent runs and table counts")
    ver_p = sub.add_parser("verify", help="live checks against the real API and database")
    ver_p.add_argument("--deep", action="store_true",
                       help="also cross-check completeness with YouTube search (100 quota units per page)")
    sub.add_parser("create-reader", help="create/reset the read-only looker_reader login")
    args = parser.parse_args(argv)

    needs_api = args.command in ("run", "verify")
    try:
        settings = Settings.from_env(require_api=needs_api)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.command == "run":
        overrides = {}
        if args.lookback_days:
            overrides["lookback_days"] = args.lookback_days
        if args.channels:
            overrides["channels"] = parse_channels(args.channels)
        settings = dataclasses.replace(settings, **overrides)
        try:
            summary = run_pipeline(settings)
        except QuotaExceededError as exc:
            print(f"FAILED: quota exceeded - try again after the quota resets (midnight Pacific time): {exc}",
                  file=sys.stderr)
            return 3
        except YouTubeAPIError as exc:
            print(f"FAILED: YouTube API error: {exc}", file=sys.stderr)
            return 1
        except psycopg.Error as exc:
            print(f"FAILED: database error: {_first_line(exc)}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        print(
            f"{summary.status.upper()} run_id={summary.run_id} date={summary.capture_date} "
            f"channels={summary.channels} videos={summary.videos_captured} "
            f"api_requests={summary.api_requests} quota_units={summary.quota_units_used}"
        )
        for issue in summary.issues:
            print(f"  WARNING: {issue}", file=sys.stderr)
        # non-zero exit for partial runs so the scheduler flags them,
        # even though the collected data was saved
        return 4 if summary.status == "partial" else 0

    try:
        return _db_command(args, settings)
    except psycopg.Error as exc:
        print(f"FAILED: database error: {_first_line(exc)}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _db_command(args, settings) -> int:
    if args.command == "verify":
        from pathlib import Path

        from .verify import Verifier, print_report
        verifier = Verifier(settings, deep=args.deep, repo_dir=Path(__file__).resolve().parent.parent)
        return print_report(verifier.run(), verifier.client)

    if args.command == "create-reader":
        import getpass
        import os
        password = os.getenv("READER_PASSWORD") or getpass.getpass("New password for looker_reader: ")
        if not os.getenv("READER_PASSWORD") and password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 2
        try:
            with db.connect(settings.database_url) as conn:
                db.create_reader(conn, password)
                host, port, dbname = conn.info.host, conn.info.port, conn.info.dbname
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("Read-only user ready. Use these in Looker Studio:")
        print(f"  Host: {host}\n  Port: {port}\n  Database: {dbname}\n  Username: {db.READER_ROLE}")
        return 0

    if args.command == "init-db":
        with db.connect(settings.database_url) as conn:
            db.init_db(conn)
        print("Database initialised.")
        return 0

    if args.command == "retransform":
        n = retransform(settings, args.run_id)
        print(f"Re-transformed run {args.run_id}: {n} snapshot rows")
        return 0

    if args.command == "status":
        with db.connect(settings.database_url) as conn:
            runs = conn.execute(
                """SELECT started_at_utc, status, videos_captured, quota_units_used, error_message
                     FROM reporting.pipeline_runs ORDER BY started_at_utc DESC LIMIT 10"""
            ).fetchall()
            counts = conn.execute(
                """SELECT (SELECT count(*) FROM core.channel), (SELECT count(*) FROM core.video),
                          (SELECT count(*) FROM core.video_daily_snapshot),
                          (SELECT max(data_capture_ts_utc) FROM reporting.video_daily_dataset)"""
            ).fetchone()
        print(f"channels={counts[0]} videos={counts[1]} snapshots={counts[2]} latest_capture_utc={counts[3]}")
        for r in runs:
            print(f"  {r[0]:%Y-%m-%d %H:%M} {r[1]:8} videos={r[2]} quota={r[3]} {r[4] or ''}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
