from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .models import DateRule

load_dotenv()

DEFAULT_HOME = Path.home() / ".flight_watch"


SEAT_CLASSES = ("economy", "premium-economy", "business", "first")


@dataclass(frozen=True)
class SearchConfig:
    """Everything that defines *what* is being priced, for one destination.

    Named for the whole search rather than just the route because the party,
    cabin and stop limit belong here too: a 2-adult nonstop premium-economy
    fare and a 1-adult connecting economy fare are different products, and
    pooling their prices would make the percentile baseline meaningless.

    One of these per destination, produced by WatchConfig.searches().
    """

    watch: str
    origin: str
    destination: str
    dates: DateRule
    max_stops: int | None
    adults: int
    children: int
    infants_in_seat: int
    infants_on_lap: int
    seat_class: str
    carry_on_bags: int
    checked_bags: int
    exclude_basic_economy: bool

    @property
    def cabin_label(self) -> str:
        cabin = self.seat_class.replace("-", " ")
        return f"{cabin} (excl. Basic)" if self.exclude_basic_economy else cabin

    @property
    def stops_label(self) -> str:
        if self.max_stops is None:
            return "any stops"
        return "nonstop only" if self.max_stops == 0 else f"max {self.max_stops} stops"

    @property
    def party_size(self) -> int:
        return self.adults + self.children + self.infants_in_seat + self.infants_on_lap

    @property
    def party_label(self) -> str:
        bits = []
        for count, noun in (
            (self.adults, "adult"),
            (self.children, "child"),
            (self.infants_in_seat + self.infants_on_lap, "infant"),
        ):
            if count:
                plural = (
                    "ren" if noun == "child" and count > 1 else "s" if count > 1 else ""
                )
                bits.append(f"{count} {noun}{plural}")
        return ", ".join(bits) or "no passengers"

    @property
    def signature(self) -> str:
        """Stable key for exactly what was priced.

        Observations recorded under a different signature never enter the same
        baseline, so changing the party, cabin, stop limit or fixed travel
        dates starts a clean history instead of corrupting the old one.
        """
        stops = "any" if self.max_stops is None else str(self.max_stops)
        return (
            f"{self.origin}-{self.destination}"
            f":{self.dates.key}"
            f":stops{stops}"
            f":{self.adults}a{self.children}c"
            f"{self.infants_in_seat}is{self.infants_on_lap}il"
            f":{self.seat_class}"
            f"{'-nobasic' if self.exclude_basic_economy else ''}"
            f":carry{self.carry_on_bags}:checked{self.checked_bags}"
        )


@dataclass
class WatchConfig:
    """One thing being watched: an origin, a set of destinations, a date rule.

    Expands into one SearchConfig per destination so each city keeps its own
    price history -- $400 to Reykjavik and $400 to Tokyo are not equally good,
    and a shared baseline would rank them as if they were.
    """

    name: str
    origin: str
    destinations: tuple[str, ...]
    dates: DateRule
    max_stops: int | None
    adults: int
    children: int
    infants_in_seat: int
    infants_on_lap: int
    seat_class: str
    carry_on_bags: int
    checked_bags: int
    exclude_basic_economy: bool
    top_n: int = 0
    full_scan_interval_hours: float = 20.0
    candidates: tuple[str, ...] = ()
    proxy_adults: int | None = None
    """Scan as this many adults instead of the real party, when set.

    Google defers results to a client-side request when a child is in the
    party, and the static payload the scraper reads comes back empty for about
    two thirds of international destinations. Pricing as adults keeps coverage;
    the real party is re-priced once, at alert time, by confirm_search().
    """

    @property
    def is_proxy(self) -> bool:
        return self.proxy_adults is not None

    def _party(self, proxy: bool) -> tuple[int, int, int, int]:
        if proxy and self.proxy_adults is not None:
            return (self.proxy_adults, 0, 0, 0)
        return (self.adults, self.children, self.infants_in_seat, self.infants_on_lap)

    def _search(self, dest: str, proxy: bool) -> SearchConfig:
        adults, children, in_seat, on_lap = self._party(proxy)
        return SearchConfig(
            watch=self.name,
            origin=self.origin,
            destination=dest,
            dates=self.dates,
            max_stops=self.max_stops,
            adults=adults,
            children=children,
            infants_in_seat=in_seat,
            infants_on_lap=on_lap,
            seat_class=self.seat_class,
            carry_on_bags=self.carry_on_bags,
            checked_bags=self.checked_bags,
            exclude_basic_economy=self.exclude_basic_economy,
        )

    def confirm_search(self, dest: str) -> SearchConfig:
        """The real party, used to re-price a destination before alerting."""
        return self._search(dest, proxy=False)

    def searches(self) -> list[SearchConfig]:
        return [self._search(dest, proxy=True) for dest in self.destinations]

    def party_size_check(self) -> int:
        return self.adults + self.children + self.infants_in_seat + self.infants_on_lap

    @property
    def requests_per_full_scan(self) -> int:
        return len(self.destinations) * len(self.dates.trips())


