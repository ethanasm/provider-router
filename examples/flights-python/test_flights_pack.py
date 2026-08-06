"""The flights pack's contract: what a query loses, and who has to prove it."""

from __future__ import annotations

import pytest
from provider_router import ContractViolation

from flights import (
    Cabin,
    FlightCapabilities,
    FlightOffer,
    FlightQuery,
    FlightResults,
    Stops,
    assert_capability_evidence,
    check_capability_evidence,
)

FULL = FlightCapabilities(cabin=True, airlines=True, paginates=True)


def _query(**kw) -> FlightQuery:
    base = {"origin": "SFO", "destination": "JFK", "departure_date": "2026-09-15"}
    return FlightQuery(**{**base, **kw})


def test_a_fully_capable_provider_loses_nothing():
    q = _query(cabin=Cabin.BUSINESS, stops=Stops.NONSTOP, exclude_airlines=("NK",))
    assert q.unsupported_by(FULL) == ()


def test_each_unsupported_constraint_is_named():
    q = _query(cabin=Cabin.FIRST, include_airlines=("AS",), stops=Stops.NONSTOP)
    caps = FlightCapabilities(cabin=False, airlines=False, paginates=False, max_stops=False)
    assert set(q.unsupported_by(caps)) == {"cabin", "airlines", "stops"}


def test_a_constraint_not_asked_for_is_never_reported():
    """Only what the caller actually requested can be lost."""
    caps = FlightCapabilities(cabin=False, airlines=False, paginates=False, max_stops=False)
    assert _query().unsupported_by(caps) == ()


def test_round_trip_is_derived_from_the_return_date():
    assert not _query().round_trip
    assert _query(return_date="2026-09-22").round_trip


def test_declaring_a_capability_unsupported_requires_evidence():
    """The lesson this pack exists to encode.

    An adapter author reading their own function signature — rather than the
    provider's schema — is how a supported capability gets recorded as absent,
    and the symptom of getting it wrong is silence.
    """
    unevidenced = FlightCapabilities(cabin=False, airlines=True, paginates=True)
    problems = check_capability_evidence(unevidenced, "someprovider")

    assert len(problems) == 1
    assert "cabin" in problems[0]
    assert "evidence" in problems[0]


def test_evidence_satisfies_the_check():
    evidenced = FlightCapabilities(
        cabin=False,
        airlines=True,
        paginates=True,
        evidence="tools/list 2026-08-06: search-flight has no cabin parameter",
    )
    assert check_capability_evidence(evidenced, "someprovider") == []


def test_blank_evidence_does_not_count():
    caps = FlightCapabilities(cabin=False, airlines=True, paginates=True, evidence="   ")
    assert check_capability_evidence(caps, "someprovider")


def test_a_fully_capable_provider_needs_no_evidence():
    """Positive claims are self-correcting — the call fails if you're wrong."""
    assert check_capability_evidence(FULL, "someprovider") == []


def test_every_unsupported_capability_is_listed_together():
    caps = FlightCapabilities(cabin=False, airlines=False, paginates=False)
    problems = check_capability_evidence(caps, "someprovider")
    assert len(problems) == 1
    for name in ("airlines", "cabin", "paginates"):
        assert name in problems[0]


def test_assert_variant_raises():
    caps = FlightCapabilities(cabin=False, airlines=True, paginates=True)
    with pytest.raises(ContractViolation, match="evidence"):
        assert_capability_evidence(caps, "someprovider")
    assert_capability_evidence(FULL, "someprovider")  # does not raise


def test_results_report_partiality_rather_than_hiding_it():
    """A result set missing whole airlines is a success by every transport measure."""
    whole = FlightResults(
        origin="SFO", destination="JFK", departure_date="2026-09-15", provider="p"
    )
    assert not whole.partial and whole.count == 0

    thin = FlightResults(
        origin="SFO",
        destination="JFK",
        departure_date="2026-09-15",
        provider="p",
        partial=True,
        partial_reason="1 of 2 coverage queries failed",
        offers=[
            FlightOffer(
                departure_airport="SFO",
                arrival_airport="JFK",
                price_amount="145.00",
                provider="p",
            )
        ],
    )
    assert thin.partial and thin.count == 1
    assert thin.partial_reason


def test_flight_number_is_a_full_designator_by_contract():
    offer = FlightOffer(
        departure_airport="SFO",
        arrival_airport="JFK",
        carrier_code="AS",
        flight_number="AS3361",
        price_amount="145.00",
        provider="p",
    )
    # Rendering carrier_code + flight_number would give "AS AS3361" — the exact
    # bug the normalized contract exists to prevent.
    assert offer.flight_number.startswith(offer.carrier_code)
