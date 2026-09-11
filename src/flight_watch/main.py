from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import AppConfig, load_config


def configure_logging(level_name: str, log_file: Path) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    rotating.setFormatter(formatter)
    root.addHandler(rotating)

    # fast-flights' HTTP layer logs every request at INFO; one line per date is noise.
    logging.getLogger("primp").setLevel(logging.WARNING)


logger = logging.getLogger("flight_watch")


def _selected(cfg: AppConfig, name: str | None):
    return [cfg.watch(name)] if name else cfg.watches


def cmd_scan(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .dashboard import export_dashboard
    from .scan import run_scan

    produced = 0
    for watch in _selected(cfg, args.watch):
        if not watch.destinations:
            logger.warning(
                "Skipping %s: no destinations. Run: flight-watch discover --watch %s",
                watch.name,
                watch.name,
            )
            continue
        result = run_scan(
            cfg, watch, tier=args.tier, limit=args.limit, dry_run=args.dry_run
        )
        produced += len(result.quotes)

    if args.export:
        export_dashboard(cfg, Path(args.export))
    return 0 if produced else 1


def cmd_discover(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .config import DEFAULT_HOME
    from .discovery import discover, save_destinations

    for watch in _selected(cfg, args.watch):
        if not watch.candidates:
            logger.info("Skipping %s: no candidates listed", watch.name)
            continue
        found = discover(cfg, watch, probes=args.probes, limit=args.limit)
        if not found:
            logger.error("%s: no candidate returned a price", watch.name)
            continue
        path = save_destinations(DEFAULT_HOME, watch.name, found)
        ranked = sorted(found.items(), key=lambda kv: kv[1])
        print(f"\n{watch.name}: {len(found)} viable of {len(watch.candidates)} probed")
        for dest, price in ranked[: args.show]:
            print(f"  {dest}  ${price}")
        if len(ranked) > args.show:
            print(f"  ... and {len(ranked) - args.show} more")
        print(f"Saved to {path}")
    return 0


def cmd_report(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .dashboard import build_payload
    from .storage import session

    with session(cfg.db_file) as conn:
        payload = build_payload(conn, cfg)

    shown = 0
    for block in payload["watches"]:
        if args.watch and block["name"] != args.watch:
            continue
        shown += 1
        s, r = block["summary"], block["route"]
        print(f"\n=== {block['name']} ===")
        print(f"{r['origin']} -> {r['scope']}, {r['stay_label']}, {r['stops_label']}")
        print(
            f"{r['party_label']}, {r['cabin']}"
            + (f"  [scanned as {r['proxy_label']}]" if r.get("proxy_label") else "")
        )
        if not s["observations"]:
            print("No observations yet.")
            continue
        print(
            f"Observations: {s['observations']} over {s['span_days']:.1f} days"
            f" | cheapest ever ${s['all_time_low']} | typical ${s['median']}"
        )
        ranked = sorted(block["rows"], key=lambda r: r["price"])
        for row in ranked[: args.top]:
            print(f"  {row['label']:<28} ${row['price']:<7} {row['airlines']}")

    if not shown:
        print("No matching watch.")
        return 1
    print()
    return 0


def cmd_watches(cfg: AppConfig, args: argparse.Namespace) -> int:
    for w in cfg.watches:
        combos = len(w.dates.trips())
        print(f"{w.name}")
        print(
            f"  {w.origin} -> {len(w.destinations)} destination(s), {w.dates.stay_label}"
        )
        print(
            f"  {w.dates.mode} dates, {combos} combos, {w.requests_per_full_scan} requests per full sweep"
        )
        if w.top_n:
            print(
                f"  tiered: top {w.top_n} between full sweeps every {w.full_scan_interval_hours:g}h"
            )
        if w.is_proxy:
            print(
                f"  scanned as {w.proxy_adults} adults (proxy), alerts re-price the real party"
            )
    return 0


def cmd_export(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .dashboard import export_dashboard

    print(f"Wrote {export_dashboard(cfg, Path(args.out))}")
    return 0


def cmd_notify_test(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .notifier import notify

    delivered = notify(
        cfg.notify, "flight-watch test", "Notifications are wired up correctly."
    )
    if delivered:
        print(f"Delivered via: {', '.join(delivered)}")
        return 0
    print("No channel delivered. Set FW_NTFY_TOPIC in .env.", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flight-watch",
        description="Watch Google Flights for cheap flexible-date trips.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Sweep a watch and alert if cheap")
    scan.add_argument("--watch", help="Watch name (default: all)")
    scan.add_argument("--tier", choices=("full", "top"), help="Override tier selection")
    scan.add_argument(
        "--limit", type=int, help="Only the first N destinations (testing)"
    )
    scan.add_argument("--dry-run", action="store_true", help="Never send notifications")
    scan.add_argument("--export", metavar="DIR", help="Also refresh the dashboard here")
    scan.set_defaults(func=cmd_scan)

    disc = sub.add_parser("discover", help="Find which candidate destinations fly")
    disc.add_argument("--watch", help="Watch name (default: all with candidates)")
    disc.add_argument(
        "--probes", type=int, default=3, help="Dates tried before giving up"
    )
    disc.add_argument("--limit", type=int, help="Only the first N candidates (testing)")
    disc.add_argument("--show", type=int, default=25, help="How many to print")
    disc.set_defaults(func=cmd_discover)

    report = sub.add_parser("report", help="Print what we know so far")
    report.add_argument("--watch", help="Watch name (default: all)")
    report.add_argument("--top", type=int, default=10, help="Rows per watch")
    report.set_defaults(func=cmd_report)

    listing = sub.add_parser("watches", help="Show configured watches")
    listing.set_defaults(func=cmd_watches)

    export = sub.add_parser("export", help="Write the dashboard data file")
    export.add_argument("--out", default="docs", help="Output directory")
    export.set_defaults(func=cmd_export)

    test = sub.add_parser("notify-test", help="Send a test notification")
    test.set_defaults(func=cmd_notify_test)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config()
    configure_logging(cfg.logging.level, cfg.logging.log_file)
    try:
        sys.exit(args.func(cfg, args))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