@dataclass
class ScanConfig:
    source: str
    request_delay: float
    max_retries: int


@dataclass
class AlertConfig:
    percentile: float
    baseline_days: int
    price_ceiling: int | None
    cooldown_hours: float
    rearm_drop_pct: float
    warmup_observations: int
    warmup_days: int


@dataclass
class NotifyConfig:
    ntfy_topic: str | None
    ntfy_server: str
    gmail_username: str | None
    gmail_app_password: str | None
    alert_to_email: str | None

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_topic)

    @property
    def email_enabled(self) -> bool:
        return bool(
            self.gmail_username and self.gmail_app_password and self.alert_to_email
        )


@dataclass
class LoggingConfig:
    level: str
    log_file: Path


@dataclass
class AppConfig:
    watches: list[WatchConfig]
    scan: ScanConfig
    alert: AlertConfig
    notify: NotifyConfig
    logging: LoggingConfig
    db_file: Path

    def watch(self, name: str) -> WatchConfig:
        for w in self.watches:
            if w.name == name:
                return w
        known = ", ".join(w.name for w in self.watches)
        raise RuntimeError(f"No watch named {name!r}. Known watches: {known}")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _opt(name: str) -> str | None:
    value = os.getenv(name)
    return None if value is None or value.strip() == "" else value.strip()


def _path(name: str, fallback: Path) -> Path:
    raw = _opt(name)
    path = Path(raw).expanduser() if raw else fallback
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_WATCHES_FILE = Path(__file__).resolve().parents[2] / "watches.toml"


def _watch_from_toml(name: str, raw: dict, defaults: dict) -> WatchConfig:
    def get(key, fallback=None):
        return raw.get(key, defaults.get(key, fallback))

    seat_class = str(get("seat_class", "economy")).strip().lower()
    if seat_class not in SEAT_CLASSES:
        raise RuntimeError(
            f"watch {name!r}: seat_class must be one of {', '.join(SEAT_CLASSES)}"
        )

    stays = get("stay_nights", [4])
    if isinstance(stays, int):
        stays = [stays]

    mode = str(get("mode", "rolling")).strip().lower()
    if mode == "fixed":
        dates = DateRule(
            mode="fixed",
            stay_nights=tuple(int(n) for n in stays),
            depart_from=_as_date(name, get("depart_from")),
            depart_to=_as_date(name, get("depart_to")),
        )
    else:
        dates = DateRule(
            mode="rolling",
            stay_nights=tuple(int(n) for n in stays),
            start_days=int(get("window_start_days", 21)),
            end_days=int(get("window_end_days", 180)),
        )

    max_stops = get("max_stops", None)

    watch = WatchConfig(
        name=name,
        origin=str(get("origin", "NYC")).upper(),
        destinations=tuple(str(d).upper() for d in get("destinations", [])),
        dates=dates,
        max_stops=None if max_stops is None else int(max_stops),
        adults=int(get("adults", 1)),
        children=int(get("children", 0)),
        infants_in_seat=int(get("infants_in_seat", 0)),
        infants_on_lap=int(get("infants_on_lap", 0)),
        seat_class=seat_class,
        carry_on_bags=int(get("carry_on_bags", 0)),
        checked_bags=int(get("checked_bags", 0)),
        exclude_basic_economy=bool(get("exclude_basic_economy", True)),
        proxy_adults=(
            int(raw["proxy_adults"]) if raw.get("proxy_adults") is not None else None
        ),
        top_n=int(get("top_n", 0)),
        full_scan_interval_hours=float(get("full_scan_interval_hours", 20.0)),
        candidates=tuple(str(d).upper() for d in get("candidates", [])),
    )
    if watch.party_size_check() < 1:
        raise RuntimeError(f"watch {name!r}: at least one passenger is required")
    return watch


