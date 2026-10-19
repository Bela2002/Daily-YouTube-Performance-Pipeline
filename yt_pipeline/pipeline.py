"""Orchestrates one daily run: Extract -> Load (raw) -> Transform (SQL).

Run statuses written to core.pipeline_run:
  success  - every configured channel was collected
  partial  - data was saved, but some channels were missing/failed (listed in error_message)
  failed   - nothing was saved (e.g. quota exceeded, database error); safe to re-run
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from . import db
from .config import Settings
from .extract import ExtractResult, Extractor
from .youtube_client import YouTubeClient

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    run_id: str
    status: str
    capture_date: str
    channels: int
    videos_captured: int
    raw_pages: int
    api_requests: int
    quota_units_used: int
    issues: list[str] = field(default_factory=list)


def run_pipeline(
    settings: Settings,
    client: YouTubeClient | None = None,
    now: datetime | None = None,
) -> RunSummary:
    started = now or datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    client = client or YouTubeClient(settings.youtube_api_key)

    with db.connect(settings.database_url) as conn:
        db.acquire_run_lock(conn)          # refuse to run twice at the same time
        _close_abandoned_runs(conn)
        _start_run(conn, run_id, started, settings)
        try:
            extractor = Extractor(
                client,
                lookback_days=settings.lookback_days,
                fetch_playlists=settings.fetch_playlists,
                max_playlists_per_channel=settings.max_playlists_per_channel,
                max_pages_per_playlist=settings.max_pages_per_playlist,
                max_upload_pages=settings.max_upload_pages,
                now=started,
            )
            result = extractor.run(settings.channels)
            issues = _issues(result)
            if not result.channel_ids:
                raise RuntimeError("No channel could be collected. " + "; ".join(issues))

            # Load + transform + mark finished in ONE transaction: either the
            # whole day lands, or nothing does.
            pages = db.load_raw(conn, run_id, result.responses)
            captured = db.transform(conn, run_id)
            purged = db.purge_raw(conn, settings.raw_retention_days)
            if purged:
                log.info("Purged %d raw API pages older than %d days", purged, settings.raw_retention_days)
            status = "partial" if result.is_partial else "success"
            _finish_run(conn, run_id, status, client, captured, error=client.redact("; ".join(issues)) or None)
            conn.commit()
        except BaseException as exc:      # includes Ctrl+C (KeyboardInterrupt)
            _mark_failed(conn, settings, run_id, client, client.redact(f"{type(exc).__name__}: {exc}"))
            raise

    summary = RunSummary(
        run_id=run_id,
        status=status,
        capture_date=started.date().isoformat(),
        channels=len(result.channel_ids),
        videos_captured=captured,
        raw_pages=pages,
        api_requests=client.requests_made,
        quota_units_used=client.quota_units_used,
        issues=issues,
    )
    log.info("Run complete: %s", summary)
    return summary


def retransform(settings: Settings, run_id: str) -> int:
    """Rebuild core tables from raw JSON already stored for a run (no API calls)."""
    with db.connect(settings.database_url) as conn:
        exists = conn.execute(
            "SELECT count(*) FROM raw.api_response WHERE run_id = %s", (run_id,)
        ).fetchone()[0]
        if not exists:
            raise RuntimeError(f"No raw data stored for run {run_id} (unknown run, failed run, or purged)")
        captured = db.transform(conn, run_id)
        conn.commit()
    return captured


# ----------------------------------------------------------------------
def _issues(result: ExtractResult) -> list[str]:
    issues = [f"channel not found: {c}" for c in result.missing_channels]
    issues += [f"channel failed {c}: {err}" for c, err in result.failed_channels.items()]
    issues += result.warnings
    return issues


def _close_abandoned_runs(conn: psycopg.Connection) -> None:
    """We hold the run lock, so any run still marked 'running' belongs to a
    process that was killed (timeout, power cut, kill -9). Close it."""
    n = conn.execute(
        """UPDATE core.pipeline_run
              SET status = 'failed', finished_at = now(),
                  error_message = 'abandoned: the process stopped before finishing (killed or timed out)'
            WHERE status = 'running'""").rowcount
    conn.commit()
    if n:
        log.warning("Marked %d abandoned run(s) as failed", n)


def _start_run(conn: psycopg.Connection, run_id: str, started: datetime, s: Settings) -> None:
    conn.execute(
        """INSERT INTO core.pipeline_run
               (run_id, started_at, capture_date, status, channels_input, lookback_days)
           VALUES (%s, %s, %s, 'running', %s, %s)""",
        (run_id, started, started.date(), s.channels, s.lookback_days),
    )
    conn.commit()


def _finish_run(conn, run_id, status, client, captured, error=None) -> None:
    conn.execute(
        """UPDATE core.pipeline_run
              SET status = %s, finished_at = now(), api_requests = %s,
                  quota_units_used = %s, videos_captured = %s, error_message = %s
            WHERE run_id = %s""",
        (status, client.requests_made, client.quota_units_used, captured, (error or "")[:2000] or None, run_id),
    )


def _mark_failed(conn, settings, run_id, client, error: str) -> None:
    """Record the failure without hiding the original exception, even if the
    original connection is broken (falls back to a fresh connection)."""
    log.error("Run %s failed: %s", run_id, error)
    try:
        conn.rollback()
        _finish_run(conn, run_id, "failed", client, None, error=error)
        conn.commit()
        return
    except Exception:  # connection may be dead - try a fresh one
        pass
    try:
        with db.connect(settings.database_url, attempts=1) as fresh:
            _finish_run(fresh, run_id, "failed", client, None, error=error)
    except Exception as exc:
        log.error("Could not record failure of run %s in the database: %s", run_id, exc)
