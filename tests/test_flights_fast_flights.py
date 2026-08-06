"""The fast-flights adapter: scraped pages, payload drift, round-trip totals.

The canary tests below read stored HTML fixtures rather than mocking the
parser. That is the point: this provider has no schema, so the only thing that
can catch an index moving is parsing a real page shape and asserting on the
values that come out.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from provider_router import Deadline, FailureKind, ManualClock, Outcome
from provider_router.conformance import assert_provider_contract
from provider_router.packs.flights import (
    Cabin,
    FlightQuery,
    FlightResults,
    Stops,
    check_capability_evidence,
)
from provider_router.packs.flights.fast_flights import (
    FastFlights,
    FastFlightsBlocked,
    FastFlightsUnavailable,
    GoogleFlightsFetcher,
    build_google_query,
)
from provider_router.packs.flights.google_payload import (
    GooglePayloadError,
    NoFlightsFound,
    parse_flights_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / f"google_flights_{name}.html").read_text()


def _query(**kw) -> FlightQuery:
    base = {"origin": "sfo", "destination": "jfk", "departure_date": "2026-09-15"}
    return FlightQuery(**{**base, **kw})


def _serving(name: str):
    return lambda request: fixture(name)


async def _invoke(provider: FastFlights, request: FlightQuery | None = None) -> FlightResults:
    return await provider.invoke(request or _query(), Deadline.in_seconds(30, ManualClock()))


# --------------------------------------------------------------------- contract


def test_adapter_satisfies_the_core_provider_contract():
    assert_provider_contract(
        FastFlights(fetch=_serving("healthy")),
        sample_request=_query(),
        sample_result=FlightResults(
            origin="SFO", destination="JFK", departure_date="2026-09-15", provider="fast_flights"
        ),
        expected_classifications={
            FastFlightsBlocked("consent wall"): FailureKind.TRANSIENT,
            FastFlightsUnavailable("extra not installed"): FailureKind.TERMINAL,
        },
    )


def test_capabilities_cite_their_source_and_name_the_one_real_gap():
    caps = FastFlights.capabilities
    assert check_capability_evidence(caps, "fast_flights") == []
    assert caps.paginates is False
    assert caps.cabin and caps.airlines and caps.max_stops
    assert "one ranked page" in caps.evidence


def test_airline_exclusion_is_declined_rather_than_dropped():
    """The protobuf has an include-list and no exclude field.

    Dropping the exclusion would return exactly the carriers the caller asked
    to avoid, priced as if they were acceptable.
    """
    provider = FastFlights(fetch=_serving("healthy"))
    assert not provider.supports(_query(exclude_airlines=("NK",)))
    assert provider.supports(_query(include_airlines=("AS",)))
    assert provider.supports(_query(cabin=Cabin.BUSINESS, stops=Stops.NONSTOP))


# ----------------------------------------------------------------- the canary


def test_every_payload_index_the_parser_depends_on_still_reads():
    """Canary. When Google renumbers, this fails naming the field.

    Asserting on parsed *values* rather than on indexes keeps it honest: drop in
    a captured live page and the same assertions still apply.
    """
    page = parse_flights_page(fixture("healthy"))

    assert page.airline_code_to_name["AS"] == "Alaska Airlines"  # payload[7][1][1]
    assert [it.is_best for it in page.itineraries] == [True, False]  # sections 3 then 2

    nonstop = page.itineraries[0]
    assert nonstop.price == 298  # item[1][0][1]
    assert nonstop.airline_names == ("Alaska",)  # flight[1]

    seg = nonstop.segments[0]
    assert seg.from_code == "SFO"  # segment[3]
    assert seg.from_name == "San Francisco International Airport"  # segment[4]
    assert seg.to_code == "JFK"  # segment[6]
    assert seg.to_name == "John F. Kennedy International Airport"  # segment[5]
    assert seg.departure == datetime(2026, 9, 15, 8, 0)  # segment[20] + [8]
    assert seg.arrival == datetime(2026, 9, 15, 16, 30)  # segment[21] + [10]
    assert seg.duration_minutes == 330  # segment[11]
    assert seg.plane_type == "Airbus A321neo"  # segment[17]
    assert seg.carrier == "AS"  # segment[22][0]
    assert seg.flight_number == "3361"  # segment[22][1]
    assert seg.airline_name == "Alaska Airlines"  # segment[22][3]
    assert page.health.degraded is False


def test_an_omitted_trailing_time_component_is_not_a_parse_failure():
    """Google writes 08:00 as `[8]`. Read positionally, that loses the hour."""
    page = parse_flights_page(fixture("healthy"))
    assert page.itineraries[0].segments[0].departure == datetime(2026, 9, 15, 8, 0)


async def test_the_second_page_section_is_read_and_holds_the_cheaper_fare():
    """The upstream library reads only "best" — where the cheapest fare isn't."""
    result = await _invoke(FastFlights(fetch=_serving("healthy")))

    assert result.count == 2
    assert [str(o.price_amount) for o in result.offers] == ["243", "298"]
    cheapest = result.offers[0]
    assert cheapest.raw is not None and cheapest.raw["is_best"] is False


