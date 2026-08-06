"""Circuit breaker and pacing — the only state this library keeps."""

from __future__ import annotations

import pytest

from conftest import Boom, FakeProvider, Throttled, collector
from provider_router import (
    AllProvidersFailed,
    BreakerConfig,
    EventName,
    ManualClock,
    Router,
)


async def test_circuit_opens_after_threshold_and_skips_the_provider():
    clock = ManualClock()
    a = FakeProvider("a", script=[Boom(), Boom()], default=Boom())
    b = FakeProvider("b")
    router = Router([a, b], clock=clock, breaker_config=BreakerConfig(failure_threshold=2))

    await router.invoke("q")
    await router.invoke("q")
    calls_before = a.calls

    await router.invoke("q")
    assert a.calls == calls_before, "an open circuit means no call at all"


async def test_advertised_retry_after_opens_the_circuit_immediately():
    """A provider that tells us its own limit is believed the first time."""
    clock = ManualClock()
    a = FakeProvider("a", script=[Throttled(retry_after=30)], default="ok")
    b = FakeProvider("b")
    router = Router([a, b], clock=clock, breaker_config=BreakerConfig(failure_threshold=99))

    await router.invoke("q")
    assert router.breaker.state_for("a").open_until == pytest.approx(30.0)

    await router.invoke("q")
    assert a.calls == 1, "still cooling down"

    clock.advance(31)
    result = await router.invoke("q")
    assert a.calls == 2 and result.provider == "a", "recovers once the window passes"


async def test_success_closes_the_circuit_and_clears_history():
    clock = ManualClock()
    a = FakeProvider("a", script=[Boom()], default="ok")
    router = Router(
        [a, FakeProvider("b")], clock=clock, breaker_config=BreakerConfig(failure_threshold=2)
    )

    await router.invoke("q")
    assert router.breaker.state_for("a").consecutive_failures == 1

    await router.invoke("q")
    assert router.breaker.state_for("a").consecutive_failures == 0


async def test_failed_half_open_probe_reopens_without_waiting_for_threshold():
    """A provider that fails its trial call has not recovered — don't re-probe immediately."""
    clock = ManualClock()
    a = FakeProvider("a", default=Boom())
    router = Router(
        [a, FakeProvider("b")],
        clock=clock,
        breaker_config=BreakerConfig(failure_threshold=1, base_cooldown=10),
    )

    await router.invoke("q")
    first_open = router.breaker.state_for("a").open_until

    clock.advance(11)
    await router.invoke("q")  # the probe, which fails

    state = router.breaker.state_for("a")
    assert state.consecutive_opens == 2
    assert state.open_until > first_open + 10, "cooldown backs off after a failed probe"


async def test_cooldown_backs_off_exponentially_and_is_capped():
    clock = ManualClock()
    a = FakeProvider("a", default=Boom())
    router = Router(
        [a, FakeProvider("b")],
        clock=clock,
        breaker_config=BreakerConfig(failure_threshold=1, base_cooldown=10, max_cooldown=25),
    )

    cooldowns = []
    for _ in range(4):
        await router.invoke("q")
        state = router.breaker.state_for("a")
        cooldowns.append(state.open_until - clock.monotonic())
        clock.advance(state.open_until - clock.monotonic() + 1)

    assert cooldowns[0] == pytest.approx(10)
    assert cooldowns[1] == pytest.approx(20)
    assert cooldowns[2] == pytest.approx(25), "capped"
    assert cooldowns[3] == pytest.approx(25)


async def test_pacing_delays_the_second_call_to_a_provider():
    """Some public endpoints require it — Nominatim allows one call per second."""
    clock = ManualClock()
    a = FakeProvider("a")
    router = Router([a], clock=clock, breaker_config=BreakerConfig(min_interval=1.1))

    await router.invoke("q")
    await router.invoke("q")

    assert clock.slept == [pytest.approx(1.1)]


async def test_pacing_is_per_provider():
    clock = ManualClock()
    a, b = FakeProvider("a", script=[Boom()]), FakeProvider("b")
    router = Router([a, b], clock=clock, breaker_config=BreakerConfig(min_interval=5))

    await router.invoke("q")
    assert clock.slept == [], "b has never been called, so nothing to wait for"


async def test_per_provider_config_overrides_the_default():
    clock = ManualClock()
    a = FakeProvider("a", default=Boom())
    router = Router(
        [a, FakeProvider("b")],
        clock=clock,
        breaker_config=BreakerConfig(failure_threshold=99),
        provider_configs={"a": BreakerConfig(failure_threshold=1, base_cooldown=7)},
    )

    await router.invoke("q")
    assert router.breaker.state_for("a").open_until == pytest.approx(7.0)


async def test_circuit_open_event_carries_the_cooldown():
    clock = ManualClock()
    events, sink = collector()
    a = FakeProvider("a", script=[Throttled(retry_after=12)])
    Router([a, FakeProvider("b")], clock=clock, events=sink)
    await Router([a, FakeProvider("b")], clock=clock, events=sink).invoke("q")

    opened = next(e for e in events if e.name == EventName.CIRCUIT_OPENED)
    assert opened.provider == "a"
    assert opened.fields["cooldown"] == pytest.approx(12)


async def test_every_provider_open_exhausts_the_route():
    clock = ManualClock()
    a = FakeProvider("a", default=Boom())
    b = FakeProvider("b", default=Boom())
    router = Router([a, b], clock=clock, breaker_config=BreakerConfig(failure_threshold=1))

    with pytest.raises(AllProvidersFailed):
        await router.invoke("q")  # both fail for real, opening both circuits

    with pytest.raises(AllProvidersFailed) as excinfo:
        await router.invoke("q")  # now nothing is even called

    assert all("circuit open" in (r.skipped or "") for r in excinfo.value.attempts)
    assert a.calls == 1 and b.calls == 1
