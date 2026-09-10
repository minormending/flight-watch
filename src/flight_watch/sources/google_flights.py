from __future__ import annotations

import logging
import random
import time

from fast_flights import FlightQuery, Passengers, create_query, get_flights

from ..models import Quote, TripDates

logger = logging.getLogger(__name__)


class GoogleFlightsSource:
    """Free Google Flights scraper backed by fast-flights.

    fast-flights rebuilds the base64-protobuf `tfs` parameter that Google
    Flights uses internally, so one call is one real Google search. There is no
    API key and no quota, but the tradeoff is that Google can change the
    response shape at any time -- treat parse failures as expected.
    """

    name = "google-flights"

    def __init__(
        self,
        origin: str,
        destination: str,
        passengers: Passengers | None = None,
        seat_class: str = "economy",
        carry_on_bags: int = 0,
        checked_bags: int = 0,
        currency: str = "USD",
        max_retries: int = 3,
    ) -> None:
        self.origin = origin
        self.destination = destination
        self.passengers = passengers or Passengers(adults=1)
        self.seat_class = seat_class
        self.carry_on_bags = carry_on_bags
        self.checked_bags = checked_bags
        self.currency = currency
        self.max_retries = max_retries

    def _query(self, trip: TripDates):
        return create_query(
            flights=[
                FlightQuery(
                    date=trip.depart.isoformat(),
                    from_airport=self.origin,
                    to_airport=self.destination,
                ),
                FlightQuery(
                    date=trip.ret.isoformat(),
                    from_airport=self.destination,
                    to_airport=self.origin,
                ),
            ],
            trip="round-trip",
            seat=self.seat_class,
            passengers=self.passengers,
            # Google treats these as "include estimated bag fees for this many
            # bags per passenger", not as a filter -- so 1 means one carry-on
            # each, and larger values change nothing.
            carry_on_bags=self.carry_on_bags,
            checked_bags=self.checked_bags,
            currency=self.currency,
            language="en-US",
        )

    def fetch(self, trip: TripDates) -> Quote | None:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                results = get_flights(self._query(trip))
            except Exception as exc:  # noqa: BLE001 - scraper, anything can surface
                last_error = exc
                if attempt < self.max_retries:
                    backoff = 2**attempt + random.uniform(0, 1)
                    logger.debug(
                        "%s attempt %d/%d failed (%s); retrying in %.1fs",
                        trip.depart,
                        attempt,
                        self.max_retries,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
                continue

            quote = self._cheapest(results, trip)
            if quote is not None:
                return quote
            logger.debug("%s returned no priced itineraries", trip.depart)
            return None

        logger.warning(
            "%s failed after %d attempts: %s", trip.depart, self.max_retries, last_error
        )
        return None

    def _cheapest(self, results, trip: TripDates) -> Quote | None:
        best = None
        for flight in results:
            price = getattr(flight, "price", None)
            # Google returns 0 / non-int for "price unavailable" rows.
            if not isinstance(price, int) or price <= 0:
                continue
            if best is None or price < best.price:
                best = flight

        if best is None:
            return None

        legs = list(getattr(best, "flights", []) or [])
        durations = [
            leg.duration
            for leg in legs
            if isinstance(getattr(leg, "duration", None), int)
        ]

        return Quote(
            booking_url=self._query(trip).url(),
            depart_date=trip.depart.isoformat(),
            return_date=trip.ret.isoformat(),
            price=int(best.price),
            currency=self.currency,
            airlines=", ".join(best.airlines or []),
            stops=max(len(legs) - 1, 0) if legs else None,
            duration_minutes=sum(durations) if durations else None,
        )
