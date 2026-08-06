"""The flights pack's request contract.

The union of what any flight provider can be asked. Adapters take this whole
object and pass through only what their provider accepts — which is the half
of the problem that bites, because the gaps are silent: a provider that ignores
`cabin` does not fail, it returns a price for a different cabin.

Each adapter declares what it can honor in :class:`FlightCapabilities`, and
:meth:`FlightQuery.unsupported_by` names the constraints that would be lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["Cabin", "FlightCapabilities", "FlightQuery", "Stops"]


class Cabin(str, Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class Stops(str, Enum):
    ANY = "any"
    NONSTOP = "nonstop"


@dataclass(frozen=True, slots=True)
class FlightCapabilities:
    """What a flight provider can actually honor.

    **This describes the provider, not your adapter.** Ground every ``False``
    in the provider's own documented parameter set — for an MCP server, its
    ``tools/list`` schema. The failure mode is specific and expensive: an
    adapter author reads their own function signature, sees no ``cabin``
    parameter, and records ``cabin=False``. The provider supported it all
    along; now the router faithfully reports a dropped constraint, everything
    looks correct, and every search quietly returns the provider's default.

    That is not hypothetical — it is exactly what happened in the codebase this
    pack was extracted from, and it went unnoticed because the *symptom* of
    getting it wrong is silence.
    """

    cabin: bool
    """Can filter by cabin class."""

    airlines: bool
    """Can include/exclude carriers server-side."""

    paginates: bool
    """Walks multiple result pages for a fuller set."""

    max_stops: bool = True

    evidence: str | None = None
    """How the above was determined — a schema URL, a tool name, a date.

    Required by the pack's conformance test for any capability declared
    ``False``, so "the provider cannot" is reviewable rather than assumed.
    """


@dataclass(frozen=True, slots=True)
class FlightQuery:
    """One provider-agnostic flight search."""

    origin: str
    destination: str
    departure_date: str
    """YYYY-MM-DD. Adapters reformat for their provider (Kiwi wants dd/mm/yyyy)."""

    return_date: str | None = None
    adults: int = 1
    cabin: Cabin | None = None
    stops: Stops = Stops.ANY
    include_airlines: tuple[str, ...] = ()
    exclude_airlines: tuple[str, ...] = ()
    limit: int = 75

    @property
    def round_trip(self) -> bool:
        return self.return_date is not None

    def unsupported_by(self, caps: FlightCapabilities) -> tuple[str, ...]:
        """Constraints in this query that ``caps`` cannot honor.

        Empty means the provider can answer exactly the question asked. A
        non-empty result does not make the provider useless — it means the
        answer will be to a *different* question, which the caller deserves to
        know before it lands in a price history.
        """
        missing: list[str] = []
        if self.cabin is not None and not caps.cabin:
            missing.append("cabin")
        if (self.include_airlines or self.exclude_airlines) and not caps.airlines:
            missing.append("airlines")
        if self.stops is not Stops.ANY and not caps.max_stops:
            missing.append("stops")
        return tuple(missing)
