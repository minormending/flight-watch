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
    quote: Quote,
    decision: AlertDecision,
    search: SearchConfig,
    watch=None,
    confirmed: Quote | None = None,
) -> tuple[str, str]:
    """Build the (title, body) pair sent to every notification channel.

    `confirmed` is a re-price of the same trip using the watch's real party,
    for watches that scan under a proxy party. When Google withholds a price
    for the real party -- which is why the proxy exists -- it is None and the
    body says so rather than quietly presenting the proxy as exact.
    """
    headline = confirmed.price if confirmed else quote.price
    title = (
        f"${headline} {search.origin}->{search.destination} "
        f"{quote.depart_date} ({quote.nights_label})"
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

    lines = [
        f"{search.origin} -> {search.destination}, {quote.nights_label}",
        f"Out {quote.depart_date}  /  Back {quote.return_date}",
    ]

    proxy = watch is not None and getattr(watch, "is_proxy", False)
    true_party = watch.confirm_search(search.destination) if proxy else search
    if proxy and confirmed is not None:
        each = round(confirmed.price / true_party.party_size)
        lines += [
            f"${confirmed.price} {confirmed.currency} total for"
            f" {true_party.party_label} (~${each} each)",
            f"(found at ${quote.price} pricing as"
            f" {search.party_label}; re-checked for your party)",
        ]
    elif proxy:
        lines += [
            f"${quote.price} {quote.currency} priced as {search.party_label}",
            f"APPROXIMATE -- Google would not price {true_party.party_label}"
            " for this trip. Expect within about 1%, but check before booking.",
        ]
    else:
        each = round(quote.price / search.party_size)
        lines += [
            f"${quote.price} {quote.currency} total for {search.party_label}"
            f" (~${each} each)",
        ]

    lines += [
        f"{search.cabin_label}, {search.carry_on_bags} carry-on each,"
        f" {search.stops_label}",
        "",
        f"Airline: {quote.airlines or 'unknown'}",
        f"Outbound: {stops}, {duration}",
        "",
        decision.reason,
        f"(baseline: {decision.pool_size} observations)",
        "",
        (confirmed or quote).booking_url,
    ]
    if search.destination == "SJU":
        lines += ["", "Southwest never appears in Google Flights -- check separately."]
    return title, "\n".join(lines)
