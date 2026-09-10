from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TripDates:
    """One candidate trip: leave on `depart`, come back `stay_nights` later."""

    depart: date
    ret: date

    @property
    def nights(self) -> int:
        return (self.ret - self.depart).days


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

    booking_url: str = ""
    """Google Flights deep link for exactly this search.

    Built by the source from the same protobuf `tfs` parameter used to fetch
    the price, so the link opens the identical party, cabin and bag filters.
    A natural-language `?q=` URL does NOT work -- Google silently drops it and
    lands on a blank 1-adult economy search.
    """


def trip_window(
    stay_nights: int, start_days: int, end_days: int, today: date | None = None
) -> list[TripDates]:
    """Every departure date in the window, each paired with its return date."""
    today = today or date.today()
    return [
        TripDates(depart=d, ret=d + timedelta(days=stay_nights))
        for offset in range(start_days, end_days + 1)
        for d in [today + timedelta(days=offset)]
    ]
