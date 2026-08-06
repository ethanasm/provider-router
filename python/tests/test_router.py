"""Router behaviour: ordering, failover, degradation, aborts, deadlines."""

from __future__ import annotations

import pytest

from conftest import Boom, FakeProvider, Fatal, OverBudget, Throttled, collector
from provider_router import (
    AllProvidersFailed,
    BreakerConfig,
    Deadline,
    EventName,
    ManualClock,
    Outcome,
    RouteAborted,
    Router,
)


async def test_first_provider_wins_and_others_are_untouched():
    a, b = FakeProvider("a"), FakeProvider("b")
    result = await Router([a, b]).invoke("q")

    assert result.value == "ok"
    assert result.provider == "a"
    assert result.outcome is Outcome.OK
    assert b.calls == 0, "a healthy primary must not cause downstream traffic"
    assert not result.failed_over


async def test_order_is_preference_not_a_set():
    a, b = FakeProvider("a", default="from-a"), FakeProvider("b", default="from-b")
    assert (await Router([b, a]).invoke("q")).provider == "b"
    assert (await Router([a, b]).invoke("q")).provider == "a"


async def test_fails_over_on_transient_error():
    a = FakeProvider("a", script=[Boom()])
    b = FakeProvider("b", default="rescued")
    result = await Router([a, b]).invoke("q")

    assert result.value == "rescued"
    assert result.provider == "b"
    assert result.failed_over
    assert result.attempts[0].failure is not None
    assert result.attempts[0].provider == "a"


async def test_fails_over_on_rate_limit():
    a = FakeProvider("a", script=[Throttled(retry_after=30)])
    b = FakeProvider("b")
    result = await Router([a, b], clock=ManualClock()).invoke("q")
    assert result.provider == "b"


async def test_unsupported_provider_is_skipped_not_called():
    """The whole reason `supports` exists: never let a provider answer a different question."""
    a = FakeProvider("a", supports_request=False)
    b = FakeProvider("b")
    result = await Router([a, b]).invoke("q")

    assert result.provider == "b"
    assert a.calls == 0
    assert result.attempts[0].skipped == "unsupported request"


async def test_budget_failure_aborts_route_instead_of_failing_over():
    """Failing over after a spend ceiling trips would spend more against that ceiling."""
    a = FakeProvider("a", script=[OverBudget()])
    b = FakeProvider("b")

    with pytest.raises(RouteAborted) as excinfo:
        await Router([a, b]).invoke("q")

    assert b.calls == 0, "a tripped ceiling must not cascade into more spend"
    assert excinfo.value.failure.is_route_terminal


async def test_all_failing_raises_with_per_provider_detail():
    a = FakeProvider("a", script=[Boom()])
    b = FakeProvider("b", script=[Fatal("nope")])

    with pytest.raises(AllProvidersFailed) as excinfo:
        await Router([a, b]).invoke("q")

    message = str(excinfo.value)
    assert "a: transient" in message
    assert "b: terminal: nope" in message
    assert len(excinfo.value.attempts) == 2


async def test_degraded_result_is_held_while_better_is_sought():
    a = FakeProvider("a", default="thin", degraded=True)
    b = FakeProvider("b", default="rich")
    result = await Router([a, b]).invoke("q")

    assert result.value == "rich"
    assert result.provider == "b"
    assert result.outcome is Outcome.OK


async def test_degraded_result_is_returned_when_nothing_better_exists():
    """Thin beats nothing — but the caller is told it was thin."""
    a = FakeProvider("a", default="thin", degraded=True)
    b = FakeProvider("b", script=[Boom()])
    result = await Router([a, b]).invoke("q")

    assert result.value == "thin"
    assert result.provider == "a"
    assert result.degraded


async def test_first_degraded_wins_over_later_degraded():
    a = FakeProvider("a", default="thin-a", degraded=True)
    b = FakeProvider("b", default="thin-b", degraded=True)
    result = await Router([a, b]).invoke("q")
    assert result.value == "thin-a", "order is preference for degraded results too"


async def test_result_reports_the_answering_provider():
    """Callers comparing results over time must see the source change.

    This is the API that keeps a failover from reading as a real change in the
    underlying data — a price series whose provider silently changed will
    otherwise show a phantom drop.
    """
    a = FakeProvider("a", script=[Boom()])
    b = FakeProvider("b", default=42)
    result = await Router([a, b]).invoke("q")

    assert result.provider == "b"
    assert [r.provider for r in result.attempts] == ["a", "b"]


async def test_deadline_stops_further_providers():
    clock = ManualClock()
    a = FakeProvider("a", script=[Boom()])
    b = FakeProvider("b")
    deadline = Deadline.in_seconds(0, clock)

    with pytest.raises(AllProvidersFailed) as excinfo:
        await Router([a, b], clock=clock).invoke("q", deadline=deadline)

    assert all(r.skipped == "deadline expired" for r in excinfo.value.attempts)
    assert a.calls == 0 and b.calls == 0


async def test_deadline_is_passed_down_to_providers():
    """Adapters own transport retry, so they need to know when to stop."""
    clock = ManualClock()
    a = FakeProvider("a")
    await Router([a], clock=clock).invoke("q", timeout=5)

    assert a.seen_deadlines[0].remaining() == pytest.approx(5.0)


async def test_broken_classify_does_not_mask_the_original_error():
    class BadClassifier(FakeProvider):
        def classify(self, exc: BaseException):
            raise RuntimeError("classifier is broken")

    a = BadClassifier("a", script=[Boom()])
    b = FakeProvider("b")
    result = await Router([a, b]).invoke("q")

    assert result.provider == "b"
    failure = result.attempts[0].failure
    assert failure is not None and isinstance(failure.cause, Boom)


async def test_events_name_the_provider_that_served():
    events, sink = collector()
    a = FakeProvider("a", script=[Boom()])
    b = FakeProvider("b")
    await Router([a, b], events=sink).invoke("q")

    names = [e.name for e in events]
    assert EventName.FAILOVER_TRIGGERED in names
    selected = next(e for e in events if e.name == EventName.ROUTE_SELECTED)
    assert selected.provider == "b"


async def test_a_broken_event_sink_cannot_fail_a_route():
    def explode(event):
        raise RuntimeError("logging is down")

    result = await Router([FakeProvider("a")], events=explode).invoke("q")
    assert result.provider == "a"


def test_router_rejects_empty_and_duplicate_providers():
    with pytest.raises(ValueError, match="at least one provider"):
        Router([])
    with pytest.raises(ValueError, match="unique"):
        Router([FakeProvider("dup"), FakeProvider("dup")])


async def test_default_timeout_applies_when_no_deadline_given():
    clock = ManualClock()
    a = FakeProvider("a")
    await Router([a], clock=clock, default_timeout=3).invoke("q")
    assert a.seen_deadlines[0].remaining() == pytest.approx(3.0)


async def test_pacing_is_skipped_when_it_would_blow_the_deadline():
    clock = ManualClock()
    config = BreakerConfig(min_interval=10.0)
    a = FakeProvider("a")
    b = FakeProvider("b")
    router = Router([a, b], clock=clock, breaker_config=config)

    await router.invoke("q")  # primes a's last-call time
    result = await router.invoke("q", timeout=1)

    assert a.calls == 1, "pacing must not be honored past the caller's deadline"
    assert result.provider == "b"
