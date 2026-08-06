"""The Kiwi adapter: coverage unions, partiality, and the mutually-exclusive filters."""

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
from flights.kiwi import (
    KiwiError,
    KiwiFlights,
    KiwiUnsupportedQuery,
    to_kiwi_date,
)


def _query(**kw) -> FlightQuery:
    base = {"origin": "sfo", "destination": "jfk", "departure_date": "2026-09-15"}
    return FlightQuery(**{**base, **kw})


def _itinerary(carrier="AS", number=3361, price="450.00", dep="2026-09-15T08:00:00Z"):
    return {
        "price": price,
        "outbound": {
            "from": "SFO",
            "to": "JFK",
            "departureTime": dep,
            "arrivalTime": "2026-09-15T16:30:00Z",
            "durationSeconds": 19800,
            "stops": 0,
            "segments": [{"carrier": carrier, "flightNumber": number, "departureTime": dep}],
        },
    }


def _body(itineraries: list[dict]) -> str:
    payload = {"itineraries": itineraries, "currency": "USD"}
    content = [{"type": "text", "text": json.dumps(payload)}]
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": content}})


def _transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------- contract


def test_adapter_satisfies_the_core_provider_contract():
    assert_provider_contract(
        KiwiFlights(),
        sample_request=_query(),
        sample_result=FlightResults(
            origin="SFO", destination="JFK", departure_date="2026-09-15", provider="kiwi"
        ),
        expected_classifications={
            KiwiError("throttled", status=429): FailureKind.RATE_LIMITED,
            KiwiError("boom", status=503): FailureKind.TRANSIENT,
            KiwiUnsupportedQuery("cannot express"): FailureKind.TERMINAL,
        },
    )


def test_capabilities_cite_their_source_including_the_one_genuine_gap():
    """`paginates=False` is the only negative claim, and it carries its evidence."""
    caps = KiwiFlights.capabilities
    assert check_capability_evidence(caps, "kiwi") == []
    assert caps.paginates is False
    assert caps.cabin and caps.airlines and caps.max_stops
    assert "no limit/offset/page" in caps.evidence.lower()


# ------------------------------------------------------------ argument mapping


def test_dates_are_reformatted_for_the_provider():
    assert to_kiwi_date("2026-09-15") == "15/09/2026"


@pytest.mark.parametrize(
    ("cabin", "expected"),
    [(Cabin.ECONOMY, "M"), (Cabin.PREMIUM_ECONOMY, "W"), (Cabin.BUSINESS, "C"), (Cabin.FIRST, "F")],
)
def test_cabin_maps_onto_single_letter_codes(cabin, expected):
    assert KiwiFlights()._arguments(_query(cabin=cabin))["cabinClass"] == expected


def test_nonstop_becomes_a_stopover_ceiling():
    assert KiwiFlights()._arguments(_query(stops=Stops.NONSTOP))["max_sector_stopovers"] == 0
    assert "max_sector_stopovers" not in KiwiFlights()._arguments(_query())


def test_airline_filters_are_comma_joined():
    args = KiwiFlights()._arguments(_query(include_airlines=("AS", "UA")))
    assert args["select_airlines"] == "AS,UA"

    args = KiwiFlights()._arguments(_query(exclude_airlines=("NK", "F9")))
    assert args["exclude_airlines"] == "NK,F9"


def test_include_and_exclude_together_is_declined_not_half_applied():
    """The provider treats these as mutually exclusive.

    Silently dropping one half would return flights filtered by the other — an
    answer to a question nobody asked.
    """
    both = _query(include_airlines=("AS",), exclude_airlines=("NK",))
    assert not KiwiFlights().supports(both)
    assert KiwiFlights().supports(_query(include_airlines=("AS",)))


# ---------------------------------------------------------------- the union


async def test_repeated_draws_are_unioned_and_deduped_by_segments():
    """Each stateless call is a sample, not a result — so ask more than once.

    Kiwi's ids carry a per-query prefix, so dedupe must key on segments or the
    union collapses to nothing.
    """
    draws = [
        [_itinerary("AS", 3361), _itinerary("UA", 742, price="500.00")],
        [_itinerary("AS", 3361), _itinerary("B6", 615, price="399.00")],
    ]
    seen = iter(draws)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_body(next(seen)))

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await KiwiFlights(client=client).invoke(_query(), Deadline.in_seconds(30, clock))

    assert result.count == 3, "AS3361 appeared in both draws and is counted once"
    assert not result.partial
    assert [str(o.price_amount) for o in result.offers] == ["399.00", "450.00", "500.00"]


