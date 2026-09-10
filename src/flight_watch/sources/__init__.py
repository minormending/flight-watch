from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..models import Quote, TripDates

if TYPE_CHECKING:
    from ..config import SearchConfig


class PriceSource(Protocol):
    """A backend that can price one round trip.

    Deliberately narrow so the free scraper and a paid API can be swapped
    without touching the scan loop or the alert engine.
    """

    name: str

    def fetch(self, trip: TripDates) -> Quote | None:
        """Cheapest quote for `trip`, or None if nothing was returned."""
        ...


def build_source(name: str, search: "SearchConfig") -> PriceSource:
    if name == "google-flights":
        from fast_flights import Passengers

        from .google_flights import GoogleFlightsSource

        return GoogleFlightsSource(
            origin=search.origin,
            destination=search.destination,
            passengers=Passengers(
                adults=search.adults,
                children=search.children,
                infants_in_seat=search.infants_in_seat,
                infants_on_lap=search.infants_on_lap,
            ),
            seat_class=search.seat_class,
            carry_on_bags=search.carry_on_bags,
            checked_bags=search.checked_bags,
        )
    if name == "serpapi":
        from .serpapi import SerpApiSource

        return SerpApiSource(origin=search.origin, destination=search.destination)
    raise RuntimeError(f"Unknown price source: {name!r}")
