"""Database helpers: connection, migrations, raw load and SQL transform."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .extract import RawResponse

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
MIGRATIONS = ["001_schema.sql", "002_views.sql"]

# Arbitrary constant used as a PostgreSQL advisory-lock key so that two
# pipeline runs can never write at the same time.
RUN_LOCK_KEY = 7_420_261_018


class RunAlreadyInProgress(RuntimeError):
    pass


def connect(database_url: str, attempts: int = 3, wait_seconds: float = 3.0) -> psycopg.Connection:
    """Connect with a few retries: serverless Postgres (e.g. Neon) may need
    a few seconds to wake up, and networks blip."""
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(database_url, connect_timeout=30, application_name="yt_pipeline")
        except psycopg.OperationalError as exc:
            if attempt == attempts:
                raise
            log.warning("Database connection failed (attempt %d/%d): %s", attempt, attempts,
                        str(exc).strip().splitlines()[0] if str(exc).strip() else exc)
            time.sleep(wait_seconds * attempt)
    raise AssertionError("unreachable")


def is_transaction_pooler(host: str | None, port: int | str | None) -> bool:
    """Neon's '-pooler' hosts and Supabase's port 6543 run PgBouncer in
    transaction mode, where session-level advisory locks do not work."""
    return "-pooler." in (host or "") or str(port) == "6543"


def acquire_run_lock(conn: psycopg.Connection) -> None:
    """Session-level lock, released automatically when the connection closes."""
    if is_transaction_pooler(conn.info.host, conn.info.port):
        raise RuntimeError(
            "DATABASE_URL points to a connection pooler (transaction mode), which breaks the "
            "run lock. Use the DIRECT connection string: in Neon click Connect and turn OFF "
            "'Connection pooling' (the host must not contain '-pooler')."
        )
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_KEY,)).fetchone()[0]
    conn.commit()
    if not got:
        raise RunAlreadyInProgress("Another pipeline run is already in progress - not starting a second one")


def purge_raw(conn: psycopg.Connection, retention_days: int) -> int:
    """Delete raw API pages older than the retention window (0 = keep forever).
    Core tables keep the history; raw is only needed for audits/re-runs."""
    if retention_days <= 0:
        return 0
    cur = conn.execute(
        "DELETE FROM raw.api_response WHERE fetched_at < now() - make_interval(days => %s)",
        (retention_days,),
    )
    return cur.rowcount


def init_db(conn: psycopg.Connection) -> None:
    """Create schemas, tables, functions and reporting views (idempotent)."""
    with conn.cursor() as cur:
        for name in MIGRATIONS:
            log.info("Applying %s", name)
            cur.execute((SQL_DIR / name).read_text(encoding="utf-8"))
    conn.commit()


def load_raw(conn: psycopg.Connection, run_id: str, responses: list[RawResponse]) -> int:
    """LOAD step: insert every API response page untouched (JSONB)."""
    rows = [
        (run_id, r.endpoint, Jsonb(r.request_params), Jsonb(r.context), Jsonb(r.payload), r.fetched_at)
        for r in responses
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO raw.api_response
                   (run_id, endpoint, request_params, context, payload, fetched_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            rows,
        )
    return len(rows)


def transform(conn: psycopg.Connection, run_id: str) -> int:
    """TRANSFORM step: build core tables from this run's raw JSON in SQL.
    Returns the number of daily snapshot rows written by the run."""
    transform_sql = (SQL_DIR / "transform.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('pipeline.run_id', %s, true)", (run_id,))
        cur.execute(transform_sql)
        cur.execute("SELECT count(*) FROM core.video_daily_snapshot WHERE run_id = %s", (run_id,))
        return cur.fetchone()[0]


READER_ROLE = "looker_reader"


def create_reader(conn: psycopg.Connection, password: str, role: str = READER_ROLE) -> None:
    """Create (or reset the password of) a read-only login for Looker Studio.
    It can read the `reporting` views only - not `raw` or `core`."""
    if len(password) < 12:
        raise ValueError("Use a password of at least 12 characters")
    ident, pw = sql.Identifier(role), sql.Literal(password)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if exists:
        # (non-superusers such as Neon's owner role may not touch SUPERUSER etc. on ALTER)
        conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(ident, pw))
    else:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(ident, pw))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA reporting TO {}").format(ident))
    conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO {}").format(ident))
    conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA reporting GRANT SELECT ON TABLES TO {}").format(ident))
    conn.commit()
