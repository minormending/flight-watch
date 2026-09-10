from __future__ import annotations

import logging
import random
import time

import json

from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
from fast_flights.parser import parse_js
from selectolax.lexbor import LexborHTMLParser

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
        exclude_basic_economy: bool = False,
        currency: str = "USD",
        max_retries: int = 3,
    ) -> None:
        self.origin = origin
        self.destination = destination
        self.passengers = passengers or Passengers(adults=1)
        self.seat_class = seat_class
        self.carry_on_bags = carry_on_bags
        self.checked_bags = checked_bags
        self.exclude_basic_economy = exclude_basic_economy
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
            # Basic Economy has no carry-on, no seat selection and no changes,
            # so for a family those fares are not really bookable.
            exclude_basic_economy=self.exclude_basic_economy,
            currency=self.currency,
            language="en-US",
        )

    def _fetch_all(self, query):
        """Fetch a query and return BOTH result lists Google sends back.

        Google's payload carries two: payload[2][0] is "Top departing
        flights" -- where the cheapest fares actually live -- and
        payload[3][0] is "Other departing flights". fast_flights.get_flights()
        parses only the second, so a price watcher built on it systematically
        misses the cheapest option. Measured on NYC->SJU for 3 passengers:
        it reported $1,211 while $1,001 was on the page.

        Rather than reimplement Google's index-based item format, we splice
        both lists into the slot parse_js already knows how to read, so the
        library keeps doing the fragile part.
        """
        html = fetch_flights_html(query)
        script = LexborHTMLParser(html).css_first(r"script.ds\:1")
        if script is None:
            raise RuntimeError("no result payload in response")

        raw = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
        payload = json.loads(raw)

        def items(slot):
            entry = payload[slot] if len(payload) > slot else None
            return (entry[0] if entry else None) or []

        merged = list(payload)
        merged[3] = [items(2) + items(3)]
        return parse_js("data:" + json.dumps(merged) + ",")

    def fetch(self, trip: TripDates) -> Quote | None:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                results = self._fetch_all(self._query(trip))
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
