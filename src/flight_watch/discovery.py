from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path

from .config import AppConfig, WatchConfig
from .sources import build_source

logger = logging.getLogger(__name__)


def destinations_file(home: Path, watch: str) -> Path:
    return home / "destinations" / f"{watch}.json"


def load_destinations(home: Path, watch: str) -> list[str]:
    path = destinations_file(home, watch)
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8"))["destinations"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return []


def save_destinations(home: Path, watch: str, found: dict[str, int]) -> Path:
    """Persist discovered destinations outside watches.toml.

    Keeping them in a sidecar means discovery never has to rewrite the file a
    human maintains, and a bad discovery run can be undone by deleting it.
    """
    path = destinations_file(home, watch)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(found, key=lambda d: found[d])
    path.write_text(
        json.dumps({"destinations": ordered, "cheapest": found}, indent=2),
        encoding="utf-8",
    )
    return path


def discover(
    cfg: AppConfig,
    watch: WatchConfig,
    probes: int = 3,
    limit: int | None = None,
) -> dict[str, int]:
    """Probe every candidate and keep the ones that actually return a price.

    A destination is only dropped after every probe date comes back empty:
    availability varies by date, so a single blank day is not evidence that
    there is no nonstop. Probes stop at the first hit, so a viable destination
    usually costs one request and a dead one costs `probes`.
    """
    trips = watch.dates.trips()
    if not trips:
        raise RuntimeError(f"watch {watch.name!r} generates no trip dates")

    # Spread the probes across the window rather than clustering on day one.
    picks = sorted({0, len(trips) // 2, len(trips) - 1})[:probes]
    probe_trips = [trips[i] for i in picks]

    candidates = list(watch.candidates or watch.destinations)
    if limit:
        candidates = candidates[:limit]

    logger.info(
        "Probing %d candidates for %s (%s, %s, %d date%s each)",
        len(candidates),
        watch.name,
        watch.dates.stay_label,
        watch.searches()[0].stops_label if watch.destinations else "nonstop only",
        len(probe_trips),
        "" if len(probe_trips) == 1 else "s",
    )

    found: dict[str, int] = {}
    for index, dest in enumerate(candidates, start=1):
        source = build_source(cfg.scan.source, watch._search(dest, proxy=True))
        source.max_retries = 1  # type: ignore[attr-defined]

        for trip in probe_trips:
            try:
                quote = source.fetch(trip)
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s probe raised %s", dest, exc)
                quote = None
            if quote is not None:
                found[dest] = quote.price
                break
            time.sleep(random.uniform(0.4, 1.0))

        if index % 25 == 0 or index == len(candidates):
            logger.info("  probed %d/%d, %d viable", index, len(candidates), len(found))
        time.sleep(random.uniform(cfg.scan.request_delay * 0.4, cfg.scan.request_delay))

    return found
