"""Run the shared routing vectors from ``spec/vectors/routing.json``.

The TypeScript port runs this same file through an equivalent harness. That is
the only mechanism keeping two implementations of one contract honest: a change
to routing behaviour either lands in both ports or turns this red in one of
them.

The harness deliberately builds providers from a tiny script language rather
than from mocks — a vector has to mean the same thing in Python and TypeScript,
and "a list of strings" survives that translation where a mock framework does
not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from provider_router import (
    AllProvidersFailed,
    Attempt,
    BreakerConfig,
    Deadline,
    Failure,
    ManualClock,
    Outcome,
    RouteAborted,
    Router,
    budget_exhausted,
    rate_limited,
    terminal,
    transient,
)

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "spec" / "vectors" / "routing.json").read_text()
)


class ScriptedError(Exception):
    def __init__(self, behaviour: str) -> None:
        super().__init__(behaviour)
        self.behaviour = behaviour


class ScriptedProvider:
    """A provider whose every call is dictated by a list of strings."""

    def __init__(self, spec: dict[str, Any], clock: ManualClock) -> None:
        self.name: str = spec["name"]
        self._script: list[str] = list(spec["script"])
        self._supports: bool = spec.get("supports", True)
        self._duration: float = spec.get("duration", 0.0)
        self._clock = clock
        self.calls = 0

    def _behaviour(self) -> str:
        # Past the end of the script the last entry repeats, so a vector only
        # has to spell out the part that varies.
        index = min(self.calls - 1, len(self._script) - 1)
        return self._script[index]

    def supports(self, request: Any) -> bool:
        return self._supports

    async def invoke(self, request: Any, deadline: Deadline) -> str:
        self.calls += 1
        if self._duration:
            self._clock.advance(self._duration)
        behaviour = self._behaviour()
        if behaviour in ("ok", "degraded"):
            return behaviour
        raise ScriptedError(behaviour)

    def classify(self, exc: BaseException) -> Failure:
        behaviour = getattr(exc, "behaviour", "terminal")
        if behaviour == "classify_throws":
            raise RuntimeError("this adapter's classify is broken")
        if behaviour == "classify_returns_nothing":
            return None  # type: ignore[return-value]
        if behaviour.startswith("rate_limited"):
            _, _, after = behaviour.partition(":")
            return rate_limited("throttled", retry_after=float(after) if after else None)
        if behaviour == "transient":
            return transient("blip")
        if behaviour == "budget":
            return budget_exhausted("daily ceiling reached")
        return terminal(behaviour)

    def assess(self, result: str, attempt: Attempt) -> Outcome:
        return Outcome.DEGRADED if result == "degraded" else Outcome.OK


def _breaker_config(spec: dict[str, Any] | None) -> BreakerConfig:
    spec = spec or {}
    return BreakerConfig(
        failure_threshold=spec.get("failure_threshold", 3),
        base_cooldown=spec.get("base_cooldown", 5.0),
        max_cooldown=spec.get("max_cooldown", 300.0),
        min_interval=spec.get("min_interval", 0.0),
    )


def _describe(record: Any) -> str:
    if record.skipped is not None:
        return f"{record.provider}:skipped"
    if record.failure is not None:
        return f"{record.provider}:{record.failure.kind.value}"
    return f"{record.provider}:{record.outcome.value}"


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
async def test_shared_vector(case: dict[str, Any]) -> None:
    clock = ManualClock()
    providers = [ScriptedProvider(p, clock) for p in case["providers"]]
    cooldowns: dict[str, float] = {}

    def sink(event: Any) -> None:
        if event.name == "router.provider.circuit_open" and event.provider:
            cooldowns[event.provider] = event.fields["cooldown"]

    router: Router[str, str] = Router(
        providers,
        clock=clock,
        breaker_config=_breaker_config(case.get("breaker")),
        events=sink,
        default_timeout=case.get("timeout"),
    )

    expect = case["expect"]
    routes = case.get("routes", 1)
    advance = case.get("advance_between_routes", 0)
    result = None
    raised: str | None = None

    for index in range(routes):
        if index:
            clock.advance(advance)
        slept_before = len(clock.slept)
        try:
            result = await router.invoke("request")
            raised = None
        except AllProvidersFailed as exc:
            raised, result = "all_providers_failed", None
            attempts = exc.attempts
        except RouteAborted as exc:
            raised, result = "route_aborted", None
            attempts = exc.attempts
        else:
            attempts = result.attempts
        slept = clock.slept[slept_before:]

    assert raised == expect.get("raises"), f"expected raises={expect.get('raises')}"

    if "served_by" in expect:
        assert result is not None
        assert result.provider == expect["served_by"]
    if "outcome" in expect:
        assert result is not None
        assert result.outcome.value == expect["outcome"]
    if "failed_over" in expect:
        assert result is not None
        assert result.failed_over is expect["failed_over"]
    if "attempts" in expect:
        assert [_describe(a) for a in attempts] == expect["attempts"]
    if "calls" in expect:
        actual = {p.name: p.calls for p in providers}
        assert {k: actual[k] for k in expect["calls"]} == expect["calls"]
    if "circuit_open" in expect:
        for name, should_be_open in expect["circuit_open"].items():
            state = router.breaker.state_for(name)
            assert state.is_open(clock.monotonic()) is should_be_open, name
    if "cooldown" in expect:
        for name, seconds in expect["cooldown"].items():
            assert cooldowns.get(name) == pytest.approx(seconds), name
    if "slept" in expect:
        assert slept == pytest.approx(expect["slept"])


def test_every_case_asserts_something() -> None:
    """A vector with an empty `expect` would pass silently and prove nothing."""
    for case in VECTORS["cases"]:
        assert case["expect"], case["name"]
