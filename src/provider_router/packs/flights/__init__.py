"""Flight search across interchangeable providers.

The pack this library was extracted from: three unrelated flight sources —
an MCP server, a second MCP server with completely different semantics, and a
Google Flights scraper — normalized to one contract and ordered by preference.

    from provider_router import Router
    from provider_router.packs.flights import FlightQuery, Cabin

    router = Router([skiplagged, kiwi, fast_flights])
    result = await router.invoke(FlightQuery("SFO", "JFK", "2026-09-15", cabin=Cabin.BUSINESS))
"""

from .conformance import assert_capability_evidence, check_capability_evidence
from .models import FlightOffer, FlightResults, Layover
from .query import Cabin, FlightCapabilities, FlightQuery, Stops

__all__ = [
    "Cabin",
    "FlightCapabilities",
    "FlightOffer",
    "FlightQuery",
    "FlightResults",
    "Layover",
    "Stops",
    "assert_capability_evidence",
    "check_capability_evidence",
]
