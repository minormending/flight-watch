from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_HOME = Path.home() / ".flight_watch"


SEAT_CLASSES = ("economy", "premium-economy", "business", "first")


@dataclass
class SearchConfig:
    """Everything that defines *what* is being priced.

    Named for the whole search rather than just the route because the party and
    cabin belong here too: a 2-adult premium-economy fare and a 1-adult economy
    fare are different products, and pooling their prices would make the
    percentile baseline meaningless.
    """

    origin: str
    destination: str
    stay_nights: int
    window_start_days: int
    window_end_days: int
    adults: int
    children: int
    infants_in_seat: int
    infants_on_lap: int
    seat_class: str
    carry_on_bags: int
    checked_bags: int

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
        baseline, so changing the party or cabin starts a clean history instead
        of corrupting the old one.
        """
        return (
            f"{self.origin}-{self.destination}"
            f":{self.stay_nights}n"
            f":{self.adults}a{self.children}c"
            f"{self.infants_in_seat}is{self.infants_on_lap}il"
            f":{self.seat_class}"
            f":carry{self.carry_on_bags}:checked{self.checked_bags}"
        )


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
    search: SearchConfig
    scan: ScanConfig
    alert: AlertConfig
    notify: NotifyConfig
    logging: LoggingConfig
    db_file: Path


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


def load_config() -> AppConfig:
    seat_class = _env("FW_SEAT_CLASS", "premium-economy").strip().lower()
    if seat_class not in SEAT_CLASSES:
        raise RuntimeError(
            f"FW_SEAT_CLASS must be one of {', '.join(SEAT_CLASSES)}; got {seat_class!r}"
        )

    search = SearchConfig(
        origin=_env("FW_ORIGIN", "NYC").upper(),
        destination=_env("FW_DESTINATION", "SJU").upper(),
        stay_nights=int(_env("FW_STAY_NIGHTS", "4")),
        window_start_days=int(_env("FW_WINDOW_START_DAYS", "21")),
        window_end_days=int(_env("FW_WINDOW_END_DAYS", "180")),
        adults=int(_env("FW_ADULTS", "2")),
        children=int(_env("FW_CHILDREN", "1")),
        infants_in_seat=int(_env("FW_INFANTS_IN_SEAT", "0")),
        infants_on_lap=int(_env("FW_INFANTS_ON_LAP", "0")),
        seat_class=seat_class,
        carry_on_bags=int(_env("FW_CARRY_ON_BAGS", "1")),
        checked_bags=int(_env("FW_CHECKED_BAGS", "0")),
    )
    if search.window_end_days <= search.window_start_days:
        raise RuntimeError(
            "FW_WINDOW_END_DAYS must be greater than FW_WINDOW_START_DAYS"
        )
    if search.party_size < 1:
        raise RuntimeError("At least one passenger is required")

    ceiling = _opt("FW_PRICE_CEILING")

    return AppConfig(
        search=search,
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
