from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TripDates:
    """One candidate trip: leave on `depart`, come back on `ret`."""

    depart: date
    ret: date

    @property
    def nights(self) -> int:
        return (self.ret - self.depart).days


@dataclass(frozen=True)
class DateRule:
    """Which (depart, return) pairs a watch prices.

    Two shapes:

    - ``rolling`` -- "any 4-night trip in the next six months". Departures run
      from ``start_days`` to ``end_days`` ahead of today and the return is
      allowed to fall outside that horizon, because the horizon only says how
      far ahead to look.
    - ``fixed`` -- "any 4-6 night trip between 12 and 20 February". Both ends
      must sit inside the window: a trip that returns after ``depart_to`` is
      not the trip that was asked for.
    """

    mode: str
    stay_nights: tuple[int, ...]
    start_days: int | None = None
    end_days: int | None = None
    depart_from: date | None = None
    depart_to: date | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("rolling", "fixed"):
            raise ValueError(f"unknown date mode: {self.mode!r}")
        if not self.stay_nights:
            raise ValueError("stay_nights must not be empty")
        if any(n < 1 for n in self.stay_nights):
            raise ValueError("stay_nights must all be >= 1")
        if self.mode == "rolling":
            if self.start_days is None or self.end_days is None:
                raise ValueError("rolling mode needs start_days and end_days")
            if self.end_days <= self.start_days:
                raise ValueError("end_days must be greater than start_days")
        else:
            if self.depart_from is None or self.depart_to is None:
                raise ValueError("fixed mode needs depart_from and depart_to")
            if self.depart_to < self.depart_from:
                raise ValueError("depart_to must not precede depart_from")

    @property
    def stay_label(self) -> str:
        lo, hi = min(self.stay_nights), max(self.stay_nights)
        return f"{lo} nights" if lo == hi else f"{lo}-{hi} nights"

    @property
    def key(self) -> str:
        """The part of a search signature the dates contribute.

        A fixed window *is* the product -- February half-term is not March --
        so it is part of the key and changing it starts fresh history. A
        rolling horizon only decides how many dates get sampled, so widening it
        keeps the history it already has.
        """
        stays = "-".join(str(n) for n in sorted(self.stay_nights))
        if self.mode == "fixed":
            return f"fixed{self.depart_from:%Y%m%d}-{self.depart_to:%Y%m%d}:{stays}n"
        return f"rolling:{stays}n"

    def trips(self, today: date | None = None) -> list[TripDates]:
        today = today or date.today()
        if self.mode == "rolling":
            assert self.start_days is not None and self.end_days is not None
            departures = [
                today + timedelta(days=offset)
                for offset in range(self.start_days, self.end_days + 1)
            ]
            limit = None
        else:
            assert self.depart_from is not None and self.depart_to is not None
            span = (self.depart_to - self.depart_from).days
            departures = [
                self.depart_from + timedelta(days=offset) for offset in range(span + 1)
            ]
            limit = self.depart_to

        out: list[TripDates] = []
        for depart in departures:
            for nights in sorted(self.stay_nights):
                ret = depart + timedelta(days=nights)
                if limit is not None and ret > limit:
                    continue
                out.append(TripDates(depart=depart, ret=ret))
        return out


@dataclass(frozen=True)
class Quote:
    """The cheapest itinerary found for one TripDates at one point in time.

    For a round-trip search Google prices the *outbound* options at the full
    round-trip total, so `price` is the whole trip, not one leg.
    """

    depart_date: str
    return_date: str
    price: int
    currency: str
    airlines: str
    stops: int | None
    duration_minutes: int | None

    @property
    def nights_label(self) -> str:
        from datetime import date as _date

        nights = (
            _date.fromisoformat(self.return_date)
            - _date.fromisoformat(self.depart_date)
        ).days
        return f"{nights} nights"

    booking_url: str = ""
    """Google Flights deep link for exactly this search.

    Built by the source from the same protobuf `tfs` parameter used to fetch
    the price, so the link opens the identical party, cabin and bag filters.
    A natural-language `?q=` URL does NOT work -- Google silently drops it and
    lands on a blank 1-adult economy search.
    """
