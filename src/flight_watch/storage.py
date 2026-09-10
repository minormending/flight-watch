from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from .models import Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    origin          TEXT    NOT NULL,
    destination     TEXT    NOT NULL,
    stay_nights     INTEGER NOT NULL,
    pairs_requested INTEGER NOT NULL DEFAULT 0,
    pairs_ok        INTEGER NOT NULL DEFAULT 0,
    pairs_failed    INTEGER NOT NULL DEFAULT 0,
    best_price      INTEGER
);

CREATE TABLE IF NOT EXISTS observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id          INTEGER NOT NULL REFERENCES scans(id),
    checked_at       TEXT    NOT NULL,
    origin           TEXT    NOT NULL,
    destination      TEXT    NOT NULL,
    depart_date      TEXT    NOT NULL,
    return_date      TEXT    NOT NULL,
    price            INTEGER NOT NULL,
    currency         TEXT    NOT NULL,
    airlines         TEXT,
    stops            INTEGER,
    duration_minutes INTEGER,
    source           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_route_time
    ON observations (origin, destination, checked_at);
CREATE INDEX IF NOT EXISTS idx_obs_pair
    ON observations (origin, destination, depart_date, return_date);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at    TEXT    NOT NULL,
    depart_date TEXT    NOT NULL,
    return_date TEXT    NOT NULL,
    price       INTEGER NOT NULL,
    threshold   INTEGER NOT NULL,
    pool_size   INTEGER NOT NULL,
    channels    TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(db_file: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_file)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def start_scan(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    stay_nights: int,
    pairs_requested: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO scans (started_at, origin, destination, stay_nights, pairs_requested)"
        " VALUES (?, ?, ?, ?, ?)",
        (utcnow(), origin, destination, stay_nights, pairs_requested),
    )
    return int(cur.lastrowid)


def finish_scan(
    conn: sqlite3.Connection,
    scan_id: int,
    ok: int,
    failed: int,
    best_price: Optional[int],
) -> None:
    conn.execute(
        "UPDATE scans SET finished_at = ?, pairs_ok = ?, pairs_failed = ?, best_price = ?"
        " WHERE id = ?",
        (utcnow(), ok, failed, best_price, scan_id),
    )


def record_quotes(
    conn: sqlite3.Connection,
    scan_id: int,
    origin: str,
    destination: str,
    source: str,
    quotes: Iterable[Quote],
) -> int:
    now = utcnow()
    rows = [
        (
            scan_id,
            now,
            origin,
            destination,
            q.depart_date,
            q.return_date,
            q.price,
            q.currency,
            q.airlines,
            q.stops,
            q.duration_minutes,
            source,
        )
        for q in quotes
    ]
    conn.executemany(
        "INSERT INTO observations (scan_id, checked_at, origin, destination, depart_date,"
        " return_date, price, currency, airlines, stops, duration_minutes, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


def baseline_prices(
    conn: sqlite3.Connection, origin: str, destination: str, days: int
) -> list[int]:
    """Every observed price on this route in the last `days` days.

    Route-level rather than per-date-pair: the trip has no fixed dates, so any
    4-night trip is interchangeable and they all belong in the same pool.
    """
    rows = conn.execute(
        "SELECT price FROM observations"
        " WHERE origin = ? AND destination = ? AND checked_at >= ?",
        (origin, destination, _since(days)),
    ).fetchall()
    return [int(r["price"]) for r in rows]


def history_span_days(conn: sqlite3.Connection, origin: str, destination: str) -> float:
    row = conn.execute(
        "SELECT MIN(checked_at) AS lo, MAX(checked_at) AS hi FROM observations"
        " WHERE origin = ? AND destination = ?",
        (origin, destination),
    ).fetchone()
    if not row or not row["lo"] or not row["hi"]:
        return 0.0
    lo = datetime.fromisoformat(row["lo"])
    hi = datetime.fromisoformat(row["hi"])
    return (hi - lo).total_seconds() / 86400.0


def last_alert(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts ORDER BY fired_at DESC LIMIT 1"
    ).fetchone()


def record_alert(
    conn: sqlite3.Connection,
    quote: Quote,
    threshold: int,
    pool_size: int,
    channels: Sequence[str],
) -> None:
    conn.execute(
        "INSERT INTO alerts (fired_at, depart_date, return_date, price, threshold,"
        " pool_size, channels) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            utcnow(),
            quote.depart_date,
            quote.return_date,
            quote.price,
            threshold,
            pool_size,
            ",".join(channels),
        ),
    )
