from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from .models import Quote

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    origin          TEXT    NOT NULL,
    destination     TEXT    NOT NULL,
    stay_nights     INTEGER NOT NULL,
    watch           TEXT    NOT NULL DEFAULT '',
    tier            TEXT    NOT NULL DEFAULT 'full',
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
    signature        TEXT    NOT NULL DEFAULT 'legacy',
    price            INTEGER NOT NULL,
    currency         TEXT    NOT NULL,
    airlines         TEXT,
    stops            INTEGER,
    duration_minutes INTEGER,
    source           TEXT    NOT NULL,
    booking_url      TEXT    NOT NULL DEFAULT '',
    watch            TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_obs_sig_time
    ON observations (signature, checked_at);
CREATE INDEX IF NOT EXISTS idx_obs_pair
    ON observations (origin, destination, depart_date, return_date);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at    TEXT    NOT NULL,
    signature   TEXT    NOT NULL DEFAULT 'legacy',
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


def _migrate(conn: sqlite3.Connection) -> None:
    """Add the signature columns to databases created before they existed.

    Pre-existing rows keep the 'legacy' default, so they are simply never
    matched by a real signature and drop out of the baseline instead of
    polluting it.
    """
    for table in ("observations", "alerts"):
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if columns and "signature" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN signature TEXT NOT NULL DEFAULT 'legacy'"
            )

    additions = {
        "observations": [
            ("booking_url", "TEXT NOT NULL DEFAULT ''"),
            ("watch", "TEXT NOT NULL DEFAULT ''"),
        ],
        "scans": [
            ("watch", "TEXT NOT NULL DEFAULT ''"),
            ("tier", "TEXT NOT NULL DEFAULT 'full'"),
        ],
    }
    for table, cols in additions.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for column, decl in cols:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    _upgrade_signatures(conn)


# Signatures gained a date-mode and a stop-limit segment when watches learned
# about fixed windows and nonstop-only. Everything recorded before that was a
# rolling, any-stops search, so the old keys describe the same product and are
# rewritten rather than orphaned -- otherwise the refactor would silently throw
# away real history and restart the warmup gate.
_OLD_SIGNATURE = re.compile(r"^([A-Z]{3}-[A-Z]{3}):(\d+)n:(\d+a\d+c.*)$")


def _upgrade_signatures(conn: sqlite3.Connection) -> None:
    for table in ("observations", "alerts"):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "signature" not in existing:
            continue
        rows = conn.execute(
            f"SELECT DISTINCT signature FROM {table} WHERE signature != 'legacy'"
        ).fetchall()
        for row in rows:
            old = row["signature"]
            match = _OLD_SIGNATURE.match(old)
            if not match:
                continue
            new = (
                f"{match.group(1)}:rolling:{match.group(2)}n:stopsany:{match.group(3)}"
            )
            conn.execute(
                f"UPDATE {table} SET signature = ? WHERE signature = ?", (new, old)
            )
            logger.info("Upgraded signature %s -> %s in %s", old, new, table)


def connect(db_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Migrate before the schema script: it creates an index over `signature`,
    # which an older database does not have a column for yet.
    _migrate(conn)
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
    watch: str = "",
    tier: str = "full",
) -> int:
    cur = conn.execute(
        "INSERT INTO scans (started_at, origin, destination, stay_nights,"
        " pairs_requested, watch, tier) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (utcnow(), origin, destination, stay_nights, pairs_requested, watch, tier),
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
    signature: str,
    source: str,
    quotes: Iterable[Quote],
    watch: str = "",
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
            signature,
            q.price,
            q.currency,
            q.airlines,
            q.stops,
            q.duration_minutes,
            source,
            q.booking_url,
            watch,
        )
        for q in quotes
    ]
    conn.executemany(
        "INSERT INTO observations (scan_id, checked_at, origin, destination, depart_date,"
        " return_date, signature, price, currency, airlines, stops, duration_minutes,"
        " source, booking_url, watch)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


def baseline_prices(conn: sqlite3.Connection, signature: str, days: int) -> list[int]:
    """Every price observed for this exact search in the last `days` days.

    Pooled across date pairs, because the trip has no fixed dates and any
    stay of the right length is interchangeable -- but never across
    signatures, since a different party or cabin is a different product.
    """
    rows = conn.execute(
        "SELECT price FROM observations WHERE signature = ? AND checked_at >= ?",
        (signature, _since(days)),
    ).fetchall()
    return [int(r["price"]) for r in rows]


def history_span_days(conn: sqlite3.Connection, signature: str) -> float:
    row = conn.execute(
        "SELECT MIN(checked_at) AS lo, MAX(checked_at) AS hi FROM observations"
        " WHERE signature = ?",
        (signature,),
    ).fetchone()
    if not row or not row["lo"] or not row["hi"]:
        return 0.0
    lo = datetime.fromisoformat(row["lo"])
    hi = datetime.fromisoformat(row["hi"])
    return (hi - lo).total_seconds() / 86400.0


def last_alert(conn: sqlite3.Connection, signature: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts WHERE signature = ? ORDER BY fired_at DESC LIMIT 1",
        (signature,),
    ).fetchone()


def record_alert(
    conn: sqlite3.Connection,
    quote: Quote,
    signature: str,
    threshold: int,
    pool_size: int,
    channels: Sequence[str],
) -> None:
    conn.execute(
        "INSERT INTO alerts (fired_at, signature, depart_date, return_date, price,"
        " threshold, pool_size, channels) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utcnow(),
            signature,
            quote.depart_date,
            quote.return_date,
            quote.price,
            threshold,
            pool_size,
            ",".join(channels),
        ),
    )


def last_full_scan_age_hours(conn: sqlite3.Connection, watch: str) -> float | None:
    """Hours since this watch last completed a full sweep, or None if never."""
    row = conn.execute(
        "SELECT MAX(finished_at) AS at FROM scans"
        " WHERE watch = ? AND tier = 'full' AND finished_at IS NOT NULL",
        (watch,),
    ).fetchone()
    if not row or not row["at"]:
        return None
    return (
        datetime.now(timezone.utc) - datetime.fromisoformat(row["at"])
    ).total_seconds() / 3600.0


def cheapest_destinations(
    conn: sqlite3.Connection, signatures: Sequence[str], limit: int, days: int = 14
) -> list[str]:
    """The `limit` destinations with the lowest price seen recently.

    Drives the between-sweeps tier: re-poll where the deals actually are
    instead of spending the whole request budget on Tokyo every time.
    """
    if not signatures:
        return []
    placeholders = ",".join("?" * len(signatures))
    rows = conn.execute(
        "SELECT destination, MIN(price) AS low FROM observations"
        f" WHERE signature IN ({placeholders}) AND checked_at >= ?"
        " GROUP BY destination ORDER BY low ASC LIMIT ?",
        (*signatures, _since(days), limit),
    ).fetchall()
    return [r["destination"] for r in rows]
