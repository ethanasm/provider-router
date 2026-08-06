"""The flights pack's output contract.

One shape, whatever answered. Every adapter in this pack normalizes into these
models, so a caller can swap Skiplagged for Kiwi for a Google Flights scrape
without touching a line of its own code.

Ported from vacation-price-tracker, where three unrelated providers were made
to conform to it in production. The field set is deliberately conservative:
anything only one provider can supply belongs in ``raw`` rather than as a
first-class field that is ``None`` two-thirds of the time.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["FlightOffer", "FlightResults", "Layover"]


class Layover(BaseModel):
    """A stop between segments."""

    airport: str = Field(description="Layover airport IATA code")
    city: str | None = None
    arrival_time: datetime | None = None
    departure_time: datetime | None = None
    duration_minutes: int | None = None


class FlightOffer(BaseModel):
    """One priced itinerary, normalized."""

    departure_airport: str
    arrival_airport: str
    departure_time: datetime | None = None
    arrival_time: datetime | None = None

    airline_name: str | None = None
    carrier_code: str | None = Field(default=None, description="Airline IATA code, e.g. 'AS'")

    flight_number: str | None = Field(
        default=None,
        description=(
            "Full carrier-prefixed designator, e.g. 'AS3361' — never a bare number. "
            "Callers render this as-is and must not concatenate carrier_code onto it."
        ),
    )
    """Contractual across every adapter.

    One provider hides this in an id string, another returns it structurally,
    a third has it buried in an undocumented payload index. Normalizing here is
    the point: rendering `carrier_code + flight_number` produces "AS AS3361",
    which is exactly the bug this contract exists to prevent.
    """

    duration_minutes: int | None = None
    stops: int = 0
    stops_text: str | None = None
    layovers: list[Layover] = Field(default_factory=list)

    price_amount: Decimal
    price_currency: str = "USD"
    price_display: str | None = None

    booking_link: str | None = None

    provider: str = Field(description="Which adapter produced this offer")
    raw: dict[str, Any] | None = Field(
        default=None,
        description="The provider's original payload, for debugging and provider-specific needs",
    )


class FlightResults(BaseModel):
    """A whole answer to one query."""

    offers: list[FlightOffer] = Field(default_factory=list)

    origin: str
    destination: str
    departure_date: str
    return_date: str | None = None
    round_trip: bool = False

    provider: str
    currency: str = "USD"

    partial: bool = False
    """Whether this answer is known to be incomplete.

    Set when an adapter succeeded overall but lost part of its result — a
    multi-query union where one query failed, a paginated sweep that stopped
    early. The router reads this to decide ``DEGRADED``, which is the whole
    reason it exists: a result set that silently omits entire airlines is a
    success by every transport measure and useless to the caller.
    """

    partial_reason: str | None = None

    @property
    def count(self) -> int:
        return len(self.offers)
