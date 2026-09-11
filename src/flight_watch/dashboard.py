from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .alerts import percentile
from .config import AppConfig, SearchConfig, WatchConfig
from .storage import baseline_prices, history_span_days, session, utcnow

logger = logging.getLogger(__name__)


def _latest_rows(conn: sqlite3.Connection, signature: str) -> list[dict[str, Any]]:
    """The most recent price for each departure date under one signature.

    Most-recent rather than min-ever: a price we saw three weeks ago and can no
    longer book is not something to put in front of someone.
    """
    rows = conn.execute(
        """
        SELECT o.depart_date, o.return_date, o.price, o.airlines, o.stops,
               o.checked_at, o.booking_url
        FROM observations o
        JOIN (
            SELECT depart_date, MAX(checked_at) AS latest
            FROM observations WHERE signature = ? GROUP BY depart_date
        ) newest
          ON newest.depart_date = o.depart_date AND newest.latest = o.checked_at
        WHERE o.signature = ?
        GROUP BY o.depart_date
        ORDER BY o.depart_date
        """,
        (signature, signature),
    ).fetchall()
    return [dict(r) for r in rows]


def _threshold(conn: sqlite3.Connection, search: SearchConfig, cfg: AppConfig):
    pool = baseline_prices(conn, search.signature, cfg.alert.baseline_days)
    return int(percentile(pool, cfg.alert.percentile)) if pool else None


def _watch_block(
    conn: sqlite3.Connection, cfg: AppConfig, watch: WatchConfig
) -> dict[str, Any]:
    searches = watch.searches()
    multi = len(searches) > 1
    rows: list[dict[str, Any]] = []

    if multi:
        # One row per destination: the reader's question is "where is cheap?",
        # not "which Tuesday in February is cheap in Lisbon?".
        for search in searches:
            latest = _latest_rows(conn, search.signature)
            if not latest:
                continue
            best = min(latest, key=lambda r: r["price"])
            rows.append(
                {
                    "label": search.destination,
                    "destination": search.destination,
                    "price": int(best["price"]),
                    "airlines": best["airlines"] or "",
                    "depart": best["depart_date"],
                    "ret": best["return_date"],
                    "stops": best["stops"],
                    "booking_url": best["booking_url"],
                    "threshold": _threshold(conn, search, cfg),
                }
            )
        rows.sort(key=lambda r: r["price"])
    else:
        search = searches[0]
        threshold = _threshold(conn, search, cfg)
        for r in _latest_rows(conn, search.signature):
            rows.append(
                {
                    "label": f"{r['depart_date']} → {r['return_date']}",
                    "destination": search.destination,
                    "price": int(r["price"]),
                    "airlines": r["airlines"] or "",
                    "depart": r["depart_date"],
                    "ret": r["return_date"],
                    "stops": r["stops"],
                    "booking_url": r["booking_url"],
                    "threshold": threshold,
                }
            )

    sigs = [s.signature for s in searches]
    marks = ",".join("?" * len(sigs))
    all_prices = [
        int(r["price"])
        for r in conn.execute(
            f"SELECT price FROM observations WHERE signature IN ({marks})", sigs
        ).fetchall()
    ]
    history = [
        {
            "day": r["day"],
            "low": int(r["low"]),
            "mean": round(float(r["mean"]), 1),
            "n": int(r["n"]),
        }
        for r in conn.execute(
            "SELECT substr(checked_at,1,10) AS day, MIN(price) AS low,"
            " AVG(price) AS mean, COUNT(*) AS n FROM observations"
            f" WHERE signature IN ({marks}) GROUP BY day ORDER BY day",
            sigs,
        ).fetchall()
    ]
    last_scan = conn.execute(
        "SELECT MAX(finished_at) AS at FROM scans WHERE watch = ?", (watch.name,)
    ).fetchone()
    span = max((history_span_days(conn, s.signature) for s in searches), default=0.0)
    sample = searches[0]
    best_row = (
        rows[0] if multi else (min(rows, key=lambda r: r["price"]) if rows else None)
    )

    return {
        "name": watch.name,
        "kind": "destinations" if multi else "dates",
        "route": {
            "origin": watch.origin,
            "scope": (f"{len(searches)} destinations" if multi else sample.destination),
            "stay_label": watch.dates.stay_label,
            "stops_label": sample.stops_label,
            "window": (
                f"{watch.dates.depart_from} to {watch.dates.depart_to}"
                if watch.dates.mode == "fixed"
                else f"{watch.dates.start_days}-{watch.dates.end_days} days out"
            ),
            "party_label": watch.confirm_search(sample.destination).party_label,
            "party_size": watch.confirm_search(sample.destination).party_size,
            "cabin": sample.cabin_label,
            "carry_on_bags": watch.carry_on_bags,
            "proxy_label": f"{watch.proxy_adults} adults" if watch.is_proxy else None,
            "percentile": cfg.alert.percentile,
            "baseline_days": cfg.alert.baseline_days,
        },
        "summary": {
            "observations": len(all_prices),
            "span_days": span,
            "all_time_low": min(all_prices) if all_prices else None,
            "median": int(percentile(all_prices, 50)) if all_prices else None,
            "threshold": best_row["threshold"] if best_row else None,
            "best": best_row,
            "last_scan": last_scan["at"] if last_scan else None,
        },
        "rows": rows,
        "history": history,
        "alerts": [
            dict(r)
            for r in conn.execute(
                "SELECT a.* FROM alerts a WHERE a.signature IN (%s)"
                " ORDER BY a.fired_at DESC LIMIT 10" % ",".join("?" * len(searches)),
                [s.signature for s in searches],
            ).fetchall()
        ],
    }


def build_payload(conn: sqlite3.Connection, cfg: AppConfig) -> dict[str, Any]:
    return {
        "generated_at": utcnow(),
        "watches": [_watch_block(conn, cfg, w) for w in cfg.watches if w.destinations],
    }


def export_dashboard(cfg: AppConfig, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with session(cfg.db_file) as conn:
        payload = build_payload(conn, cfg)
    target = out_dir / "data.json"
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Dashboard data written to %s", target)
    return target
