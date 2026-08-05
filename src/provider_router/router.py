"""The router: ordered preference, automatic failover, one normalized result.

Semantics, in one place, because every one of these was a decision:

* **Order is preference.** Providers are tried in the order given. There is no
  hidden scoring; if you want a different order, pass a different list.
* **Failover happens at the whole-invocation boundary**, never mid-call. If a
  provider paginates or fans out internally, that is one invocation it owns. A
  router that could resume someone else's half-finished pagination would have
  to understand the payload, and then it would not be domain-agnostic.
* **A ``DEGRADED`` result is kept but not settled for.** The router holds it and
  keeps trying lower-preference providers for an ``OK`` one. If none appears,
  the best degraded result is returned rather than an error — thin beats
  nothing, and the caller is told which it got.
* **``BUDGET`` aborts the route.** Failing over after a spend ceiling trips
  would spend more against the ceiling that just tripped.
* **The answering provider is always reported.** Callers that compare results
  across time — price history, anything with a threshold — need to know the
  source changed, or a failover reads as a real change in the data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic

from .breaker import Breaker, BreakerConfig, BreakerStore, InMemoryBreakerStore
from .clock import Clock, Deadline, SystemClock
from .errors import AllProvidersFailed, RouteAborted
from .events import EventName, EventSink, emit
from .outcomes import Failure, FailureKind, Outcome, terminal
from .provider import Attempt, Provider, Req, Res

__all__ = ["AttemptRecord", "RouteResult", "Router"]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """What happened with one provider during one route."""

    provider: str
    outcome: Outcome | None = None
    failure: Failure | None = None
    skipped: str | None = None
    elapsed: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.outcome is not None


@dataclass(frozen=True, slots=True)
class RouteResult(Generic[Res]):
    """A result plus the provenance the caller needs to interpret it."""

    value: Res
    provider: str
    outcome: Outcome
    attempts: tuple[AttemptRecord, ...] = ()

    @property
    def degraded(self) -> bool:
        return self.outcome is Outcome.DEGRADED

    @property
    def failed_over(self) -> bool:
        """Whether a provider ahead of this one was tried and did not serve it."""
        return any(a.provider != self.provider for a in self.attempts)


class Router(Generic[Req, Res]):
    """Calls one capability through several interchangeable providers."""

    def __init__(
        self,
        providers: Sequence[Provider[Req, Res]],
        *,
        clock: Clock | None = None,
        store: BreakerStore | None = None,
        breaker_config: BreakerConfig | None = None,
        provider_configs: dict[str, BreakerConfig] | None = None,
        events: EventSink | None = None,
        default_timeout: float | None = None,
    ) -> None:
        if not providers:
            raise ValueError("Router needs at least one provider")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ValueError(f"provider names must be unique, got {names}")

        self.providers = tuple(providers)
        self._clock = clock or SystemClock()
        self._breaker = Breaker(
            store or InMemoryBreakerStore(), self._clock, provider_configs, breaker_config
        )
        self._events = events
        self._default_timeout = default_timeout

    @property
    def breaker(self) -> Breaker:
        """Exposed for health endpoints and tests; not needed for normal use."""
        return self._breaker

    async def invoke(
        self,
        request: Req,
        *,
        deadline: Deadline | None = None,
        timeout: float | None = None,
    ) -> RouteResult[Res]:
        """Route ``request`` to the first provider that answers well.

        Raises :class:`AllProvidersFailed` if none produced a result, or
        :class:`RouteAborted` if a route-terminal failure (a spend ceiling)
        stopped the attempt early.
        """
        deadline = self._resolve_deadline(deadline, timeout)
        attempts: list[AttemptRecord] = []
        best: tuple[Res, str] | None = None

        emit(self._events, EventName.ROUTE_STARTED, providers=len(self.providers))

        for provider in self.providers:
            if deadline is not None and deadline.expired():
                attempts.append(AttemptRecord(provider.name, skipped="deadline expired"))
                emit(
                    self._events,
                    EventName.PROVIDER_SKIPPED,
                    provider.name,
                    reason="deadline_expired",
                )
                continue

            skip = self._pre_flight_skip(provider, request)
            if skip is not None:
                attempts.append(AttemptRecord(provider.name, skipped=skip))
                continue

            if attempts:
                emit(
                    self._events,
                    EventName.FAILOVER_TRIGGERED,
                    provider.name,
                    after=attempts[-1].provider,
                )

            record, value = await self._try(provider, request, deadline)
            attempts.append(record)

            if record.failure is not None and record.failure.is_route_terminal:
                emit(
                    self._events,
                    EventName.ROUTE_ABORTED,
                    provider.name,
                    reason=record.failure.kind.value,
                )
                raise RouteAborted(record.failure, tuple(attempts))

            if record.outcome is Outcome.OK:
                emit(self._events, EventName.ROUTE_SELECTED, provider.name, outcome="ok")
                return RouteResult(value, provider.name, Outcome.OK, tuple(attempts))  # type: ignore[arg-type]

            if record.outcome is Outcome.DEGRADED and best is None:
                # Hold it, but keep looking for something better.
                best = (value, provider.name)  # type: ignore[assignment]
                emit(self._events, EventName.PROVIDER_DEGRADED, provider.name)

        if best is not None:
            value, name = best
            emit(self._events, EventName.ROUTE_SELECTED, name, outcome="degraded")
            return RouteResult(value, name, Outcome.DEGRADED, tuple(attempts))

        emit(self._events, EventName.ROUTE_EXHAUSTED, attempted=len(attempts))
        raise AllProvidersFailed(tuple(attempts))

    # ------------------------------------------------------------------ internals

    def _resolve_deadline(
        self, deadline: Deadline | None, timeout: float | None
    ) -> Deadline | None:
        if deadline is not None:
            return deadline
        seconds = timeout if timeout is not None else self._default_timeout
        if seconds is None:
            return None
        return Deadline.in_seconds(seconds, self._clock)

    def _pre_flight_skip(self, provider: Provider[Req, Res], request: Req) -> str | None:
        """Reasons not to call a provider at all, checked before any I/O."""
        if not provider.supports(request):
            emit(self._events, EventName.PROVIDER_SKIPPED, provider.name, reason="unsupported")
            return "unsupported request"

        open_reason = self._breaker.skip_reason(provider.name)
        if open_reason is not None:
            emit(self._events, EventName.PROVIDER_SKIPPED, provider.name, reason="circuit_open")
            return open_reason
        return None

    async def _try(
        self, provider: Provider[Req, Res], request: Req, deadline: Deadline | None
    ) -> tuple[AttemptRecord, Res | None]:
        """Call one provider, translating whatever happens into an AttemptRecord."""
        delay = self._breaker.pace_delay(provider.name)
        if delay > 0:
            if deadline is not None and delay > deadline.remaining():
                emit(
                    self._events,
                    EventName.PROVIDER_SKIPPED,
                    provider.name,
                    reason="pace_exceeds_deadline",
                )
                return AttemptRecord(provider.name, skipped="pacing exceeds deadline"), None
            emit(self._events, EventName.PACED, provider.name, delay=delay)
            await self._clock.sleep(delay)

        self._breaker.half_open(provider.name)
        self._breaker.note_call(provider.name)
        started = self._clock.monotonic()

        call_deadline = deadline or Deadline.in_seconds(float("inf"), self._clock)
        try:
            value = await provider.invoke(request, call_deadline)
        except BaseException as exc:
            elapsed = self._clock.monotonic() - started
            failure = self._classify(provider, exc)
            if failure.is_route_terminal:
                return AttemptRecord(provider.name, failure=failure, elapsed=elapsed), None
            self._record_failure(provider.name, failure)
            emit(
                self._events,
                EventName.PROVIDER_FAILED,
                provider.name,
                kind=failure.kind.value,
                elapsed=elapsed,
            )
            return AttemptRecord(provider.name, failure=failure, elapsed=elapsed), None

        elapsed = self._clock.monotonic() - started
        attempt = Attempt(provider.name, elapsed)
        outcome = provider.assess(value, attempt)
        self._breaker.record_success(provider.name)
        return AttemptRecord(provider.name, outcome=outcome, elapsed=elapsed), value

    def _classify(self, provider: Provider[Req, Res], exc: BaseException) -> Failure:
        """Ask the adapter what went wrong; a broken ``classify`` must not mask the cause."""
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise exc
        try:
            return provider.classify(exc)
        except Exception:
            return terminal(f"{provider.name}.classify raised on {type(exc).__name__}", cause=exc)

    def _record_failure(self, name: str, failure: Failure) -> None:
        retry_after = failure.retry_after if failure.kind is FailureKind.RATE_LIMITED else None
        cooldown = self._breaker.record_failure(name, retry_after)
        if cooldown > 0:
            emit(
                self._events,
                EventName.CIRCUIT_OPENED,
                name,
                cooldown=cooldown,
                kind=failure.kind.value,
            )
