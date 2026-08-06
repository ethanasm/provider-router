"""Shared fakes.

Everything here is deliberately trivial: the point of these tests is the
router's decisions, not a mock framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from provider_router import (
    Attempt,
    Deadline,
    Failure,
    Outcome,
    rate_limited,
    terminal,
    transient,
)


class Boom(Exception):
    """A transient upstream blip."""


class Throttled(Exception):
    """A rate limit, optionally with a server-advised wait."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("throttled")
        self.retry_after = retry_after


class Fatal(Exception):
    """A permanent failure for one provider."""


class OverBudget(Exception):
    """A spend ceiling tripped."""


@dataclass
class FakeProvider:
    """A provider whose behaviour is scripted per call."""

    name: str
    # One entry per call: an exception to raise, or a value to return.
    script: list[object] = field(default_factory=list)
    supports_request: bool = True
    degraded: bool = False
    calls: int = 0
    seen_deadlines: list[Deadline] = field(default_factory=list)
    default: object = "ok"

    def supports(self, request: object) -> bool:
        return self.supports_request

    async def invoke(self, request: object, deadline: Deadline) -> object:
        self.seen_deadlines.append(deadline)
        step = self.script[self.calls] if self.calls < len(self.script) else self.default
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        return step

    def classify(self, exc: BaseException) -> Failure:
        if isinstance(exc, Throttled):
            return rate_limited("throttled", retry_after=exc.retry_after, cause=exc)
        if isinstance(exc, Boom):
            return transient("blip", cause=exc)
        if isinstance(exc, OverBudget):
            from provider_router import budget_exhausted

            return budget_exhausted("ceiling hit", cause=exc)
        return terminal(str(exc), cause=exc)

    def assess(self, result: object, attempt: Attempt) -> Outcome:
        return Outcome.DEGRADED if self.degraded else Outcome.OK


def collector() -> tuple[list, object]:
    """An event sink plus the list it fills."""
    events: list = []
    return events, events.append