# ------------------------------------------------------------------ drift


async def test_payload_drift_degrades_rather_than_crashing_or_passing_silently():
    """The case this adapter's `partial` flag exists for.

    A renumbered payload still yields offers — just without flight numbers, and
    missing a whole section. Returning that as a clean result is the failure.
    """
    provider = FastFlights(fetch=_serving("drifted"))
    result = await _invoke(provider)

    assert result.count == 1, "what survived is still returned"
    assert result.offers[0].flight_number is None
    assert result.partial
    assert "segment[22]" in result.partial_reason
    assert "payload[2][0]" in result.partial_reason
    assert provider.assess(result, None) is Outcome.DEGRADED  # type: ignore[arg-type]


def test_an_empty_page_is_healthy_not_drifted():
    """No flights is an answer. Absent sections are unfalsifiable when empty."""
    page = parse_flights_page("<script class='ds:1'>data:[], x</script>")
    assert page.itineraries == ()
    assert page.health.degraded is False
    assert page.health.reason is None


# ------------------------------------------------------------ failure modes


async def test_googles_no_flights_answer_is_an_empty_result_not_a_failure():
    """Failing over would not conjure a flight that does not exist."""
    provider = FastFlights(fetch=_serving("no_results"))
    result = await _invoke(provider)

    assert result.count == 0
    assert not result.partial
    assert provider.assess(result, None) is Outcome.OK  # type: ignore[arg-type]


async def test_a_blocked_page_is_retried_then_raised_as_transient():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return "<html>we have detected unusual traffic</html>"

    provider = FastFlights(fetch=flaky, max_retries=2)
    with pytest.raises(FastFlightsBlocked) as excinfo:
        await _invoke(provider)

    assert calls["n"] == 3, "initial attempt plus two retries"
    assert provider.classify(excinfo.value).kind is FailureKind.TRANSIENT


async def test_a_page_that_recovers_on_retry_returns_normally():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return "<html>challenge</html>" if calls["n"] == 1 else fixture("healthy")

    result = await _invoke(FastFlights(fetch=flaky, max_retries=2))
    assert result.count == 2


async def test_retrying_stops_rather_than_sleeping_past_the_deadline():
    clock = ManualClock()

    def always_blocked(request):
        return "<html>challenge</html>"

    provider = FastFlights(fetch=always_blocked, max_retries=5)
    with pytest.raises(FastFlightsBlocked):
        await provider.invoke(_query(), Deadline.in_seconds(0.5, clock))


async def test_a_missing_dependency_is_terminal_and_not_retried():
    """No amount of retrying installs a package — the router should move on."""
    calls = {"n": 0}

    def unavailable(request):
        calls["n"] += 1
        raise FastFlightsUnavailable("pip install 'provider-router[fast-flights]'")

    provider = FastFlights(fetch=unavailable, max_retries=3)
    with pytest.raises(FastFlightsUnavailable) as excinfo:
        await _invoke(provider)

    assert calls["n"] == 1, "not retried"
    assert provider.classify(excinfo.value).kind is FailureKind.TERMINAL


def test_the_default_fetcher_reports_the_missing_extra_by_name():
    with pytest.raises(FastFlightsUnavailable, match="provider-router\\[fast-flights\\]"):
        GoogleFlightsFetcher()(_query())


def test_a_page_with_no_payload_script_names_the_likely_cause():
    with pytest.raises(GooglePayloadError, match="consent wall"):
        parse_flights_page("<html><body>Before you continue</body></html>")


def test_a_payload_that_is_not_json_is_reported_as_such():
    with pytest.raises(GooglePayloadError, match="not JSON"):
        parse_flights_page("<script class='ds:1'>data:{not json}, x</script>")


def test_googles_explicit_empty_answer_has_its_own_exception_type():
    with pytest.raises(NoFlightsFound):
        parse_flights_page(fixture("no_results"))


# --------------------------------------------------------- normalization


async def test_flight_number_is_a_full_designator_never_a_bare_number():
    result = await _invoke(FastFlights(fetch=_serving("healthy")))
    nonstop = next(o for o in result.offers if o.stops == 0)

    assert nonstop.flight_number == "AS3361"
    assert nonstop.carrier_code == "AS"
    # Rendering carrier_code + flight_number must not double-prefix.
    assert not nonstop.flight_number.startswith("ASAS")


