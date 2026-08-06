"""Google Flights adapter, via the `fast-flights` scraper.

The odd one out in this pack, and the reason the pack is interesting. The other
two adapters talk to servers that promise a schema. This one reads a ranked HTML
page that promises nothing, and it still has to produce the same
:class:`FlightOffer` — flight numbers included.

Three provider facts are normalized here rather than leaked to callers:

**One page, no pagination.** There is no offset to advance. What the page does
have is *two* itinerary sections, and the upstream library reads only the first;
the cheapest fare regularly sits in the second, so both are parsed.

**Round-trip prices describe a trip the itinerary does not show.** Google's
ranked page lists outbound options priced at the round-trip total, and the query
protobuf carries no selected-flight token, so the paired return legs cannot be
fetched. Offers are marked :attr:`FlightOffer.round_trip_total` rather than
passed off as one-ways at a strange price.

**Drift is a first-class outcome.** Every field comes from an undocumented
integer index into an obfuscated array. When those indexes move, the honest
report is not a crash and not a clean result — it is a *degraded* one, so the
router keeps looking while the caller still gets whatever survived. See
:class:`~provider_router.packs.flights.google_payload.PayloadHealth`.

Install as ``provider-router[fast-flights]``. The dependency is imported lazily
and only for *fetching*: parsing needs nothing, so tests and any caller with
its own HTTP path work without it.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any

from ...clock import Deadline
from ...outcomes import Failure, Outcome, terminal, transient
from ...provider import Attempt
from .google_payload import (
    GooglePayloadError,
    NoFlightsFound,
    ParsedItinerary,
    ParsedPage,
    ParsedSegment,
    parse_flights_page,
)
from .models import FlightOffer, FlightResults, Layover
from .query import Cabin, FlightCapabilities, FlightQuery, Stops

__all__ = [
    "CABIN_TO_SEAT",
    "FAST_FLIGHTS_CAPABILITIES",
    "FastFlights",
    "FastFlightsBlocked",
    "FastFlightsError",
    "FastFlightsUnavailable",
    "Fetcher",
    "GoogleFlightsFetcher",
    "build_google_query",
]

CABIN_TO_SEAT = {
    Cabin.ECONOMY: "economy",
    Cabin.PREMIUM_ECONOMY: "premium-economy",
    Cabin.BUSINESS: "business",
    Cabin.FIRST: "first",
}

MAX_TRANSIENT_RETRIES = 2
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 4.0

FAST_FLIGHTS_CAPABILITIES = FlightCapabilities(
    cabin=True,
    airlines=True,
    paginates=False,
    max_stops=True,
    evidence=(
        "fast_flights.querying, verified 2026-08-06: create_query(seat=) takes "
        "economy|premium-economy|business|first, and FlightQuery(max_stops=, "
        "airlines=) carries both into the tfs protobuf. `airlines` is an "
        "include-list only — there is no exclude field, so exclusion queries "
        "are declined by supports() rather than half-applied. paginates=False "
        "is the real gap: one ranked page per query, no offset to advance."
    ),
)

Fetcher = Callable[[FlightQuery], "str | Awaitable[str]"]
"""``(request) -> html``. The single seam between this adapter and the network.

