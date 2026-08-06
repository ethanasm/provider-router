"""The Skiplagged adapter: classification, argument mapping, and normalization."""

from __future__ import annotations

import json

import httpx
import pytest
from provider_router import Deadline, FailureKind, ManualClock, Outcome
from provider_router.conformance import assert_provider_contract

from flights import (
    Cabin,
    FlightQuery,
    FlightResults,
    Stops,
    check_capability_evidence,
)
from flights.skiplagged import (
    SkiplaggedError,
    SkiplaggedFlights,
    parse_flight_numbers,
)


def _query(**kw) -> FlightQuery:
    base = {"origin": "sfo", "destination": "jfk", "departure_date": "2026-09-15"}
    return FlightQuery(**{**base, **kw})


def _mcp_body(flights: list[dict], *, more: bool = False) -> str:
    payload = {"flights": flights, "pagination": {"hasMoreResults": more, "limit": 75}}
    content = [{"type": "text", "text": json.dumps(payload)}]
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": content}})


def _transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


OFFER = {
    "id": "SFO-JFK-2026-09-15-trip=AS3361-UA742",
    "price": "145.00",
    "origin": "SFO",
    "destination": "JFK",
    "airlineName": "Alaska Airlines",
    "stops": 1,
}


# --------------------------------------------------------------------- contract


def test_adapter_satisfies_the_core_provider_contract():
    assert_provider_contract(
        SkiplaggedFlights(),
        sample_request=_query(),
        sample_result=FlightResults(
            origin="SFO", destination="JFK", departure_date="2026-09-15", provider="skiplagged"
        ),
        expected_classifications={
            SkiplaggedError("throttled", status=429): FailureKind.RATE_LIMITED,
            SkiplaggedError("boom", status=503): FailureKind.TRANSIENT,
            SkiplaggedError("bad airport"): FailureKind.TERMINAL,
        },
    )


def test_capabilities_cite_their_source():
    """Every capability here was read off the provider's schema, not our signature."""
    assert check_capability_evidence(SkiplaggedFlights.capabilities, "skiplagged") == []
    assert "tools/list" in SkiplaggedFlights.capabilities.evidence


def test_supports_everything_this_pack_models():
    q = _query(cabin=Cabin.FIRST, stops=Stops.NONSTOP, exclude_airlines=("NK",))
    assert SkiplaggedFlights().supports(q)


# --------------------------------------------------------------- classification


def test_an_in_payload_throttle_is_a_rate_limit_not_a_terminal_error():
    """Skiplagged reports some throttles as prose in a 200 response.

    Misreading one as terminal means the breaker never opens and we keep
    hammering a provider that is actively asking us to stop.
    """
    failure = SkiplaggedFlights().classify(
        SkiplaggedError("Failed to fetch from search: Request failed with status code 429")
    )
    assert failure.kind is FailureKind.RATE_LIMITED


def test_retry_after_is_carried_through_to_the_breaker():
    failure = SkiplaggedFlights().classify(SkiplaggedError("slow down", status=429, retry_after=90))
    assert failure.kind is FailureKind.RATE_LIMITED
    assert failure.retry_after == 90


def test_transport_blips_are_transient():
    for exc in (httpx.ConnectTimeout("timeout"), httpx.ReadError("reset")):
        assert SkiplaggedFlights().classify(exc).kind is FailureKind.TRANSIENT


def test_an_unrecognized_error_is_terminal_not_optimistically_transient():
    """Guessing 'transient' on an unknown error turns a misconfiguration into a loop."""
    assert SkiplaggedFlights().classify(ValueError("nope")).kind is FailureKind.TERMINAL


# ------------------------------------------------------------ argument mapping


@pytest.mark.parametrize(
    ("cabin", "expected"),
    [
        (Cabin.ECONOMY, "economy"),
        (Cabin.PREMIUM_ECONOMY, "premium"),
        (Cabin.BUSINESS, "business"),
        (Cabin.FIRST, "first"),
    ],
)
def test_cabin_maps_onto_fare_class(cabin, expected):
    args = SkiplaggedFlights()._arguments(_query(cabin=cabin), 0)
    assert args["fareClass"] == expected


