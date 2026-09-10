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


def cmd_scan(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .dashboard import export_dashboard
    from .scan import run_scan

    result = run_scan(cfg, limit=args.limit, dry_run=args.dry_run)

    if args.export:
        export_dashboard(cfg, Path(args.export))

    return 0 if result.quotes else 1


def cmd_report(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .dashboard import build_payload
    from .storage import session

    with session(cfg.db_file) as conn:
        data = build_payload(conn, cfg)

    if not data["scans"]:
        print("No scans recorded yet. Run: flight-watch scan")
        return 1

    summary = data["summary"]
    print(
        f"\n{cfg.search.origin} -> {cfg.search.destination},"
        f" {cfg.search.stay_nights} nights"
    )
    print(
        f"{cfg.search.party_label}, {cfg.search.seat_class.replace('-', ' ')},"
        f" {cfg.search.carry_on_bags} carry-on each"
    )
    print(
        f"Observations: {summary['observations']} over {summary['span_days']:.1f} days"
    )
    print(
        f"Cheapest ever: ${summary['all_time_low']}   Typical (p50): ${summary['median']}"
    )
    print(f"Alert threshold (p{cfg.alert.percentile:g}): ${summary['threshold']}\n")

    print("Cheapest departure dates seen recently:")
    cheapest = sorted(data["by_date"], key=lambda r: r["price"])
    for row in cheapest[: args.top]:
        print(
            f"  {row['depart']} -> {row['ret']}   ${row['price']:<6} {row['airlines']}"
        )

    if data["alerts"]:
        print("\nRecent alerts:")
        for a in data["alerts"][:5]:
            print(f"  {a['fired_at'][:16]}  ${a['price']}  {a['depart_date']}")
    print()
    return 0


def cmd_export(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .dashboard import export_dashboard

    path = export_dashboard(cfg, Path(args.out))
    print(f"Wrote {path}")
    return 0


def cmd_notify_test(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .notifier import notify

    delivered = notify(
        cfg.notify,
        f"flight-watch test ({cfg.search.origin}->{cfg.search.destination})",
        "If you can read this, notifications are wired up correctly.",
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

    scan = sub.add_parser("scan", help="Sweep the date window and alert if cheap")
    scan.add_argument(
        "--limit", type=int, help="Only check the first N dates (testing)"
    )
    scan.add_argument("--dry-run", action="store_true", help="Never send notifications")
    scan.add_argument("--export", metavar="DIR", help="Also refresh the dashboard here")
    scan.set_defaults(func=cmd_scan)

    report = sub.add_parser("report", help="Print what we know so far")
    report.add_argument("--top", type=int, default=10, help="How many dates to list")
    report.set_defaults(func=cmd_report)

    export = sub.add_parser("export", help="Write the dashboard data file")
    export.add_argument(
        "--out", default="docs", help="Output directory (default: docs)"
    )
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
