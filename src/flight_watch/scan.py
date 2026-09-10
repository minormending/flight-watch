from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from .alerts import AlertDecision, evaluate, render_alert
from .config import AppConfig
from .models import Quote, trip_window
from .notifier import notify
from .sources import PriceSource, build_source
from .storage import finish_scan, record_alert, record_quotes, session, start_scan

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    quotes: list[Quote] = field(default_factory=list)
    failed: int = 0
    decision: Optional[AlertDecision] = None
    delivered: list[str] = field(default_factory=list)

    @property
    def best(self) -> Optional[Quote]:
        return min(self.quotes, key=lambda q: q.price) if self.quotes else None


def _sleep(base: float) -> None:
    """Jittered pause so the request pattern is not perfectly periodic."""
    if base > 0:
        time.sleep(random.uniform(base * 0.5, base * 1.5))


def collect(
    source: PriceSource,
    cfg: AppConfig,
    limit: Optional[int] = None,
) -> tuple[list[Quote], int]:
    trips = trip_window(
        cfg.search.stay_nights,
        cfg.search.window_start_days,
        cfg.search.window_end_days,
    )
    if limit:
        trips = trips[:limit]

    logger.info(
        "Scanning %d departure dates %s -> %s (%d nights, %s, %s) via %s",
        len(trips),
        cfg.search.origin,
        cfg.search.destination,
        cfg.search.stay_nights,
        cfg.search.party_label,
        cfg.search.cabin_label,
        source.name,
    )

    quotes: list[Quote] = []
    failed = 0

    for index, trip in enumerate(trips, start=1):
        try:
            quote = source.fetch(trip)
        except KeyboardInterrupt:
            logger.warning(
                "Interrupted after %d/%d dates; keeping what we have", index, len(trips)
            )
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s raised %s", trip.depart, exc)
            quote = None

        if quote is None:
            failed += 1
        else:
            quotes.append(quote)

        if index % 25 == 0 or index == len(trips):
            cheapest = min((q.price for q in quotes), default=None)
            logger.info(
                "  %d/%d done, %d failed, cheapest so far %s",
                index,
                len(trips),
                failed,
                f"${cheapest}" if cheapest else "n/a",
            )

        if index < len(trips):
            _sleep(cfg.scan.request_delay)

    return quotes, failed


def run_scan(
    cfg: AppConfig, limit: Optional[int] = None, dry_run: bool = False
) -> ScanResult:
    source = build_source(cfg.scan.source, cfg.search)
    source.max_retries = cfg.scan.max_retries  # type: ignore[attr-defined]

    quotes, failed = collect(source, cfg, limit=limit)
    result = ScanResult(quotes=quotes, failed=failed)

    with session(cfg.db_file) as conn:
        scan_id = start_scan(
            conn,
            cfg.search.origin,
            cfg.search.destination,
            cfg.search.stay_nights,
            len(quotes) + failed,
        )
        record_quotes(
            conn,
            scan_id,
            cfg.search.origin,
            cfg.search.destination,
            cfg.search.signature,
            source.name,
            quotes,
        )
        best = result.best
        finish_scan(conn, scan_id, len(quotes), failed, best.price if best else None)

        decision = evaluate(conn, cfg.search, cfg.alert, best)
        result.decision = decision

        if best is None:
            logger.error("Scan produced no quotes at all -- is the scraper broken?")
            return result

        logger.info(
            "Best: $%d on %s -> %s (%s)",
            best.price,
            best.depart_date,
            best.return_date,
            best.airlines or "unknown",
        )
        logger.info(
            "Alert decision: %s (%s)",
            "FIRE" if decision.fire else "hold",
            decision.reason,
        )

        if decision.fire and not dry_run:
            title, body = render_alert(best, decision, cfg.search)
            result.delivered = notify(cfg.notify, title, body)
            record_alert(
                conn,
                best,
                cfg.search.signature,
                decision.threshold or 0,
                decision.pool_size,
                result.delivered,
            )
        elif decision.fire and dry_run:
            logger.info("Dry run: alert suppressed")

    return result
