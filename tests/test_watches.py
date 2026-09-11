from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from flight_watch.config import SearchConfig, WatchConfig
from flight_watch.models import DateRule
from flight_watch.storage import baseline_prices, connect

FEB = dict(
    mode="fixed",
    stay_nights=(4, 5, 6),
    depart_from=date(2027, 2, 12),
    depart_to=date(2027, 2, 20),
)


def make_watch(**overrides) -> WatchConfig:
    base = dict(
        name="foreign-feb",
        origin="NYC",
        destinations=("LIS", "LHR"),
        dates=DateRule(**FEB),
        max_stops=0,
        adults=2,
        children=1,
        infants_in_seat=0,
        infants_on_lap=0,
        seat_class="economy",
        carry_on_bags=1,
        checked_bags=0,
        exclude_basic_economy=True,
        top_n=20,
        proxy_adults=3,
    )
    base.update(overrides)
    return WatchConfig(**base)


def test_fixed_window_keeps_the_return_inside_it():
    """'4-6 nights between the 12th and 20th' must not return on the 21st."""
    trips = DateRule(**FEB).trips()
    assert len(trips) == 12
    assert all(date(2027, 2, 12) <= t.depart for t in trips)
    assert all(t.ret <= date(2027, 2, 20) for t in trips)
    assert {t.nights for t in trips} == {4, 5, 6}
    # Departing the 17th cannot fit even the shortest stay before the 20th.
    assert max(t.depart for t in trips) == date(2027, 2, 16)


def test_rolling_window_lets_the_return_run_past_the_horizon():
    trips = DateRule(
        mode="rolling", stay_nights=(4,), start_days=21, end_days=180
    ).trips(today=date(2026, 9, 11))
    assert len(trips) == 160
    assert trips[-1].ret > date(2026, 9, 11) + timedelta(days=180)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(mode="sideways", stay_nights=(4,)),
        dict(mode="rolling", stay_nights=()),
        dict(mode="rolling", stay_nights=(0,), start_days=1, end_days=2),
        dict(mode="rolling", stay_nights=(4,), start_days=30, end_days=10),
        dict(mode="fixed", stay_nights=(4,)),
    ],
)
def test_bad_date_rules_are_rejected(kwargs):
    with pytest.raises(ValueError):
        DateRule(**kwargs)


def test_fixed_dates_are_part_of_the_signature():
    """February half-term is not March; their prices must not share a pool."""
    a = make_watch().searches()[0]
    march = make_watch(
        dates=DateRule(
            mode="fixed",
            stay_nights=(4, 5, 6),
            depart_from=date(2027, 3, 12),
            depart_to=date(2027, 3, 20),
        )
    ).searches()[0]
    assert a.signature != march.signature


def test_stop_limit_is_part_of_the_signature():
    assert make_watch().searches()[0].signature != (
        make_watch(max_stops=None).searches()[0].signature
    )


def test_proxy_scans_as_adults_but_confirms_the_real_party():
    watch = make_watch()
    scanned = watch.searches()[0]
    real = watch.confirm_search("LIS")
    assert watch.is_proxy
    assert (scanned.adults, scanned.children) == (3, 0)
    assert (real.adults, real.children) == (2, 1)
    # The two are different products and must not share a baseline.
    assert scanned.signature != real.signature


def test_no_proxy_means_scan_and_confirm_match():
    watch = make_watch(proxy_adults=None)
    assert not watch.is_proxy
    assert watch.searches()[0].signature == watch.confirm_search("LIS").signature


def test_each_destination_gets_its_own_signature():
    sigs = {s.destination: s.signature for s in make_watch().searches()}
    assert len(set(sigs.values())) == 2, "$400 to Lisbon is not $400 to London"


def test_old_signatures_are_upgraded_not_orphaned(tmp_path):
    """The date-mode and stop-limit segments arrived after data existed.

    Everything recorded before them was a rolling any-stops search, so the old
    keys name the same product; dropping them would restart the warmup gate and
    throw away real history.
    """
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.executescript("""
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER, checked_at TEXT,
            origin TEXT, destination TEXT, depart_date TEXT, return_date TEXT,
            signature TEXT, price INTEGER, currency TEXT, airlines TEXT,
            stops INTEGER, duration_minutes INTEGER, source TEXT);
        """)
    raw.executemany(
        "INSERT INTO observations (checked_at, origin, destination, depart_date,"
        " return_date, signature, price, currency, source)"
        " VALUES (?, 'NYC','SJU','2026-11-01','2026-11-05', ?, ?, 'USD','g')",
        [
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "NYC-SJU:4n:2a1c0is0il:economy-nobasic:carry1:checked0",
                price,
            )
            for price in (952, 1001, 1191)
        ],
    )
    raw.commit()
    raw.close()

    conn = connect(db)
    try:
        upgraded = (
            "NYC-SJU:rolling:4n:stopsany:2a1c0is0il:economy-nobasic:carry1:checked0"
        )
        assert sorted(baseline_prices(conn, upgraded, 30)) == [952, 1001, 1191]
        current = SearchConfig(
            watch="sju",
            origin="NYC",
            destination="SJU",
            dates=DateRule(
                mode="rolling", stay_nights=(4,), start_days=21, end_days=180
            ),
            max_stops=None,
            adults=2,
            children=1,
            infants_in_seat=0,
            infants_on_lap=0,
            seat_class="economy",
            carry_on_bags=1,
            checked_bags=0,
            exclude_basic_economy=True,
        )
        assert current.signature == upgraded
    finally:
        conn.close()


def test_legacy_rows_are_left_alone(tmp_path):
    db = tmp_path / "l.db"
    conn = connect(db)
    conn.execute(
        "INSERT INTO observations (scan_id, checked_at, origin, destination,"
        " depart_date, return_date, signature, price, currency, source)"
        " VALUES (1, ?, 'NYC','SJU','2026-11-01','2026-11-05','legacy',285,'USD','g')",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    conn.commit()
    conn.close()

    conn = connect(db)
    try:
        assert baseline_prices(conn, "legacy", 30) == [285]
    finally:
        conn.close()
