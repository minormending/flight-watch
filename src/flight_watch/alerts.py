from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from .config import AlertConfig, SearchConfig
from .models import Quote
from .storage import baseline_prices, history_span_days, last_alert


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile. `p` is 0-100."""
    if not values:
        raise ValueError("percentile() of empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(ordered[int(k)])
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


@dataclass
class AlertDecision:
    fire: bool
    reason: str
    threshold: Optional[int] = None
    pool_size: int = 0


def evaluate(
    conn: sqlite3.Connection,
    search: SearchConfig,
    cfg: AlertConfig,
    best: Optional[Quote],
) -> AlertDecision:
    """Decide whether `best` is cheap enough to be worth waking someone up.

    "Cheap" is defined against this route's own recent history rather than a
    fixed number, because the only way to know a fare is good is to have
    watched it for a while.
    """
    if best is None:
        return AlertDecision(False, "no quotes in this scan")

    pool = baseline_prices(conn, search.signature, cfg.baseline_days)
    span = history_span_days(conn, search.signature)

    if len(pool) < cfg.warmup_observations or span < cfg.warmup_days:
        return AlertDecision(
            False,
            f"warming up ({len(pool)}/{cfg.warmup_observations} observations, "
            f"{span:.1f}/{cfg.warmup_days} days)",
            pool_size=len(pool),
        )

    threshold = int(percentile(pool, cfg.percentile))

    if cfg.price_ceiling is not None and best.price > cfg.price_ceiling:
        return AlertDecision(
            False,
            f"${best.price} above ceiling ${cfg.price_ceiling}",
            threshold,
            len(pool),
        )

    if best.price > threshold:
        return AlertDecision(
            False,
            f"${best.price} above p{cfg.percentile:g} threshold ${threshold}",
            threshold,
            len(pool),
        )

    previous = last_alert(conn, search.signature)
    if previous is not None:
        age_hours = (
            datetime.now(timezone.utc) - datetime.fromisoformat(previous["fired_at"])
        ).total_seconds() / 3600.0
        if age_hours < cfg.cooldown_hours:
            rearm_at = int(previous["price"] * (1 - cfg.rearm_drop_pct / 100.0))
            if best.price > rearm_at:
                return AlertDecision(
                    False,
                    f"cooldown ({age_hours:.1f}h < {cfg.cooldown_hours:g}h); "
                    f"needs <= ${rearm_at} to re-arm",
                    threshold,
                    len(pool),
                )

    return AlertDecision(
        True,
        f"${best.price} at or below p{cfg.percentile:g} threshold ${threshold}",
        threshold,
        len(pool),
    )


def render_alert(
    quote: Quote, decision: AlertDecision, search: SearchConfig
) -> tuple[str, str]:
    """Build the (title, body) pair sent to every notification channel."""
    title = (
        f"${quote.price} {search.origin}->{search.destination} "
        f"{quote.depart_date} ({search.stay_nights}n, {search.party_size} pax)"
    )
    stops = (
        "nonstop"
        if quote.stops == 0
        else (
            f"{quote.stops} stop{'s' if (quote.stops or 0) > 1 else ''}"
            if quote.stops is not None
            else "unknown routing"
        )
    )
    duration = (
        f"{quote.duration_minutes // 60}h {quote.duration_minutes % 60}m"
        if quote.duration_minutes
        else "unknown"
    )
    per_person = round(quote.price / search.party_size)
    cabin = search.seat_class.replace("-", " ")
    body = "\n".join(
        [
            f"{search.origin} -> {search.destination}, {search.stay_nights} nights",
            f"Out {quote.depart_date}  /  Back {quote.return_date}",
            f"${quote.price} {quote.currency} total for {search.party_label}"
            f" (~${per_person} each)",
            f"{cabin}, {search.carry_on_bags} carry-on each",
            "",
            f"Airline: {quote.airlines or 'unknown'}",
            f"Outbound: {stops}, {duration}",
            "",
            decision.reason,
            f"(baseline: {decision.pool_size} observations)",
            "",
            quote.booking_url,
            "",
            "Southwest never appears in Google Flights -- worth a separate check.",
        ]
    )
    return title, body
