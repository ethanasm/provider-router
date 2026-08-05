"""Clock, deadline, store, and result-shape primitives."""

from __future__ import annotations

import pytest

from provider_router import (
    BreakerState,
    Deadline,
    Failure,
    FailureKind,
    InMemoryBreakerStore,
    Outcome,
    SystemClock,
    budget_exhausted,
    rate_limited,
    terminal,
    transient,
)


async def test_system_clock_advances_and_sleeps():
    clock = SystemClock()
    before = clock.monotonic()
    await clock.sleep(0.01)
    await clock.sleep(0)  # a zero sleep must be a no-op, not a scheduler round-trip
    assert clock.monotonic() >= before


def test_deadline_floors_at_zero_and_reports_expiry():
    from provider_router import ManualClock

    clock = ManualClock()
    deadline = Deadline.in_seconds(5, clock)

    assert deadline.remaining() == pytest.approx(5)
    assert not deadline.expired()

    clock.advance(10)
    assert deadline.remaining() == 0.0, "remaining never goes negative"
    assert deadline.expired()


def test_in_memory_store_defaults_and_clears():
    store = InMemoryBreakerStore()
    assert store.get("unseen") == BreakerState()

    store.set("a", BreakerState(consecutive_failures=2))
    assert store.get("a").consecutive_failures == 2

    store.clear()
    assert store.get("a") == BreakerState()


def test_only_budget_failures_are_route_terminal():
    assert budget_exhausted().is_route_terminal
    assert not rate_limited().is_route_terminal
    assert not transient().is_route_terminal
    assert not terminal().is_route_terminal


def test_failure_helpers_carry_their_kind_and_cause():
    cause = ValueError("upstream")
    failure = rate_limited("slow down", retry_after=12.5, cause=cause)

    assert failure.kind is FailureKind.RATE_LIMITED
    assert failure.retry_after == 12.5
    assert failure.cause is cause
    assert isinstance(failure, Failure)


def test_outcome_and_failure_kind_are_stable_strings():
    """These leak into logs and dashboards — renaming one is a breaking change."""
    assert Outcome.OK.value == "ok"
    assert Outcome.DEGRADED.value == "degraded"
    assert FailureKind.RATE_LIMITED.value == "rate_limited"
    assert FailureKind.BUDGET.value == "budget"
    assert FailureKind.UNSUPPORTED.value == "unsupported"


def test_breaker_state_open_window_is_half_open_at_the_boundary():
    state = BreakerState(open_until=10.0)
    assert state.is_open(9.99)
    assert not state.is_open(10.0), "the cooldown expires at the boundary, not after it"
