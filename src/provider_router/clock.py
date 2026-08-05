"""Clock and deadline primitives.

Time is injected rather than imported so that breaker cooldowns, pacing, and
deadlines are testable without ``sleep``. Everything uses a *monotonic* clock:
wall-clock jumps (NTP, DST) must never open or close a circuit breaker.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

__all__ = ["Clock", "Deadline", "ManualClock", "SystemClock"]


class Clock(Protocol):
    """A monotonic clock the router can await against."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary fixed point. Never goes backwards."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds``."""
        ...


class SystemClock:
    """The real clock."""

    __slots__ = ()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


@dataclass(slots=True)
class ManualClock:
    """A clock tests drive by hand.

    ``sleep`` advances time instead of waiting, so a test can exercise an hour
    of breaker cooldown instantly and deterministically.
    """

    now: float = 0.0
    slept: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.slept.append(seconds)
            self.now += seconds

    def advance(self, seconds: float) -> None:
        """Move time forward without recording a sleep."""
        self.now += seconds


@dataclass(frozen=True, slots=True)
class Deadline:
    """An absolute point in monotonic time by which a route must finish.

    Passed down into ``Provider.invoke`` so adapters can bound their own
    internal retries. Adapters own transport retry; the router owns failover.
    Without a shared deadline those two nest and multiply: an adapter that
    retries three times inside a router that tries three providers is nine
    upstream calls and nine times the latency.
    """

    at: float
    clock: Clock = field(default_factory=SystemClock, repr=False, compare=False)
    """The clock this deadline is measured against.

    Carried on the deadline rather than asked of the caller: the adapter that
    receives it in ``invoke`` wants ``deadline.remaining()``, and making it
    hunt down the router's clock to answer that is friction with no upside.
    """

    @classmethod
    def in_seconds(cls, seconds: float, clock: Clock | None = None) -> Deadline:
        clock = clock or SystemClock()
        return cls(clock.monotonic() + seconds, clock)

    def remaining(self) -> float:
        """Seconds left, floored at zero."""
        return max(0.0, self.at - self.clock.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.0