async def test_a_connection_yields_stops_a_layover_and_wall_clock_duration():
    result = await _invoke(FastFlights(fetch=_serving("healthy")))
    connecting = next(o for o in result.offers if o.stops == 1)

    assert connecting.stops_text == "1 stop"
    assert [lay.airport for lay in connecting.layovers] == ["ORD"]
    assert connecting.layovers[0].duration_minutes == 95
    # 06:15 → 17:10 is 655 minutes; the two flying legs total only 380, and
    # reporting that would understate the trip by the whole layover.
    assert connecting.duration_minutes == 655
    assert connecting.departure_airport == "SFO"
    assert connecting.arrival_airport == "JFK"


async def test_airline_name_comes_from_the_pages_own_code_map():
    result = await _invoke(FastFlights(fetch=_serving("healthy")))
    assert {o.airline_name for o in result.offers} == {"Alaska Airlines", "United"}


async def test_a_round_trip_offer_is_marked_as_carrying_a_return_it_cannot_show():
    """The page prices round trips against outbound-only itineraries.

    Unmarked, such an offer is indistinguishable from a one-way at twice the
    going rate — and would be stored next to offers that mean something else.
    """
    result = await _invoke(FastFlights(fetch=_serving("healthy")), _query(return_date="2026-09-22"))

    assert result.round_trip
    assert all(o.round_trip_total for o in result.offers)


async def test_one_way_offers_are_not_marked():
    result = await _invoke(FastFlights(fetch=_serving("healthy")))
    assert not any(o.round_trip_total for o in result.offers)


async def test_the_limit_is_applied_client_side_cheapest_first():
    """There is no offset to advance — one page is the whole answer."""
    result = await _invoke(FastFlights(fetch=_serving("healthy")), _query(limit=1))
    assert [str(o.price_amount) for o in result.offers] == ["243"]


async def test_an_async_fetcher_is_awaited_rather_than_threaded():
    async def fetch(request):
        return fixture("healthy")

    result = await _invoke(FastFlights(fetch=fetch))
    assert result.count == 2


async def test_priced_at_zero_or_missing_is_dropped_not_shown_as_free():
    page_html = fixture("healthy").replace(", 298]]]", ", 0]]]")
    result = await _invoke(FastFlights(fetch=lambda request: page_html))

    assert result.count == 1
    assert result.offers[0].price_amount == Decimal(243)


# ------------------------------------------------------ the query translation


@pytest.fixture
def fake_fast_flights(monkeypatch):
    """Stand in for the optional extra, recording what it is asked to build.

    Injected rather than imported so the translation is covered on every run —
    a test skipped when the extra is absent is a test that never runs in CI.
    """
    built: dict[str, object] = {}

    class Leg:
        def __init__(self, *, date, from_airport, to_airport, max_stops=None, airlines=None):
            self.date, self.max_stops, self.airlines = date, max_stops, airlines
            self.route = (from_airport, to_airport)

    class Passengers:
        def __init__(self, *, adults):
            self.adults = adults

    def create_query(**kwargs):
        built.update(kwargs)
        return "query-object"

    module = types.ModuleType("fast_flights")
    module.FlightQuery = Leg  # type: ignore[attr-defined]
    module.Passengers = Passengers  # type: ignore[attr-defined]
    module.create_query = create_query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fast_flights", module)
    return built


def test_a_one_way_query_carries_cabin_stops_and_the_airline_include_list(fake_fast_flights):
    build_google_query(
        _query(cabin=Cabin.BUSINESS, stops=Stops.NONSTOP, include_airlines=("AS", "UA"), adults=2)
    )

    assert fake_fast_flights["trip"] == "one-way"
    assert fake_fast_flights["seat"] == "business"
    assert fake_fast_flights["passengers"].adults == 2
    (leg,) = fake_fast_flights["flights"]
    assert leg.route == ("SFO", "JFK")
    assert leg.max_stops == 0
    assert leg.airlines == ["AS", "UA"]


def test_an_unconstrained_query_sends_no_ceiling_and_no_airline_list(fake_fast_flights):
    build_google_query(_query())

    (leg,) = fake_fast_flights["flights"]
    assert leg.max_stops is None
    assert leg.airlines is None
    assert fake_fast_flights["seat"] == "economy"


def test_a_round_trip_adds_the_reverse_leg(fake_fast_flights):
    build_google_query(_query(return_date="2026-09-22"))

    outbound, inbound = fake_fast_flights["flights"]
    assert fake_fast_flights["trip"] == "round-trip"
    assert outbound.route == ("SFO", "JFK") and outbound.date == "2026-09-15"
    assert inbound.route == ("JFK", "SFO") and inbound.date == "2026-09-22"


def test_currency_and_language_reach_the_query(fake_fast_flights):
    build_google_query(_query(), currency="EUR", language="fr")
    assert (fake_fast_flights["currency"], fake_fast_flights["language"]) == ("EUR", "fr")


def test_building_a_query_without_the_extra_names_the_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "fast_flights", None)
    with pytest.raises(FastFlightsUnavailable, match="provider-router\\[fast-flights\\]"):
        build_google_query(_query())
