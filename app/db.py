"""
SQLite storage: routine metric polls + backup-event summaries, each with
its own retention window.

Two tables, deliberately separate (per the handoff spec):
  - metrics: one row per host per poll. 30-day rolling retention -
    includes both normal-cadence and finer backup-window samples, so
    the temperature curve during a backup is just a denser slice of the
    same series.
  - backup_events: one row per detected backup run (start/end/duration/
    peak temps). ~52 rows/year, kept for a full year by default so
    slow drift across months is visible - independently prunable from
    the routine metrics.
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    ts TEXT NOT NULL,
    cpu_temp REAL,
    nvme_composite_temp REAL,
    nvme_sensor1_temp REAL,
    nvme_sensor2_temp REAL,
    nvme_critical_warning INTEGER,
    nvme_media_errors INTEGER,
    nvme_percentage_used REAL,
    fan_rpm REAL,
    cpu_temp_warn REAL,
    cpu_temp_crit REAL,
    nvme_composite_warn REAL,
    nvme_composite_crit REAL,
    in_backup_window INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_metrics_host_ts ON metrics (host, ts);

CREATE TABLE IF NOT EXISTS backup_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    duration_seconds REAL,
    peak_nvme_temp REAL,
    peak_cpu_temp REAL,
    status TEXT NOT NULL DEFAULT 'in_progress'
);
CREATE INDEX IF NOT EXISTS idx_backup_events_host_start ON backup_events (host, start_ts);
"""


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_metric(metric: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO metrics (
                host, ts, cpu_temp, nvme_composite_temp, nvme_sensor1_temp,
                nvme_sensor2_temp, nvme_critical_warning, nvme_media_errors,
                nvme_percentage_used, fan_rpm, cpu_temp_warn, cpu_temp_crit,
                nvme_composite_warn, nvme_composite_crit, in_backup_window
            ) VALUES (:host, :ts, :cpu_temp, :nvme_composite_temp, :nvme_sensor1_temp,
                :nvme_sensor2_temp, :nvme_critical_warning, :nvme_media_errors,
                :nvme_percentage_used, :fan_rpm, :cpu_temp_warn, :cpu_temp_crit,
                :nvme_composite_warn, :nvme_composite_crit, :in_backup_window)
            """,
            metric,
        )


def start_backup_event(host: str, start_ts: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO backup_events (host, start_ts, status) VALUES (?, ?, 'in_progress')",
            (host, start_ts),
        )
        return cur.lastrowid


def finish_backup_event(event_id: int, end_ts: str, duration_seconds: float,
                         peak_nvme_temp: float | None, peak_cpu_temp: float | None,
                         status: str = "completed") -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE backup_events
            SET end_ts = ?, duration_seconds = ?, peak_nvme_temp = ?,
                peak_cpu_temp = ?, status = ?
            WHERE id = ?
            """,
            (end_ts, duration_seconds, peak_nvme_temp, peak_cpu_temp, status, event_id),
        )


def get_recent_metrics(host: str | None = None, since_ts: str | None = None, limit: int = 2000) -> list[dict]:
    clauses, params = [], []
    if host:
        clauses.append("host = ?")
        params.append(host)
    if since_ts:
        clauses.append("ts >= ?")
        params.append(since_ts)
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM metrics{where_sql}", params).fetchone()[0]

        if total <= limit:
            rows = conn.execute(f"SELECT * FROM metrics{where_sql} ORDER BY ts ASC", params).fetchall()
            return [dict(row) for row in rows]

        # More rows in the requested window than the cap - evenly downsample
        # across the FULL span instead of the old "ORDER BY ts DESC LIMIT"
        # behaviour, which silently truncated to just the most recent slice
        # (e.g. a 7d/30d request looked identical to a much shorter one,
        # since 3s-cadence data blows past a few-thousand-row cap in well
        # under a day).
        stride = -(-total // limit)  # ceil division
        rows = conn.execute(
            f"""
            WITH filtered AS (
                SELECT *, ROW_NUMBER() OVER (ORDER BY ts ASC) AS rn
                FROM metrics{where_sql}
            )
            SELECT * FROM filtered WHERE (rn - 1) % ? = 0 ORDER BY ts ASC
            """,
            [*params, stride],
        ).fetchall()
        return [{k: v for k, v in dict(row).items() if k != "rn"} for row in rows]


def get_latest_metric(host: str) -> dict | None:
    """Single most-recent row for a host - used to prime the UI at page
    load, distinct from get_recent_metrics()'s historical-series intent
    (which now always returns ascending, downsampled if needed)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM metrics WHERE host = ? ORDER BY ts DESC LIMIT 1", (host,)
        ).fetchone()
    return dict(row) if row else None


def get_backup_events(host: str | None = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM backup_events"
    params: list = []
    if host:
        query += " WHERE host = ?"
        params.append(host)
    query += " ORDER BY start_ts DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def prune_old_data() -> dict:
    """Delete rows past each table's retention window. Returns counts deleted."""
    now = time.time()
    metrics_cutoff_ts = _iso_days_ago(config.METRICS_RETENTION_DAYS)
    backup_cutoff_ts = _iso_days_ago(config.BACKUP_EVENT_RETENTION_DAYS)

    with _connect() as conn:
        m = conn.execute("DELETE FROM metrics WHERE ts < ?", (metrics_cutoff_ts,))
        b = conn.execute(
            "DELETE FROM backup_events WHERE start_ts < ? AND status != 'in_progress'",
            (backup_cutoff_ts,),
        )
        deleted = {"metrics_deleted": m.rowcount, "backup_events_deleted": b.rowcount}

    deleted["elapsed_seconds"] = round(time.time() - now, 3)
    return deleted


def _iso_days_ago(days: int) -> str:
    import datetime

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return cutoff.isoformat()


def interrupt_stale_backup_events() -> int:
    """Mark any backup_events left 'in_progress' from a prior process
    (crash/restart mid-backup) as 'interrupted', so nothing sits open
    forever. Called once at app startup."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE backup_events SET status = 'interrupted' WHERE status = 'in_progress'"
        )
        return cur.rowcount
