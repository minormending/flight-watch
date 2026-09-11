from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .alerts import AlertDecision, evaluate, render_alert
from .config import AppConfig, SearchConfig, WatchConfig
from .models import Quote, TripDates
from .notifier import notify
from .sources import PriceSource, build_source
from .storage import (
    cheapest_destinations,
    finish_scan,
    last_full_scan_age_hours,
    record_alert,
    record_quotes,
    session,
    start_scan,
)

logger = logging.getLogger(__name__)


@dataclass
class DestinationResult:
    search: SearchConfig
    quotes: list[Quote] = field(default_factory=list)
    failed: int = 0
    decision: Optional[AlertDecision] = None

    @property
    def best(self) -> Optional[Quote]:
        return min(self.quotes, key=lambda q: q.price) if self.quotes else None


@dataclass
class ScanResult:
    watch: str
    tier: str
    destinations: list[DestinationResult] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    alerted: Optional[DestinationResult] = None

    @property
    def quotes(self) -> list[Quote]:
        return [q for d in self.destinations for q in d.quotes]

    @property
    def best(self) -> Optional[DestinationResult]:
        scored = [d for d in self.destinations if d.best]
        if not scored:
            return None
        return min(scored, key=lambda d: d.best.price)  # type: ignore[union-attr]


def _sleep(base: float) -> None:
    """Jittered pause so the request pattern is not perfectly periodic."""
    if base > 0:
        time.sleep(random.uniform(base * 0.5, base * 1.5))


def choose_tier(conn, watch: WatchConfig, override: str | None) -> str:
    """Full sweep if one is due, otherwise just the cheapest destinations.

    With a 10-hour agent and a 20-hour full interval this alternates
    full / top / full / top, so every destination is refreshed about daily
    while the promising ones are re-checked in between.
    """
    if override:
        return override
    if not watch.top_n or len(watch.destinations) <= watch.top_n:
        return "full"
    age = last_full_scan_age_hours(conn, watch.name)
    if age is None or age >= watch.full_scan_interval_hours:
        return "full"
    return "top"


def collect(
    source: PriceSource,
    search: SearchConfig,
    trips: list[TripDates],
    delay: float,
) -> tuple[list[Quote], int]:
    quotes: list[Quote] = []
    failed = 0
    for index, trip in enumerate(trips, start=1):
        try:
            quote = source.fetch(trip)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s %s raised %s", search.destination, trip.depart, exc)
            quote = None

        if quote is None:
            failed += 1
        else:
            quotes.append(quote)
        if index < len(trips):
            _sleep(delay)
    return quotes, failed


def _confirm_price(
    cfg: AppConfig, watch: WatchConfig, result: DestinationResult
) -> Optional[Quote]:
    """Re-price the alerting trip with the watch's real party.

    Only meaningful for a proxy watch. Returns None when Google withholds a
    price for the real party, which is exactly why the proxy exists.
    """
    if not watch.is_proxy or result.best is None:
        return None

    best = result.best
    search = watch.confirm_search(result.search.destination)
    source = build_source(cfg.scan.source, search)
    source.max_retries = cfg.scan.max_retries  # type: ignore[attr-defined]
    trip = TripDates(
        depart=date.fromisoformat(best.depart_date),
        ret=date.fromisoformat(best.return_date),
    )
    try:
        return source.fetch(trip)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Confirmation re-price failed: %s", exc)
        return None


def run_scan(
    cfg: AppConfig,
    watch: WatchConfig,
    tier: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> ScanResult:
    trips = watch.dates.trips()

    with session(cfg.db_file) as conn:
        chosen = choose_tier(conn, watch, tier)
        searches = watch.searches()
        if chosen == "top":
            sigs = [s.signature for s in searches]
            keep = set(cheapest_destinations(conn, sigs, watch.top_n))
            if keep:
                searches = [s for s in searches if s.destination in keep]

    if limit:
        searches = searches[:limit]

    if not searches:
        raise RuntimeError(
            f"watch {watch.name!r} has no destinations. "
            f"Run: flight-watch discover --watch {watch.name}"
        )

    logger.info(
        "Scan %s [%s]: %d destination%s x %d date%s = %d requests (%s, %s, %s)",
        watch.name,
        chosen,
        len(searches),
        "" if len(searches) == 1 else "s",
        len(trips),
        "" if len(trips) == 1 else "s",
        len(searches) * len(trips),
        watch.dates.stay_label,
        searches[0].stops_label,
        (
            f"proxy {watch.proxy_adults} adults"
            if watch.is_proxy
            else searches[0].party_label
        ),
    )

    result = ScanResult(watch=watch.name, tier=chosen)

    for index, search in enumerate(searches, start=1):
        source = build_source(cfg.scan.source, search)
        source.max_retries = cfg.scan.max_retries  # type: ignore[attr-defined]
        try:
            quotes, failed = collect(source, search, trips, cfg.scan.request_delay)
        except KeyboardInterrupt:
            logger.warning("Interrupted after %d/%d destinations", index, len(searches))
            break

        dest_result = DestinationResult(search=search, quotes=quotes, failed=failed)
        result.destinations.append(dest_result)

        with session(cfg.db_file) as conn:
            scan_id = start_scan(
                conn,
                search.origin,
                search.destination,
                min(search.dates.stay_nights),
                len(trips),
                watch=watch.name,
                tier=chosen,
            )
            record_quotes(
                conn,
                scan_id,
                search.origin,
                search.destination,
                search.signature,
                source.name,
                quotes,
                watch=watch.name,
            )
            best = dest_result.best
            finish_scan(
                conn, scan_id, len(quotes), failed, best.price if best else None
            )
            dest_result.decision = evaluate(conn, search, cfg.alert, best)

        if index % 10 == 0 or index == len(searches):
            top = result.best
            logger.info(
                "  %d/%d destinations, cheapest %s",
                index,
                len(searches),
                (
                    f"${top.best.price} {top.search.destination}"
                    if top and top.best
                    else "n/a"
                ),
            )
        if index < len(searches):
            _sleep(cfg.scan.request_delay)

    _maybe_alert(cfg, watch, result, dry_run)
    return result


def _maybe_alert(
    cfg: AppConfig, watch: WatchConfig, result: ScanResult, dry_run: bool
) -> None:
    firing = [
        d for d in result.destinations if d.decision and d.decision.fire and d.best
    ]
    if not firing:
        top = result.best
        if top and top.best:
            logger.info(
                "Best: $%d %s on %s (%s) -- no alert",
                top.best.price,
                top.search.destination,
                top.best.depart_date,
                top.decision.reason if top.decision else "",
            )
        else:
            logger.error("Scan produced no quotes at all -- is the scraper broken?")
        return

    # One alert per scan: the cheapest qualifying destination. Firing on every
    # qualifying city at once would be a pile of notifications for one decision.
    winner = min(firing, key=lambda d: d.best.price)  # type: ignore[union-attr]
    result.alerted = winner
    best = winner.best
    assert best is not None and winner.decision is not None

    logger.info(
        "ALERT %s $%d on %s (%s)",
        winner.search.destination,
        best.price,
        best.depart_date,
        winner.decision.reason,
    )
    if dry_run:
        logger.info("Dry run: alert suppressed")
        return

    confirmed = _confirm_price(cfg, watch, winner)
    title, body = render_alert(
        best, winner.decision, winner.search, watch=watch, confirmed=confirmed
    )
    result.delivered = notify(cfg.notify, title, body)

    with session(cfg.db_file) as conn:
        record_alert(
            conn,
            best,
            winner.search.signature,
            winner.decision.threshold or 0,
            winner.decision.pool_size,
            result.delivered,
        )
