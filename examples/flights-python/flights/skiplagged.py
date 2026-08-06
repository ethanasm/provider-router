"""Skiplagged adapter — flight search over its public MCP server.

No API key. Speaks JSON-RPC 2.0 over Streamable HTTP: one `initialize`
handshake, then `tools/call` carrying the session id from the response header.

Two provider quirks are normalized here rather than leaking to callers:

* **Flight numbers are not structured.** They are encoded in the offer's id
  string — `SFO-CDG-2026-06-15-trip=AC744-LH6825,TS251` — and parsed out. The
  `~` prefix marks a hidden-city segment.
* **Rate limits arrive two different ways.** Sometimes an HTTP 429, sometimes a
  perfectly ordinary 200 whose payload says "Request failed with status code
  429", because the MCP server proxies a fare backend that throttles it. Both
  have to classify as RATE_LIMITED or the breaker never opens.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from provider_router.clock import Deadline
from provider_router.outcomes import Failure, Outcome, rate_limited, terminal, transient
from provider_router.provider import Attempt

from .models import FlightOffer, FlightResults
from .query import Cabin, FlightCapabilities, FlightQuery, Stops

__all__ = ["CABIN_TO_FARE_CLASS", "SKIPLAGGED_CAPABILITIES", "SkiplaggedFlights"]

DEFAULT_URL = "https://mcp.skiplagged.com/mcp"
MCP_PROTOCOL_VERSION = "2024-11-05"

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

CABIN_TO_FARE_CLASS = {
    Cabin.ECONOMY: "economy",
    Cabin.PREMIUM_ECONOMY: "premium",
    Cabin.BUSINESS: "business",
    Cabin.FIRST: "first",
}

SKIPLAGGED_CAPABILITIES = FlightCapabilities(
    cabin=True,
    airlines=True,
    paginates=True,
    max_stops=True,
    evidence=(
        "sk_flights_search tools/list schema, verified 2026-08-06: fareClass "
        "(enum basic-economy|economy|premium|business|first, default economy), "
        "preferredAirlines, excludedAirlines, maxStops."
    ),
)
"""Every capability here was read off the provider's own schema.

Worth saying explicitly, because the alternative — reading them off *this
adapter's* function signature — is how the source codebase came to believe
Skiplagged could not filter by cabin. It could, all along; the client simply
never sent `fareClass`, so every search silently returned the economy default.
"""


class SkiplaggedError(Exception):
    """Any failure from the Skiplagged MCP, carrying what we know about it."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _is_rate_limit_text(message: str | None) -> bool:
    """Detect a throttle described in prose rather than a status code."""
    if not message:
        return False
    lowered = message.lower()
    return "429" in lowered or "too many requests" in lowered or "rate limit" in lowered


def parse_flight_numbers(offer_id: str | None) -> list[str]:
    """Pull carrier-prefixed designators out of Skiplagged's id string.

    ``…-trip=AC744-LH6825,TS251`` → ``["AC744", "LH6825", "TS251"]``. Legs are
    comma-separated, segments hyphen-separated, and a leading ``~`` marks a
    hidden-city segment. Returns ``[]`` for anything unparseable — a missing
    flight number degrades the offer, it does not fail the search.
    """
    if not offer_id or "trip=" not in offer_id:
        return []
    designators: list[str] = []
    for leg in offer_id.split("trip=", 1)[1].split(","):
        for segment in leg.split("-"):
            token = segment.lstrip("~").strip()
            if re.fullmatch(r"[A-Z0-9]{2}\d{1,4}", token):
                designators.append(token)
    return designators