def _as_date(name: str, value) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise RuntimeError(f"watch {name!r}: fixed mode needs depart_from and depart_to")


def _merge_discovered(watch: WatchConfig, home: Path) -> WatchConfig:
    """Fold in destinations found by `discover` for watches that list none."""
    if watch.destinations:
        return watch
    from .discovery import load_destinations

    found = load_destinations(home, watch.name)
    if found:
        watch.destinations = tuple(found)
    return watch


def load_watches(
    path: Path | None = None, home: Path | None = None
) -> list[WatchConfig]:
    path = (
        path or Path(os.getenv("FW_WATCHES_FILE") or DEFAULT_WATCHES_FILE).expanduser()
    )
    if not path.exists():
        raise RuntimeError(f"No watches file at {path}. Copy watches.example.toml.")

    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults = doc.get("defaults", {})
    home = home or DEFAULT_HOME
    watches = [
        _merge_discovered(_watch_from_toml(name, raw, defaults), home)
        for name, raw in doc.get("watch", {}).items()
    ]
    if not watches:
        raise RuntimeError(f"{path} defines no [watch.*] sections")
    return watches


def load_config() -> AppConfig:
    ceiling = _opt("FW_PRICE_CEILING")

    return AppConfig(
        watches=load_watches(),
        scan=ScanConfig(
            source=_env("FW_SOURCE", "google-flights"),
            request_delay=float(_env("FW_REQUEST_DELAY", "2.5")),
            max_retries=int(_env("FW_MAX_RETRIES", "3")),
        ),
        alert=AlertConfig(
            percentile=float(_env("FW_ALERT_PERCENTILE", "20")),
            baseline_days=int(_env("FW_BASELINE_DAYS", "30")),
            price_ceiling=int(ceiling) if ceiling else None,
            cooldown_hours=float(_env("FW_COOLDOWN_HOURS", "20")),
            rearm_drop_pct=float(_env("FW_REARM_DROP_PCT", "10")),
            warmup_observations=int(_env("FW_WARMUP_OBSERVATIONS", "300")),
            warmup_days=int(_env("FW_WARMUP_DAYS", "3")),
        ),
        notify=NotifyConfig(
            ntfy_topic=_opt("FW_NTFY_TOPIC"),
            ntfy_server=_env("FW_NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            gmail_username=_opt("GMAIL_USERNAME"),
            gmail_app_password=_opt("GMAIL_APP_PASSWORD"),
            alert_to_email=_opt("ALERT_TO_EMAIL"),
        ),
        logging=LoggingConfig(
            level=_env("FW_LOG_LEVEL", "INFO").upper(),
            log_file=_path("FW_LOG_FILE", DEFAULT_HOME / "flight_watch.log"),
        ),
        db_file=_path("FW_DB_FILE", DEFAULT_HOME / "flight_watch.db"),
    )