async def test_the_cheapest_price_wins_for_a_repeated_pairing():
    draws = [[_itinerary("AS", 3361, price="450.00")], [_itinerary("AS", 3361, price="410.00")]]
    seen = iter(draws)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_body(next(seen)))

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await KiwiFlights(client=client).invoke(_query(), Deadline.in_seconds(30, clock))

    assert result.count == 1
    assert str(result.offers[0].price_amount) == "410.00"


async def test_one_lost_draw_yields_a_partial_result_not_a_failure():
    """The case the pack's `partial` flag exists for.

    A surviving draw is useful but may be missing whole carriers, so it comes
    back flagged rather than passed off as a complete answer.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(503, text="upstream unavailable")
        return httpx.Response(200, text=_body([_itinerary()]))

    clock = ManualClock()
    async with _transport(handler) as client:
        provider = KiwiFlights(client=client)
        result = await provider.invoke(_query(), Deadline.in_seconds(30, clock))

    assert result.count == 1
    assert result.partial and "1/2" in result.partial_reason
    assert provider.assess(result, None) is Outcome.DEGRADED  # type: ignore[arg-type]


async def test_losing_every_draw_is_a_failure_not_an_empty_result():
    """No draw at all is not 'no flights' — it must classify and fail over."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    clock = ManualClock()
    async with _transport(handler) as client:
        provider = KiwiFlights(client=client)
        with pytest.raises(KiwiError) as excinfo:
            await provider.invoke(_query(), Deadline.in_seconds(30, clock))

    assert "all 2 coverage queries failed" in str(excinfo.value)


async def test_a_deadline_stops_further_draws_and_flags_the_result():
    clock = ManualClock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.advance(40)
        return httpx.Response(200, text=_body([_itinerary()]))

    async with _transport(handler) as client:
        result = await KiwiFlights(client=client).invoke(_query(), Deadline.in_seconds(30, clock))

    assert result.count == 1
    assert result.partial and "deadline" in result.partial_reason


# ------------------------------------------------------------- normalization


async def test_bare_flight_numbers_are_prefixed_into_designators():
    """Kiwi returns 3361; the contract requires AS3361 on every adapter."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_body([_itinerary("AS", 3361)]))

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await KiwiFlights(client=client, coverage_queries=1).invoke(
            _query(), Deadline.in_seconds(30, clock)
        )

    offer = result.offers[0]
    assert offer.flight_number == "AS3361"
    assert offer.carrier_code == "AS"
    # Rendering carrier_code + flight_number must not double-prefix.
    assert not offer.flight_number.startswith("ASAS")


async def test_an_already_prefixed_number_is_not_prefixed_twice():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_body([_itinerary("AS", "AS3361")]))

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await KiwiFlights(client=client, coverage_queries=1).invoke(
            _query(), Deadline.in_seconds(30, clock)
        )

    assert result.offers[0].flight_number == "AS3361"


async def test_offers_without_a_usable_price_are_skipped():
    bad = [{"price": None, "outbound": {}}, {"price": "0", "outbound": {"segments": []}}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_body([*bad, _itinerary()]))

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await KiwiFlights(client=client, coverage_queries=1).invoke(
            _query(), Deadline.in_seconds(30, clock)
        )

    assert result.count == 1


async def test_the_limit_is_applied_client_side():
    """No limit parameter exists on the provider — that is the pagination gap."""
    many = [_itinerary("AS", n, price=f"{400 + n}.00") for n in range(1, 8)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_body(many))

    clock = ManualClock()
    async with _transport(handler) as client:
        result = await KiwiFlights(client=client, coverage_queries=1).invoke(
            _query(limit=3), Deadline.in_seconds(30, clock)
        )

    assert result.count == 3, "cheapest three"
    assert [str(o.price_amount) for o in result.offers] == ["401.00", "402.00", "403.00"]


def test_an_inexpressible_query_classifies_as_terminal_not_transient():
    """Retrying or failing over on it changes nothing — the query itself is the problem."""
    failure = KiwiFlights().classify(KiwiUnsupportedQuery("both include and exclude"))
    assert failure.kind is FailureKind.TERMINAL
