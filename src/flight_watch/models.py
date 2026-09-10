from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote


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

    def booking_url(
        self,
        origin: str,
        destination: str,
        party_label: str | None = None,
        cabin: str | None = None,
    ) -> str:
        """A Google Flights URL a human can open to actually book this.

        Google parses this natural-language form, so the party and cabin have
        to be spelled out or the link lands on a 1-adult economy search that
        does not match the price in the alert.
        """
        q = (
            f"Flights from {origin} to {destination} "
            f"on {self.depart_date} through {self.return_date}"
        )
        if party_label:
            q += f" for {party_label}"
        if cabin:
            q += f" in {cabin}"
        return f"https://www.google.com/travel/flights?q={quote(q)}"


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