Everything `fast-flights`-specific lives behind it, so the extra is needed only
to *fetch*: pass your own callable — a recorded fixture, a shared session, a
different scraper — and the parsing and normalization here work untouched.
Sync callables are run in a worker thread; awaitables are awaited.
"""


class FastFlightsError(Exception):
    """Base error for this adapter."""


class FastFlightsUnavailable(FastFlightsError):
    """The `fast-flights` extra is not installed.

    Terminal, not transient: no amount of retrying installs a package. The
    router moves to the next provider, which is the correct response to "this
    one is not usable in this deployment".
    """


class FastFlightsBlocked(FastFlightsError):
    """Google served something other than results — consent wall, challenge, drift.

    Transient by default. Blocks are intermittent and clear on retry; treating
    them as terminal would take a working provider out of rotation over a
    single unlucky page.
    """


class FastFlights:
    """Flight search by scraping Google Flights."""

    name = "fast_flights"
    capabilities = FAST_FLIGHTS_CAPABILITIES

    def __init__(
        self,
        *,
        fetch: Fetcher | None = None,
        proxy: str | None = None,
        currency: str = "USD",
        language: str = "en-US",
        max_retries: int = MAX_TRANSIENT_RETRIES,
    ) -> None:
        self._fetch = fetch
        self._proxy = proxy
        self._currency = currency
        self._language = language
        self._max_retries = max(0, max_retries)

    # ---------------------------------------------------------------- contract

    def supports(self, request: FlightQuery) -> bool:
        """Everything this pack models except airline *exclusion*.

        The query protobuf has an include-list and no exclude field. Dropping
        an exclusion silently would return exactly the carriers the caller
        asked to avoid, priced as though they were acceptable — so the query is
        declined and the router asks a provider that can answer it.
        """
        if request.exclude_airlines:
            return False
        return not request.unsupported_by(self.capabilities)

    def classify(self, exc: BaseException) -> Failure:
        if isinstance(exc, FastFlightsUnavailable):
            return terminal(str(exc), cause=exc)
        if isinstance(exc, FastFlightsBlocked | TimeoutError):
            return transient(str(exc), cause=exc)
        if isinstance(exc, FastFlightsError):
            return terminal(str(exc), cause=exc)
        return terminal(str(exc), cause=exc)

    def assess(self, result: FlightResults, attempt: Attempt) -> Outcome:
        """Payload drift produces offers that are real but incomplete."""
        return Outcome.DEGRADED if result.partial else Outcome.OK

    async def invoke(self, request: FlightQuery, deadline: Deadline) -> FlightResults:
        page = await self._fetch_page(request, deadline)
        offers = [
            offer
            for itinerary in page.itineraries
            if (offer := self._normalize(itinerary, request, page)) is not None
        ]
        offers.sort(key=lambda o: o.price_amount)
        return FlightResults(
            offers=offers[: request.limit],
            origin=request.origin.upper(),
            destination=request.destination.upper(),
            departure_date=request.departure_date,
            return_date=request.return_date,
            round_trip=request.round_trip,
            provider=self.name,
            currency=self._currency,
            partial=page.health.degraded,
            partial_reason=page.health.reason,
        )

    # --------------------------------------------------------------- internals

    async def _fetch_page(self, request: FlightQuery, deadline: Deadline) -> ParsedPage:
        """One query, retried briefly on a blocked or unreadable page.

        The adapter owns transport retry; the router owns failover. Retrying
        here is right because Google's blocks are per-request and clear in
        seconds, and swapping providers over one unlucky page would be a much
        larger move than the problem warrants.
        """
        fetch = self._fetch or GoogleFlightsFetcher(
            proxy=self._proxy, currency=self._currency, language=self._language
        )

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                delay = min(BASE_BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
                remaining = deadline.remaining()
                if remaining is not None and remaining <= delay:
                    # Sleeping past the deadline spends the caller's whole
                    # budget on a provider that has already failed twice.
                    break
                await asyncio.sleep(delay)
            try:
                html = await _call_fetcher(fetch, request)
                return parse_flights_page(html)
            except NoFlightsFound:
                # Google answered; the answer was "none". Not a failure, and
                # not something a retry or another provider would improve.
                return ParsedPage()
            except FastFlightsUnavailable:
                # A missing dependency is not a bad page. Retrying it two more
                # times just delays the router's move to a usable provider.
                raise
            except GooglePayloadError as exc:
                last_error = exc
            except Exception as exc:  # scraper failures come in many shapes
                last_error = exc

        raise FastFlightsBlocked(
            f"Google Flights page unreadable after {self._max_retries + 1} attempt(s): {last_error}"
        ) from last_error

    def _normalize(
        self, itinerary: ParsedItinerary, request: FlightQuery, page: ParsedPage
    ) -> FlightOffer | None:
        if itinerary.price is None or itinerary.price <= 0 or not itinerary.segments:
            return None

        first, last = itinerary.segments[0], itinerary.segments[-1]
        stops = len(itinerary.segments) - 1
        carrier = first.carrier

        return FlightOffer(
            departure_airport=(first.from_code or request.origin).upper(),
            arrival_airport=(last.to_code or request.destination).upper(),
            departure_time=first.departure,
            arrival_time=last.arrival,
            airline_name=(
                page.airline_code_to_name.get((carrier or "").upper())
                or first.airline_name
                or (itinerary.airline_names[0] if itinerary.airline_names else None)
            ),
            carrier_code=carrier,
            flight_number=first.designator,
            duration_minutes=_duration_minutes(itinerary.segments),
            stops=stops,
            stops_text="Direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}",
            layovers=_layovers(itinerary.segments),
            price_amount=Decimal(itinerary.price),
            price_currency=self._currency,
            price_display=f"${itinerary.price}" if self._currency == "USD" else None,
            # The page ranks round-trip *totals* against outbound itineraries.
            round_trip_total=request.round_trip,
            provider=self.name,
            raw={
                "is_best": itinerary.is_best,
                "airline_names": list(itinerary.airline_names),
                "segments": [_segment_dict(s) for s in itinerary.segments],
            },
        )


async def _call_fetcher(fetch: Fetcher, request: FlightQuery) -> str:
    """Accept a sync or async fetcher without making callers declare which.

    The default one is sync (the library's is), so it runs in a thread rather
    than blocking the event loop for the several seconds a scrape takes.
    """
    if inspect.iscoroutinefunction(fetch):
        return await fetch(request)  # type: ignore[no-any-return]
    result = await asyncio.to_thread(fetch, request)
    if inspect.isawaitable(result):
        return await result
    return result


def build_google_query(
    request: FlightQuery, *, currency: str = "USD", language: str = "en-US"
) -> Any:
    """Translate a :class:`FlightQuery` into a `fast-flights` query object.

    Separate from the fetcher so the translation — seat class, stop ceiling,
    airline include-list, the second leg on a round trip — is testable without
    a network call, and reusable by anyone who wants to drive the scraper
    themselves.
    """
    try:
        from fast_flights import FlightQuery as GoogleLeg
        from fast_flights import Passengers, create_query
    except ImportError as exc:
        raise FastFlightsUnavailable(
            "the `fast-flights` extra is required to build Google Flights queries: "
            "pip install 'provider-router[fast-flights]'"
        ) from exc

    stops = 0 if request.stops is Stops.NONSTOP else None
    # Include-list only; supports() has already declined exclusion queries.
    airlines = list(request.include_airlines) or None
    legs = [
        GoogleLeg(
            date=request.departure_date,
            from_airport=request.origin.upper(),
            to_airport=request.destination.upper(),
            max_stops=stops,
            airlines=airlines,
        )
    ]
    if request.return_date:
        legs.append(
            GoogleLeg(
                date=request.return_date,
                from_airport=request.destination.upper(),
                to_airport=request.origin.upper(),
                max_stops=stops,
                airlines=airlines,
            )
        )
    return create_query(
        flights=legs,
        trip="round-trip" if request.return_date else "one-way",
        seat=CABIN_TO_SEAT.get(request.cabin or Cabin.ECONOMY, "economy"),
        passengers=Passengers(adults=max(1, request.adults)),
        language=language,
        currency=currency,
    )


class GoogleFlightsFetcher:
    """The default fetcher: build the query, scrape the page, return its HTML.

    A class rather than a closure so the `fast-flights` import happens on the
    first call and not at module import — the whole point of the extra being
    optional is that importing this module must work without it.
    """

    def __init__(
        self, *, proxy: str | None = None, currency: str = "USD", language: str = "en-US"
    ) -> None:
        self._proxy = proxy
        self._currency = currency
        self._language = language

    def __call__(self, request: FlightQuery) -> str:
        try:
            from fast_flights.fetcher import fetch_flights_html
        except ImportError as exc:
            raise FastFlightsUnavailable(
                "no fetcher available: install the extra "
                "(pip install 'provider-router[fast-flights]') or pass fetch=..."
            ) from exc
        query = build_google_query(request, currency=self._currency, language=self._language)
        html: str = fetch_flights_html(query, proxy=self._proxy)
        return html


def _duration_minutes(segments: tuple[ParsedSegment, ...]) -> int | None:
    """Wall-clock door to door, falling back to flying time.

    Summing segment durations drops every layover, which on a two-stop
    itinerary can understate the trip by hours — so prefer the actual span and
    use the sum only when a timestamp is missing.
    """
    start, end = segments[0].departure, segments[-1].arrival
    if isinstance(start, datetime) and isinstance(end, datetime) and end >= start:
        return int((end - start).total_seconds()) // 60
    flying = [s.duration_minutes for s in segments if s.duration_minutes is not None]
    return sum(flying) if flying else None


def _layovers(segments: tuple[ParsedSegment, ...]) -> list[Layover]:
    layovers: list[Layover] = []
    for arriving, departing in pairwise(segments):
        airport = arriving.to_code or departing.from_code
        if not airport:
            continue
        duration = None
        if isinstance(arriving.arrival, datetime) and isinstance(departing.departure, datetime):
            gap = departing.departure - arriving.arrival
            if gap.total_seconds() >= 0:
                duration = int(gap.total_seconds()) // 60
        layovers.append(
            Layover(
                airport=airport.upper(),
                city=arriving.to_name,
                arrival_time=arriving.arrival,
                departure_time=departing.departure,
                duration_minutes=duration,
            )
        )
    return layovers


def _segment_dict(segment: ParsedSegment) -> dict[str, Any]:
    return {
        "from": segment.from_code,
        "to": segment.to_code,
        "departure": segment.departure.isoformat() if segment.departure else None,
        "arrival": segment.arrival.isoformat() if segment.arrival else None,
        "duration_minutes": segment.duration_minutes,
        "carrier": segment.carrier,
        "flight_number": segment.designator,
        "airline_name": segment.airline_name,
        "plane_type": segment.plane_type,
    }
