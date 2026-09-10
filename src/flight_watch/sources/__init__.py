from __future__ import annotations

from typing import Protocol

from ..models import Quote, TripDates


class PriceSource(Protocol):
    """A backend that can price one round trip.

    Deliberately narrow so the free scraper and a paid API can be swapped
    without touching the scan loop or the alert engine.
    """

    name: str

    def fetch(self, trip: TripDates) -> Quote | None:
        """Cheapest quote for `trip`, or None if nothing was returned."""
        ...


def build_source(name: str, origin: str, destination: str) -> PriceSource:
    if name == "google-flights":
        from .google_flights import GoogleFlightsSource

        return GoogleFlightsSource(origin=origin, destination=destination)
    if name == "serpapi":
        from .serpapi import SerpApiSource

        return SerpApiSource(origin=origin, destination=destination)
    raise RuntimeError(f"Unknown price source: {name!r}")