def test_absent_constraints_are_not_sent_at_all():
    """Let the provider apply its own defaults rather than inventing them here."""
    args = SkiplaggedFlights()._arguments(_query(), 0)
    for key in ("fareClass", "maxStops", "returnDate", "preferredAirlines", "excludedAirlines"):
        assert key not in args


def test_airlines_are_pushed_down_to_the_provider():
    """Supported server-side, so filtering in memory would fetch and discard."""
    args = SkiplaggedFlights()._arguments(
        _query(include_airlines=("AS", "UA"), exclude_airlines=("NK",)), 0
    )
    assert args["preferredAirlines"] == ["AS", "UA"]
    assert args["excludedAirlines"] == ["NK"]


def test_airport_codes_are_upper_cased():
    args = SkiplaggedFlights()._arguments(_query(), 0)
    assert args["origin"] == "SFO" and args["destination"] == "JFK"


# ------------------------------------------------------------------ id parsing


@pytest.mark.parametrize(
    ("offer_id", "expected"),
    [
        ("SFO-JFK-2026-09-15-trip=AS3361", ["AS3361"]),
        ("SFO-CDG-2026-06-15-2026-06-22-trip=AC744-LH6825,TS251", ["AC744", "LH6825", "TS251"]),
        ("SFO-JFK-trip=~AS3361", ["AS3361"]),  # hidden-city marker stripped
        ("no-trip-marker-here", []),
        (None, []),
        ("SFO-JFK-trip=", []),
    ],
)
def test_flight_numbers_are_parsed_out_of_the_id_string(offer_id, expected):
    """The provider has no structured field for this; the contract requires one."""
    assert parse_flight_numbers(offer_id) == expected


# ------------------------------------------------------------------- invoking


async def test_a_single_page_search_normalizes_offers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_mcp_body([OFFER]), headers={"mcp-session-id": "s1"})

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await SkiplaggedFlights(client=client).invoke(
            _query(), Deadline.in_seconds(30, clock)
        )

    assert result.count == 1
    offer = result.offers[0]
    assert offer.flight_number == "AS3361"
    assert offer.carrier_code == "AS", "derived from the designator when not supplied"
    assert offer.price_amount == 145
    assert offer.provider == "skiplagged"
    assert not result.partial


async def test_pagination_stops_when_the_provider_says_it_is_done():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, text="{}", headers={"mcp-session-id": "s1"})
        calls.append(body["params"]["arguments"])
        more = len(calls) < 2
        return httpx.Response(200, text=_mcp_body([OFFER], more=more))

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await SkiplaggedFlights(client=client).invoke(
            _query(), Deadline.in_seconds(30, clock)
        )

    assert len(calls) == 2
    assert calls[1]["offset"] == 75, "second page advances by the reported page size"
    assert result.count == 2
    assert not result.partial


async def test_hitting_the_page_cap_marks_the_result_partial():
    """More results existed than we fetched — the caller is told, not left guessing."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, text="{}", headers={"mcp-session-id": "s1"})
        return httpx.Response(200, text=_mcp_body([OFFER], more=True))

    clock = ManualClock()
    async with _transport(handler) as client:
        provider = SkiplaggedFlights(client=client, max_pages=2)
        result = await provider.invoke(_query(), Deadline.in_seconds(30, clock))

    assert result.partial and "cap" in result.partial_reason
    assert provider.assess(result, None) is Outcome.DEGRADED  # type: ignore[arg-type]


async def test_an_http_429_becomes_a_classified_rate_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down", headers={"retry-after": "45"})

    clock = ManualClock()
    async with _transport(handler) as client:
        provider = SkiplaggedFlights(client=client)
        with pytest.raises(SkiplaggedError) as excinfo:
            await provider.invoke(_query(), Deadline.in_seconds(30, clock))

    failure = provider.classify(excinfo.value)
    assert failure.kind is FailureKind.RATE_LIMITED
    assert failure.retry_after == 45


async def test_an_in_band_tool_error_is_raised_not_returned_as_empty():
    """`isError` in a 200 must not read as 'no flights found'."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, text="{}", headers={"mcp-session-id": "s1"})
        error_result = {"isError": True, "content": []}
        return httpx.Response(
            200,
            text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": error_result}),
        )

    clock = ManualClock()
    async with _transport(handler) as client:
        with pytest.raises(SkiplaggedError, match="tool error"):
            await SkiplaggedFlights(client=client).invoke(_query(), Deadline.in_seconds(30, clock))


