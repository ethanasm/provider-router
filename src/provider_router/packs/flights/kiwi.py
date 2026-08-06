"""Kiwi.com adapter — flight search over its public MCP server.

No API key, and **stateless**: no `initialize` handshake, no session header,
just `tools/call` on every request. One tool, `search-flight`.

The quirk that shaped this pack's `partial` flag lives here. Each stateless
call returns a *varying sample* of itineraries — roughly fifteen — and the
sample can omit whole carriers. Two identical queries a second apart return
different pairings. So a single call is not a search result, it is a draw from
a distribution, and asking once means silently losing airlines.

The adapter therefore unions several queries, deduped by segment fingerprint
with the cheapest price per pairing winning. When one of those queries fails
but others succeed, the union is *incomplete but useful* — returned with
`partial=True` so the router grades it DEGRADED and keeps looking for something
better rather than treating a thinner answer as an equal one.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ...clock import Deadline
from ...outcomes import Failure, Outcome, rate_limited, terminal, transient
from ...provider import Attempt
from .models import FlightOffer, FlightResults
from .query import Cabin, FlightCapabilities, FlightQuery, Stops

__all__ = ["CABIN_TO_CABIN_CLASS", "KIWI_CAPABILITIES", "KiwiFlights"]

DEFAULT_URL = "https://mcp.kiwi.com/"
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

CABIN_TO_CABIN_CLASS = {
    Cabin.ECONOMY: "M",
    Cabin.PREMIUM_ECONOMY: "W",
    Cabin.BUSINESS: "C",
    Cabin.FIRST: "F",
}

COVERAGE_QUERIES = 2
"""How many draws to union.

Two is the number the source system settled on in production: enough to stop
losing a route's only nonstop pairing, few enough to stay inside an activity
timeout at ~4s per call.
"""

KIWI_CAPABILITIES = FlightCapabilities(
    cabin=True,
    airlines=True,
    paginates=False,
    max_stops=True,
    evidence=(
        "search-flight tools/list schema, verified 2026-08-06: cabinClass "
        "(M|W|C|F), select_airlines / exclude_airlines (mutually exclusive), "
        "max_sector_stopovers, sort. No limit/offset/page parameter of any "
        "kind — pagination is the one thing genuinely absent, which is why "
        "coverage is unioned across repeated queries instead."
    ),
)


class KiwiError(Exception):
    """A failure from the Kiwi MCP, carrying what we know about it."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class KiwiUnsupportedQuery(KiwiError):
    """The query cannot be expressed in Kiwi's parameters at all."""


