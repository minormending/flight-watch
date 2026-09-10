from __future__ import annotations

from ..models import Quote, TripDates


class SerpApiSource:
    """Placeholder for the paid SerpApi backend.

    Worth wiring up if the scraper starts breaking or getting blocked. Note
    that SerpApi's Google Flights *Deals* endpoint takes a date range plus a
    `trip_length`, so it can replace this whole per-date loop with one request
    -- when that lands, the scan loop should special-case it rather than
    calling fetch() per date.
    """

    name = "serpapi"

    def __init__(self, origin: str, destination: str) -> None:
        self.origin = origin
        self.destination = destination

    def fetch(self, trip: TripDates) -> Quote | None:
        raise NotImplementedError(
            "The SerpApi backend is not implemented yet. Set FW_SOURCE=google-flights."
        )