async def test_one_malformed_offer_does_not_lose_the_others():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_mcp_body([{"id": "junk"}, OFFER]),  # first has no price
            headers={"mcp-session-id": "s1"},
        )

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await SkiplaggedFlights(client=client).invoke(
            _query(), Deadline.in_seconds(30, clock)
        )

    assert result.count == 1


async def test_a_deadline_reached_mid_sweep_returns_what_it_has_marked_partial():
    """Better a smaller honest answer than a blown activity timeout.

    The router owns failover, the adapter owns bounding itself — that split is
    why `invoke` takes a deadline at all.
    """
    clock = ManualClock()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, text="{}", headers={"mcp-session-id": "s1"})
        clock.advance(20)  # each page eats into the budget
        return httpx.Response(200, text=_mcp_body([OFFER], more=True))

    async with _transport(handler) as client:
        result = await SkiplaggedFlights(client=client, max_pages=4).invoke(
            _query(), Deadline.in_seconds(30, clock)
        )

    assert result.count == 2, "one full page, then the deadline stopped the sweep"
    assert result.partial and "deadline" in result.partial_reason


async def test_a_jsonrpc_level_error_is_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, text="{}", headers={"mcp-session-id": "s1"})
        return httpx.Response(
            200,
            text=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}}
            ),
        )

    clock = ManualClock()
    async with _transport(handler) as client:
        with pytest.raises(SkiplaggedError, match="MCP error"):
            await SkiplaggedFlights(client=client).invoke(_query(), Deadline.in_seconds(30, clock))


async def test_an_unparseable_body_fails_loudly_rather_than_reading_as_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>", headers={"mcp-session-id": "s"})

    clock = ManualClock()
    async with _transport(handler) as client:
        with pytest.raises(SkiplaggedError, match="unparseable"):
            await SkiplaggedFlights(client=client).invoke(_query(), Deadline.in_seconds(30, clock))


def test_http_status_errors_classify_by_status():
    provider = SkiplaggedFlights()
    request = httpx.Request("POST", "https://example.test")

    throttled = httpx.HTTPStatusError(
        "429",
        request=request,
        response=httpx.Response(429, headers={"retry-after": "12"}, request=request),
    )
    assert provider.classify(throttled).kind is FailureKind.RATE_LIMITED
    assert provider.classify(throttled).retry_after == 12

    unavailable = httpx.HTTPStatusError(
        "503", request=request, response=httpx.Response(503, request=request)
    )
    assert provider.classify(unavailable).kind is FailureKind.TRANSIENT

    not_found = httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )
    assert provider.classify(not_found).kind is FailureKind.TERMINAL


def test_a_non_numeric_retry_after_is_ignored_rather_than_guessed():
    """The HTTP-date form is valid but we do not parse it; better None than wrong."""
    request = httpx.Request("POST", "https://example.test")
    dated = httpx.HTTPStatusError(
        "429",
        request=request,
        response=httpx.Response(
            429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, request=request
        ),
    )
    failure = SkiplaggedFlights().classify(dated)
    assert failure.kind is FailureKind.RATE_LIMITED
    assert failure.retry_after is None