def _is_rate_limit_text(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return "429" in lowered or "too many requests" in lowered or "rate limit" in lowered


def to_kiwi_date(iso_date: str) -> str:
    """``2026-09-15`` → ``15/09/2026``. Kiwi takes dd/mm/yyyy."""
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d/%m/%Y")


class KiwiFlights:
    """Flight search via the Kiwi.com MCP."""

    name = "kiwi"
    capabilities = KIWI_CAPABILITIES

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        client: httpx.AsyncClient | None = None,
        coverage_queries: int = COVERAGE_QUERIES,
    ) -> None:
        self._url = url
        self._client = client
        self._coverage_queries = max(1, coverage_queries)
        self._request_id = 0

    # ---------------------------------------------------------------- contract

    def supports(self, request: FlightQuery) -> bool:
        """Kiwi honors every constraint this pack models — with one exception.

        `select_airlines` and `exclude_airlines` are mutually exclusive on the
        provider, so a query asking for both cannot be expressed. Better to
        decline than to silently drop one half and return flights filtered by
        the other.
        """
        if request.include_airlines and request.exclude_airlines:
            return False
        return not request.unsupported_by(self.capabilities)

    def classify(self, exc: BaseException) -> Failure:
        if isinstance(exc, KiwiUnsupportedQuery):
            return terminal(str(exc), cause=exc)
        if isinstance(exc, KiwiError):
            if exc.status == 429 or _is_rate_limit_text(str(exc)):
                return rate_limited(str(exc), retry_after=exc.retry_after, cause=exc)
            if exc.status in RETRYABLE_STATUS:
                return transient(str(exc), cause=exc)
            return terminal(str(exc), cause=exc)
        if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
            return transient(str(exc), cause=exc)
        return terminal(str(exc), cause=exc)

    def assess(self, result: FlightResults, attempt: Attempt) -> Outcome:
        """A union missing one of its draws may be missing whole airlines."""
        return Outcome.DEGRADED if result.partial else Outcome.OK

    async def invoke(self, request: FlightQuery, deadline: Deadline) -> FlightResults:
        """Union several draws, keeping the cheapest price per distinct pairing."""
        merged: dict[str, FlightOffer] = {}
        failures: list[str] = []
        completed = 0

        for draw in range(self._coverage_queries):
            if draw and deadline.expired():
                failures.append("deadline reached before all coverage queries ran")
                break
            try:
                payload = await self._call(request, deadline)
            except KiwiUnsupportedQuery:
                raise
            except KiwiError as exc:
                # One lost draw is survivable; losing them all is not.
                failures.append(str(exc))
                continue
            completed += 1
            for offer in self._parse_offers(payload):
                key = _fingerprint(offer)
                existing = merged.get(key)
                if existing is None or offer.price_amount < existing.price_amount:
                    merged[key] = offer

        if completed == 0:
            raise KiwiError(f"all {self._coverage_queries} coverage queries failed: {failures}")

        offers = sorted(merged.values(), key=lambda o: o.price_amount)[: request.limit]
        return FlightResults(
            offers=offers,
            origin=request.origin.upper(),
            destination=request.destination.upper(),
            departure_date=request.departure_date,
            return_date=request.return_date,
            round_trip=request.round_trip,
            provider=self.name,
            partial=bool(failures),
            # Name the cause, not just the count: "a draw errored" and "we ran
            # out of time" call for different responses from whoever reads it.
            partial_reason=(
                f"{completed}/{self._coverage_queries} coverage queries succeeded "
                f"({failures[0]}); some pairings may be missing"
                if failures
                else None
            ),
        )

    # ---------------------------------------------------------------- internals

    def _arguments(self, request: FlightQuery) -> dict[str, Any]:
        args: dict[str, Any] = {
            "flyFrom": request.origin.upper(),
            "flyTo": request.destination.upper(),
            "departureDate": to_kiwi_date(request.departure_date),
            "adults": request.adults,
            "sort": "price",
        }
        if request.return_date:
            args["returnDate"] = to_kiwi_date(request.return_date)
        if request.cabin is not None:
            args["cabinClass"] = CABIN_TO_CABIN_CLASS[request.cabin]
        if request.stops is Stops.NONSTOP:
            args["max_sector_stopovers"] = 0

        # Mutually exclusive on the provider; `supports()` already declined the
        # both-at-once case, so at most one of these is set here.
        if request.include_airlines:
            args["select_airlines"] = ",".join(request.include_airlines)
        elif request.exclude_airlines:
            args["exclude_airlines"] = ",".join(request.exclude_airlines)
        return args

    async def _call(self, request: FlightQuery, deadline: Deadline) -> dict[str, Any]:
        if request.include_airlines and request.exclude_airlines:
            raise KiwiUnsupportedQuery(
                "Kiwi cannot both include and exclude airlines in one search"
            )

        client = self._client or httpx.AsyncClient()
        owned = self._client is None
        try:
            self._request_id += 1
            remaining = deadline.remaining()
            response = await client.post(
                self._url,
                json={
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": "tools/call",
                    "params": {"name": "search-flight", "arguments": self._arguments(request)},
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                timeout=remaining if remaining else None,
            )
            if response.status_code >= 400:
                raise KiwiError(
                    f"Kiwi MCP returned {response.status_code}",
                    status=response.status_code,
                    retry_after=_retry_after(response),
                )
            return self._extract(_parse_sse_json(response.text))
        finally:
            if owned:
                await client.aclose()

    @staticmethod
    def _extract(body: dict[str, Any]) -> dict[str, Any]:
        if "error" in body:
            raise KiwiError(f"MCP error: {json.dumps(body['error'])[:300]}")
        result = body.get("result") or {}
        if result.get("isError"):
            raise KiwiError(f"MCP tool error: {json.dumps(result)[:300]}")
        for item in result.get("content") or []:
            if item.get("type") == "text":
                try:
                    decoded = json.loads(item["text"])
                except (ValueError, KeyError):
                    continue
                if isinstance(decoded, dict):
                    return decoded
        return result if isinstance(result, dict) else {}

    def _parse_offers(self, payload: dict[str, Any]) -> list[FlightOffer]:
        currency = payload.get("currency") or "USD"
        offers: list[FlightOffer] = []
        for itinerary in payload.get("itineraries") or payload.get("flights") or []:
            offer = self._normalize(itinerary, currency)
            if offer is not None:
                offers.append(offer)
        return offers

    def _normalize(self, itinerary: dict[str, Any], currency: str) -> FlightOffer | None:
        """Map one Kiwi itinerary onto the pack's contract.

        Top-level fields describe the **outbound** leg; the full structured
        itinerary — inbound included — rides along in ``raw`` for callers that
        need it. Kiwi is the one provider that returns segments structurally,
        so unlike Skiplagged there is no id string to parse.
        """
        price = _decimal(itinerary.get("price"))
        if price is None or price <= 0:
            return None

        outbound = itinerary.get("outbound")
        if not isinstance(outbound, dict):
            return None

        segments = outbound.get("segments")
        first = segments[0] if isinstance(segments, list) and segments else {}
        carrier = first.get("carrier")

        duration_seconds = outbound.get("durationSeconds")
        duration_minutes = (
            int(duration_seconds) // 60 if isinstance(duration_seconds, int | float) else None
        )
        stops = outbound.get("stops")
        stops = stops if isinstance(stops, int) else 0

        return FlightOffer(
            departure_airport=str(outbound.get("from") or "").upper(),
            arrival_airport=str(outbound.get("to") or "").upper(),
            departure_time=_iso(outbound.get("departureTime")),
            arrival_time=_iso(outbound.get("arrivalTime")),
            carrier_code=carrier,
            flight_number=_designator(carrier, first.get("flightNumber")),
            duration_minutes=duration_minutes,
            stops=stops,
            stops_text="Direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}",
            price_amount=price,
            price_currency=currency,
            price_display=itinerary.get("priceFormatted"),
            booking_link=itinerary.get("bookingUrl"),
            provider=self.name,
            raw=dict(itinerary),
        )


def _designator(carrier: Any, flight_number: Any) -> str | None:
    """Kiwi returns a bare number; the contract wants ``AS3361``.

    Prefixing here rather than in the caller is the whole point — a client that
    renders ``carrier_code + flight_number`` on an already-prefixed value emits
    "AS AS3361", so exactly one layer may do it, and it is this one.
    """
    if flight_number is None:
        return None
    number = str(flight_number).strip()
    if not number:
        return None
    code = str(carrier or "").strip().upper()
    return f"{code}{number}" if code and not number.upper().startswith(code) else number


def _fingerprint(offer: FlightOffer) -> str:
    """Identity of a pairing, stable across separate draws.

    Kiwi's ids carry a per-query prefix, so the same pairing gets a different id
    on every search — dedupe on the actual segments instead, or the union does
    nothing.
    """
    raw = offer.raw or {}
    parts: list[str] = []
    for leg_key in ("outbound", "inbound"):
        leg = raw.get(leg_key)
        if not isinstance(leg, dict):
            continue
        for segment in leg.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            parts.append(
                f"{segment.get('carrier')}{segment.get('flightNumber')}"
                f"@{segment.get('departureTime')}"
            )
    if parts:
        return "|".join(parts)
    # No structured segments to fingerprint — fall back to the coarse shape so
    # two genuinely different offers are never merged into one.
    return (
        f"{offer.departure_airport}-{offer.arrival_airport}"
        f"@{offer.departure_time}:{offer.price_amount}"
    )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_sse_json(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data: "):
            text = line[6:]
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise KiwiError(f"unparseable response body: {text[:200]}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
