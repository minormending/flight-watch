from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from flight_watch.alerts import AlertDecision, evaluate, percentile
from flight_watch.config import AlertConfig, SearchConfig
from flight_watch.models import Quote
from flight_watch.storage import baseline_prices, connect, record_quotes, start_scan


def make_search(**overrides) -> SearchConfig:
    base = dict(
        origin="NYC",
        destination="SJU",
        stay_nights=4,
        window_start_days=21,
        window_end_days=180,
        adults=2,
        children=1,
        infants_in_seat=0,
        infants_on_lap=0,
        seat_class="premium-economy",
        carry_on_bags=1,
        checked_bags=0,
    )
    base.update(overrides)
    return SearchConfig(**base)


SEARCH = make_search()


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


def seed(conn, prices, days_ago=1, signature=None):
    scan_id = start_scan(conn, "NYC", "SJU", 4, len(prices))
    record_quotes(
        conn,
        scan_id,
        "NYC",
        "SJU",
        signature or SEARCH.signature,
        "test",
        [quote(p) for p in prices],
    )
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        "UPDATE observations SET checked_at = ? WHERE scan_id = ?", (stamp, scan_id)
    )


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
    d = evaluate(conn, SEARCH, make_alert_cfg(warmup_observations=100), quote(250))
    assert not d.fire and "warming up" in d.reason


def test_fires_below_percentile(conn):
    seed(conn, list(range(300, 400)))
    d = evaluate(conn, SEARCH, make_alert_cfg(), quote(305))
    assert d.fire, d.reason


def test_silent_when_price_is_typical(conn):
    seed(conn, list(range(300, 400)))
    d = evaluate(conn, SEARCH, make_alert_cfg(), quote(380))
    assert not d.fire and "above p20" in d.reason


def test_ceiling_blocks_statistical_low(conn):
    seed(conn, list(range(900, 1000)))
    d = evaluate(conn, SEARCH, make_alert_cfg(price_ceiling=500), quote(905))
    assert not d.fire and "above ceiling" in d.reason


def test_cooldown_suppresses_repeat(conn):
    seed(conn, list(range(300, 400)))
    conn.execute(
        "INSERT INTO alerts (fired_at, signature, depart_date, return_date, price,"
        " threshold, pool_size, channels)"
        " VALUES (?, ?, '2026-11-01', '2026-11-05', 310, 320, 100, 'ntfy')",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), SEARCH.signature),
    )
    assert not evaluate(conn, SEARCH, make_alert_cfg(), quote(308)).fire


def test_cooldown_rearms_on_further_drop(conn):
    seed(conn, list(range(300, 400)))
    conn.execute(
        "INSERT INTO alerts (fired_at, signature, depart_date, return_date, price,"
        " threshold, pool_size, channels)"
        " VALUES (?, ?, '2026-11-01', '2026-11-05', 310, 320, 100, 'ntfy')",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), SEARCH.signature),
    )
    # 10% below 310 is 279, so 270 should punch through the cooldown.
    assert evaluate(conn, SEARCH, make_alert_cfg(), quote(270)).fire


def test_no_quotes_is_not_an_alert(conn):
    seed(conn, list(range(300, 400)))
    assert not evaluate(conn, SEARCH, make_alert_cfg(), None).fire


def test_signature_changes_with_party_and_cabin():
    base = make_search()
    assert base.signature != make_search(adults=1, children=0).signature
    assert base.signature != make_search(seat_class="economy").signature
    assert base.signature != make_search(carry_on_bags=0).signature
    assert base.signature != make_search(stay_nights=7).signature
    # The scan window only picks which dates get sampled; prices stay comparable.
    assert base.signature == make_search(window_end_days=90).signature


def test_party_label_reads_naturally():
    assert make_search().party_label == "2 adults, 1 child"
    assert make_search(adults=1, children=0).party_label == "1 adult"
    assert make_search(adults=2, children=3).party_label == "2 adults, 3 children"


def test_other_signatures_stay_out_of_the_baseline(conn):
    """A 1-adult-economy history must not price a 3-person premium trip."""
    seed(
        conn,
        list(range(280, 380)),
        signature=make_search(adults=1, children=0, seat_class="economy").signature,
    )
    d = evaluate(conn, SEARCH, make_alert_cfg(), quote(1200))
    assert not d.fire and "warming up" in d.reason
    assert d.pool_size == 0


def test_baseline_uses_only_the_matching_signature(conn):
    seed(
        conn,
        list(range(280, 380)),
        signature=make_search(adults=1, children=0, seat_class="economy").signature,
    )
    seed(conn, list(range(1200, 1300)))
    d = evaluate(conn, SEARCH, make_alert_cfg(), quote(1205))
    assert d.fire, d.reason
    assert d.pool_size == 100
    assert d.threshold is not None and d.threshold > 1000


def test_cooldown_is_scoped_to_the_signature(conn):
    """Changing the search must not inherit the old search's cooldown."""
    seed(conn, list(range(1200, 1300)))
    conn.execute(
        "INSERT INTO alerts (fired_at, signature, depart_date, return_date, price,"
        " threshold, pool_size, channels)"
        " VALUES (?, 'some-other-search', '2026-11-01', '2026-11-05', 1210, 1220, 100, 'ntfy')",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    assert evaluate(conn, SEARCH, make_alert_cfg(), quote(1205)).fire


def test_legacy_rows_are_migrated_and_excluded(tmp_path):
    """A pre-signature database keeps its rows but they never join a baseline."""
    import sqlite3

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER, checked_at TEXT,
            origin TEXT, destination TEXT, depart_date TEXT, return_date TEXT,
            price INTEGER, currency TEXT, airlines TEXT, stops INTEGER,
            duration_minutes INTEGER, source TEXT);
        INSERT INTO observations (checked_at, origin, destination, depart_date,
            return_date, price, currency, source)
        VALUES ('2026-09-10T00:00:00+00:00','NYC','SJU','2026-11-01','2026-11-05',
            291,'USD','google-flights');
        """)
    old.commit()
    old.close()

    c = connect(db)
    try:
        assert c.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert (
            c.execute("SELECT signature FROM observations").fetchone()["signature"]
            == "legacy"
        )
        assert baseline_prices(c, SEARCH.signature, 30) == []
    finally:
        c.close()
