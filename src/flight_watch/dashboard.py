from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .alerts import percentile
from .config import AppConfig
from .models import Quote
from .storage import baseline_prices, history_span_days, session

logger = logging.getLogger(__name__)


def _latest_per_departure(
    conn: sqlite3.Connection, signature: str
) -> list[dict[str, Any]]:
    """The most recent price for each departure date -- i.e. current state.

    Most-recent rather than min-ever: a price we saw three weeks ago and can no
    longer book is not something to put in front of someone.
    """
    rows = conn.execute(
        """
        SELECT o.depart_date, o.return_date, o.price, o.airlines, o.stops, o.checked_at
        FROM observations o
        JOIN (
            SELECT depart_date, MAX(checked_at) AS latest
            FROM observations
            WHERE signature = ?
            GROUP BY depart_date
        ) newest
          ON newest.depart_date = o.depart_date AND newest.latest = o.checked_at
        WHERE o.signature = ?
        GROUP BY o.depart_date
        ORDER BY o.depart_date
        """,
        (signature, signature),
    ).fetchall()
    return [
        {
            "depart": r["depart_date"],
            "ret": r["return_date"],
            "price": int(r["price"]),
            "airlines": r["airlines"] or "",
            "stops": r["stops"],
            "checked_at": r["checked_at"],
        }
        for r in rows
    ]


def _daily_min(conn: sqlite3.Connection, signature: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT substr(checked_at, 1, 10) AS day,
               MIN(price) AS low,
               AVG(price) AS mean,
               COUNT(*)   AS n
        FROM observations
        WHERE signature = ?
        GROUP BY day
        ORDER BY day
        """,
        (signature,),
    ).fetchall()
    return [
        {
            "day": r["day"],
            "low": int(r["low"]),
            "mean": round(float(r["mean"]), 1),
            "n": int(r["n"]),
        }
        for r in rows
    ]


def build_payload(conn: sqlite3.Connection, cfg: AppConfig) -> dict[str, Any]:
    search = cfg.search
    origin, destination = search.origin, search.destination
    signature = search.signature

    scans = conn.execute(
        "SELECT * FROM scans WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 30"
    ).fetchall()
    by_date = _latest_per_departure(conn, signature)
    pool = baseline_prices(conn, signature, cfg.alert.baseline_days)
    all_prices = [
        int(r["price"])
        for r in conn.execute(
            "SELECT price FROM observations WHERE signature = ?", (signature,)
        ).fetchall()
    ]

    best = min(by_date, key=lambda r: r["price"]) if by_date else None
    threshold = int(percentile(pool, cfg.alert.percentile)) if pool else None

    alerts = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM alerts WHERE signature = ? ORDER BY fired_at DESC LIMIT 20",
            (signature,),
        ).fetchall()
    ]

    booking_url = None
    if best:
        booking_url = Quote(
            depart_date=best["depart"],
            return_date=best["ret"],
            price=best["price"],
            currency="USD",
            airlines=best["airlines"],
            stops=best["stops"],
            duration_minutes=None,
        ).booking_url(
            origin, destination, search.party_label, search.seat_class.replace("-", " ")
        )

    return {
        "route": {
            "origin": origin,
            "destination": destination,
            "stay_nights": search.stay_nights,
            "party_label": search.party_label,
            "party_size": search.party_size,
            "cabin": search.seat_class.replace("-", " "),
            "carry_on_bags": search.carry_on_bags,
            "percentile": cfg.alert.percentile,
            "baseline_days": cfg.alert.baseline_days,
        },
        "summary": {
            "observations": len(all_prices),
            "span_days": history_span_days(conn, signature),
            "all_time_low": min(all_prices) if all_prices else None,
            "median": int(percentile(all_prices, 50)) if all_prices else None,
            "threshold": threshold,
            "best_now": best,
            "booking_url": booking_url,
            "last_scan": scans[0]["finished_at"] if scans else None,
        },
        "by_date": by_date,
        "history": _daily_min(conn, signature),
        "alerts": alerts,
        "scans": [
            {
                "finished_at": s["finished_at"],
                "ok": s["pairs_ok"],
                "failed": s["pairs_failed"],
                "best_price": s["best_price"],
            }
            for s in scans
        ],
    }


def export_dashboard(cfg: AppConfig, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with session(cfg.db_file) as conn:
        payload = build_payload(conn, cfg)

    target = out_dir / "data.json"
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Dashboard data written to %s", target)
    return target
