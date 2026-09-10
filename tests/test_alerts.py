from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from flight_watch.alerts import AlertDecision, evaluate, percentile
from flight_watch.config import AlertConfig, RouteConfig
from flight_watch.models import Quote
from flight_watch.storage import connect, record_quotes, start_scan

ROUTE = RouteConfig(
    origin="NYC",
    destination="SJU",
    stay_nights=4,
    window_start_days=21,
    window_end_days=180,
)


def make_alert_cfg(**overrides) -> AlertConfig:
    base = dict(
        percentile=20,
        baseline_days=30,
        price_ceiling=None,
        cooldown_hours=20,
        rearm_drop_pct=10,
        warmup_observations=10,
        warmup_days=0,
    )
    base.update(overrides)
    return AlertConfig(**base)


def quote(price: int, depart: str = "2026-11-01") -> Quote:
    return Quote(
        depart_date=depart,
        return_date="2026-11-05",
        price=price,
        currency="USD",
        airlines="JetBlue",
        stops=0,
        duration_minutes=235,
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def seed(conn, prices, days_ago=1):
    scan_id = start_scan(conn, "NYC", "SJU", 4, len(prices))
    record_quotes(conn, scan_id, "NYC", "SJU", "test", [quote(p) for p in prices])
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )
    conn.execute("UPDATE observations SET checked_at = ? WHERE scan_id = ?", (stamp, scan_id))


def test_percentile_interpolates():
    assert percentile([100], 50) == 100
    assert percentile([100, 200], 0) == 100
    assert percentile([100, 200], 100) == 200
    assert percentile([100, 200], 50) == 150
    assert percentile(list(range(1, 101)), 20) == pytest.approx(20.8)


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        percentile([], 50)


def test_no_alert_during_warmup(conn):
    seed(conn, [300, 310, 320])
    d = evaluate(conn, ROUTE, make_alert_cfg(warmup_observations=100), quote(250))
    assert not d.fire and "warming up" in d.reason


def test_fires_below_percentile(conn):
    seed(conn, list(range(300, 400)))
    d = evaluate(conn, ROUTE, make_alert_cfg(), quote(305))
    assert d.fire, d.reason


def test_silent_when_price_is_typical(conn):
    seed(conn, list(range(300, 400)))
    d = evaluate(conn, ROUTE, make_alert_cfg(), quote(380))
    assert not d.fire and "above p20" in d.reason


def test_ceiling_blocks_statistical_low(conn):
    seed(conn, list(range(900, 1000)))
    d = evaluate(conn, ROUTE, make_alert_cfg(price_ceiling=500), quote(905))
    assert not d.fire and "above ceiling" in d.reason


def test_cooldown_suppresses_repeat(conn):
    seed(conn, list(range(300, 400)))
    conn.execute(
        "INSERT INTO alerts (fired_at, depart_date, return_date, price, threshold,"
        " pool_size, channels) VALUES (?, '2026-11-01', '2026-11-05', 310, 320, 100, 'ntfy')",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    assert not evaluate(conn, ROUTE, make_alert_cfg(), quote(308)).fire


def test_cooldown_rearms_on_further_drop(conn):
    seed(conn, list(range(300, 400)))
    conn.execute(
        "INSERT INTO alerts (fired_at, depart_date, return_date, price, threshold,"
        " pool_size, channels) VALUES (?, '2026-11-01', '2026-11-05', 310, 320, 100, 'ntfy')",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    # 10% below 310 is 279, so 270 should punch through the cooldown.
    assert evaluate(conn, ROUTE, make_alert_cfg(), quote(270)).fire


def test_no_quotes_is_not_an_alert(conn):
    seed(conn, list(range(300, 400)))
    assert not evaluate(conn, ROUTE, make_alert_cfg(), None).fire
