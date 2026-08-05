"""Per-provider circuit breaker and outbound pacing.

This is the *negative* cache — the only caching this library does. Remembering
"Skiplagged asked for 90 seconds, don't call it again until then" is failover
state. Caching successful responses is the application's business and is
deliberately out of scope.

State lives behind :class:`BreakerStore` so it can be process-local (the
default, zero infrastructure) or shared across processes later. No Redis, no
database, no import-time dependency on either.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

from .clock import Clock

__all__ = ["Breaker", "BreakerConfig", "BreakerState", "BreakerStore", "InMemoryBreakerStore"]


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    """Tuning for one provider's breaker and pacer."""

    failure_threshold: int = 3
    """Consecutive transient failures before the circuit opens."""

    base_cooldown: float = 5.0
    """Cooldown for the first open. Doubles per consecutive open."""

    max_cooldown: float = 300.0

    min_interval: float = 0.0
    """Minimum seconds between calls to this provider (politeness pacing).

    Some public endpoints require it — Nominatim's usage policy is one call per
    second — and a router that fans out across providers is exactly the thing
    likely to breach it.
    """


@dataclass(frozen=True, slots=True)
class BreakerState:
    """Immutable snapshot of one provider's health."""

    consecutive_failures: int = 0
    consecutive_opens: int = 0
    open_until: float = 0.0
    last_call_at: float = float("-inf")
    probing: bool = False
    """True when the circuit has re-closed for a single trial call."""

    def is_open(self, now: float) -> bool:
        return now < self.open_until


class BreakerStore(Protocol):
    """Where breaker state lives. Swap for a shared store to coordinate processes."""

    def get(self, provider: str) -> BreakerState: ...

    def set(self, provider: str, state: BreakerState) -> None: ...


@dataclass(slots=True)
class InMemoryBreakerStore:
    """Process-local state. The default, and enough for a single-process app."""

    _states: dict[str, BreakerState] = field(default_factory=dict)

    def get(self, provider: str) -> BreakerState:
        return self._states.get(provider, BreakerState())

    def set(self, provider: str, state: BreakerState) -> None:
        self._states[provider] = state

    def clear(self) -> None:
        self._states.clear()


class Breaker:
    """Decides whether a provider may be called, and records how it went."""

    __slots__ = ("_clock", "_configs", "_default", "_store")

    def __init__(
        self,
        store: BreakerStore,
        clock: Clock,
        configs: dict[str, BreakerConfig] | None = None,
        default: BreakerConfig | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._configs = configs or {}
        self._default = default or BreakerConfig()

    def config_for(self, provider: str) -> BreakerConfig:
        return self._configs.get(provider, self._default)

    def state_for(self, provider: str) -> BreakerState:
        return self._store.get(provider)

    def skip_reason(self, provider: str) -> str | None:
        """``None`` if the provider may be called, else why it is being skipped."""
        state = self._store.get(provider)
        now = self._clock.monotonic()
        if state.is_open(now):
            return f"circuit open for {state.open_until - now:.1f}s"
        return None

    def pace_delay(self, provider: str) -> float:
        """Seconds to wait before calling, to respect ``min_interval``."""
        config = self.config_for(provider)
        if config.min_interval <= 0:
            return 0.0
        state = self._store.get(provider)
        elapsed = self._clock.monotonic() - state.last_call_at
        return max(0.0, config.min_interval - elapsed)

    def note_call(self, provider: str) -> None:
        """Record that a call is being made now (drives pacing)."""
        state = self._store.get(provider)
        self._store.set(provider, replace(state, last_call_at=self._clock.monotonic()))

    def half_open(self, provider: str) -> None:
        """Mark that this call is the trial after a cooldown expired.

        A half-open probe that fails must not be treated as one ordinary
        failure among many — it re-opens the circuit immediately, with a longer
        cooldown, rather than needing another ``failure_threshold`` failures.
        """
        state = self._store.get(provider)
        if state.consecutive_opens > 0 and not state.is_open(self._clock.monotonic()):
            self._store.set(provider, replace(state, probing=True))

    def record_success(self, provider: str) -> None:
        """A good answer closes the circuit and forgets the failure history."""
        self._store.set(
            provider,
            replace(
                self._store.get(provider),
                consecutive_failures=0,
                consecutive_opens=0,
                open_until=0.0,
                probing=False,
            ),
        )

    def record_failure(self, provider: str, retry_after: float | None = None) -> float:
        """Record a failure. Returns the cooldown applied (0.0 if still closed).

        ``retry_after`` — a provider-advertised wait — opens the circuit at once
        for exactly that long. We believe a provider that tells us its own
        limit; there is no reason to make it say so three times.
        """
        config = self.config_for(provider)
        state = self._store.get(provider)
        now = self._clock.monotonic()

        failures = state.consecutive_failures + 1
        trip = retry_after is not None or state.probing or failures >= config.failure_threshold

        if not trip:
            self._store.set(provider, replace(state, consecutive_failures=failures))
            return 0.0

        opens = state.consecutive_opens + 1
        if retry_after is not None:
            cooldown = retry_after
        else:
            cooldown = min(config.base_cooldown * (2 ** (opens - 1)), config.max_cooldown)

        self._store.set(
            provider,
            replace(
                state,
                consecutive_failures=failures,
                consecutive_opens=opens,
                open_until=now + cooldown,
                probing=False,
            ),
        )
        return cooldown
