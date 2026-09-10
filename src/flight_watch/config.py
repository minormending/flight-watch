from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_HOME = Path.home() / ".flight_watch"


@dataclass
class RouteConfig:
    origin: str
    destination: str
    stay_nights: int
    window_start_days: int
    window_end_days: int


@dataclass
class ScanConfig:
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
    route: RouteConfig
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
    route = RouteConfig(
        origin=_env("FW_ORIGIN", "NYC").upper(),
        destination=_env("FW_DESTINATION", "SJU").upper(),
        stay_nights=int(_env("FW_STAY_NIGHTS", "4")),
        window_start_days=int(_env("FW_WINDOW_START_DAYS", "21")),
        window_end_days=int(_env("FW_WINDOW_END_DAYS", "180")),
    )
    if route.window_end_days <= route.window_start_days:
        raise RuntimeError(
            "FW_WINDOW_END_DAYS must be greater than FW_WINDOW_START_DAYS"
        )

    ceiling = _opt("FW_PRICE_CEILING")

    return AppConfig(
        route=route,
        scan=ScanConfig(
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