class SkiplaggedFlights:
    """Flight search via the Skiplagged MCP."""

    name = "skiplagged"
    capabilities = SKIPLAGGED_CAPABILITIES

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        client: httpx.AsyncClient | None = None,
        max_pages: int = 4,
    ) -> None:
        self._url = url.rstrip("/")
        self._client = client
        self._max_pages = max_pages
        self._session_id: str | None = None
        self._request_id = 0

    # ---------------------------------------------------------------- contract

    def supports(self, request: FlightQuery) -> bool:
        """Skiplagged honors every constraint this pack models."""
        return not request.unsupported_by(self.capabilities)

    def classify(self, exc: BaseException) -> Failure:
        if isinstance(exc, SkiplaggedError):
            if exc.status == 429 or _is_rate_limit_text(str(exc)):
                return rate_limited(str(exc), retry_after=exc.retry_after, cause=exc)
            if exc.status in RETRYABLE_STATUS:
                return transient(str(exc), cause=exc)
            return terminal(str(exc), cause=exc)
        if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
            return transient(str(exc), cause=exc)
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 429:
                return rate_limited(str(exc), retry_after=_retry_after(exc.response), cause=exc)
            if status in RETRYABLE_STATUS:
                return transient(str(exc), cause=exc)
        return terminal(str(exc), cause=exc)

    def assess(self, result: FlightResults, attempt: Attempt) -> Outcome:
        """A paginated sweep that stopped early answered a smaller question."""
        return Outcome.DEGRADED if result.partial else Outcome.OK

    async def invoke(self, request: FlightQuery, deadline: Deadline) -> FlightResults:
        """Walk pages until the provider runs out, the cap is hit, or time is up."""
        offers: list[FlightOffer] = []
        partial_reason: str | None = None
        offset = 0

        for page in range(self._max_pages):
            if page and deadline.expired():
                partial_reason = f"deadline reached after {page} page(s)"
                break

            payload = await self._call(
                "sk_flights_search", self._arguments(request, offset), deadline
            )
            offers.extend(self._parse_offers(payload))

            pagination = payload.get("pagination") or {}
            if not pagination.get("hasMoreResults"):
                break
            offset += pagination.get("limit") or request.limit
        else:
            # Loop ran to the page cap without the provider saying it was done.
            partial_reason = f"stopped at the {self._max_pages}-page cap"

        return FlightResults(
            offers=offers,
            origin=request.origin.upper(),
            destination=request.destination.upper(),
            departure_date=request.departure_date,
            return_date=request.return_date,
            round_trip=request.round_trip,
            provider=self.name,
            partial=partial_reason is not None,
            partial_reason=partial_reason,
        )

    # ---------------------------------------------------------------- internals

    def _arguments(self, request: FlightQuery, offset: int) -> dict[str, Any]:
        """Build the tool arguments, sending only what the caller actually asked for."""
        args: dict[str, Any] = {
            "origin": request.origin.upper(),
            "destination": request.destination.upper(),
            "departureDate": request.departure_date,
            "adults": request.adults,
            "limit": request.limit,
            "offset": offset,
            "includeHiddenCity": False,
        }
        if request.return_date:
            args["returnDate"] = request.return_date
        if request.stops is Stops.NONSTOP:
            args["maxStops"] = "none"
        if request.cabin is not None:
            args["fareClass"] = CABIN_TO_FARE_CLASS[request.cabin]
        if request.include_airlines:
            args["preferredAirlines"] = list(request.include_airlines)
        if request.exclude_airlines:
            args["excludedAirlines"] = list(request.exclude_airlines)
        return args

    async def _call(
        self, tool: str, arguments: dict[str, Any], deadline: Deadline
    ) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient()
        owned = self._client is None
        try:
            await self._ensure_session(client, deadline)
            body = await self._post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
                deadline,
            )
            return self._extract(body)
        finally:
            if owned:
                await client.aclose()

    async def _ensure_session(self, client: httpx.AsyncClient, deadline: Deadline) -> None:
        if self._session_id is not None:
            return
        response = await self._send(
            client,
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "provider-router", "version": "0.1.0"},
                },
            },
            deadline,
        )
        self._session_id = response.headers.get("mcp-session-id")

    async def _post(
        self, client: httpx.AsyncClient, payload: dict[str, Any], deadline: Deadline
    ) -> dict[str, Any]:
        response = await self._send(client, payload, deadline)
        return _parse_sse_json(response.text)

    async def _send(
        self, client: httpx.AsyncClient, payload: dict[str, Any], deadline: Deadline
    ) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        remaining = deadline.remaining()
        response = await client.post(
            self._url,
            json=payload,
            headers=headers,
            timeout=remaining if remaining else None,
        )
        if response.status_code >= 400:
            raise SkiplaggedError(
                f"Skiplagged MCP returned {response.status_code}",
                status=response.status_code,
                retry_after=_retry_after(response),
            )
        return response

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @staticmethod
    def _extract(body: dict[str, Any]) -> dict[str, Any]:
        """Unwrap the tool payload, treating an in-band error as a real failure.

        The MCP can report failure as a *successful* JSON-RPC response carrying
        `isError`. Letting that through as an empty result is how a throttle
        turns into "no flights found".
        """
        if "error" in body:
            message = json.dumps(body["error"])
            raise SkiplaggedError(f"MCP error: {message}")

        result = body.get("result") or {}
        if result.get("isError"):
            raise SkiplaggedError(f"MCP tool error: {json.dumps(result)[:400]}")

        content = result.get("content") or []
        for item in content:
            if item.get("type") == "text":
                try:
                    decoded = json.loads(item["text"])
                except (ValueError, KeyError):
                    continue
                if isinstance(decoded, dict):
                    return decoded
        return result if isinstance(result, dict) else {}

    def _parse_offers(self, payload: dict[str, Any]) -> list[FlightOffer]:
        offers: list[FlightOffer] = []
        for raw in payload.get("flights") or []:
            offer = self._normalize(raw)
            if offer is not None:
                offers.append(offer)
        return offers

    def _normalize(self, raw: dict[str, Any]) -> FlightOffer | None:
        """Map one provider offer onto the pack's contract.

        Returns ``None`` rather than raising when an offer is unusable: one
        malformed row should cost that row, not the whole search.
        """
        price = _decimal(raw.get("price") or raw.get("priceAmount"))
        if price is None:
            return None

        designators = parse_flight_numbers(raw.get("id"))
        carrier = raw.get("carrierCode") or (designators[0][:2] if designators else None)

        return FlightOffer(
            departure_airport=(raw.get("origin") or raw.get("departureAirport") or "").upper(),
            arrival_airport=(raw.get("destination") or raw.get("arrivalAirport") or "").upper(),
            airline_name=raw.get("airlineName") or raw.get("airline"),
            carrier_code=carrier,
            flight_number=designators[0] if designators else None,
            duration_minutes=raw.get("durationMinutes") or raw.get("duration"),
            stops=raw.get("stops") or 0,
            stops_text=raw.get("stopsText"),
            price_amount=price,
            price_currency=raw.get("currency") or "USD",
            price_display=raw.get("priceDisplay"),
            booking_link=raw.get("bookingLink") or raw.get("url"),
            provider=self.name,
            raw=raw,
        )


def _retry_after(response: httpx.Response) -> float | None:
    """Numeric `Retry-After` only; the HTTP-date form is ignored rather than guessed."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_sse_json(text: str) -> dict[str, Any]:
    """Read a JSON-RPC body that may or may not be SSE-framed."""
    for line in text.splitlines():
        if line.startswith("data: "):
            text = line[6:]
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise SkiplaggedError(f"unparseable response body: {text[:200]}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
