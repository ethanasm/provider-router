"""The outcome taxonomy — the vocabulary adapters use to describe what happened.

The reusable kernel of this library is not the ordering (that is a list); it is
that every provider gets to describe success and failure in *the same terms*,
so the router can act on them without knowing anything about the domain.

Real providers signal the same condition in incompatible ways: an HTTP 429 with
a ``Retry-After`` header, a 200 response whose payload says "rate limit
exceeded", an unparseable block page, or a 200 that is simply missing the field
you needed. An adapter's job is to translate its provider's dialect into the
types below; the router's job is everything downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Failure",
    "FailureKind",
    "Outcome",
    "budget_exhausted",
    "rate_limited",
    "terminal",
    "transient",
]


class Outcome(str, Enum):
    """How good a *successful* result is.

    ``DEGRADED`` exists because "the call returned 200" is not the same as "the
    provider answered well". Two real examples this was built from: a flight
    search whose result set silently omits whole carriers because an internal
    coverage query failed mid-union, and a geocoder returning 200 with no
    latitude/longitude. Both are successes by any transport measure and useless
    to the caller.

    A ``DEGRADED`` result is *kept* — it is better than nothing — but the router
    will keep looking for an ``OK`` one from a lower-preference provider before
    settling for it.
    """

    OK = "ok"
    DEGRADED = "degraded"


class FailureKind(str, Enum):
    """Why a provider could not answer."""

    RATE_LIMITED = "rate_limited"
    """Throttled. Carries ``retry_after`` when the provider advertised one."""

    TRANSIENT = "transient"
    """Worth trying again later — 5xx, connection reset, timeout."""

    TERMINAL = "terminal"
    """This provider cannot serve this request, and retrying will not help."""

    UNSUPPORTED = "unsupported"
    """The provider cannot honor a constraint in the request.

    Distinct from ``TERMINAL`` because it is not a failure at all — it is the
    router declining to let a provider answer a *different question* than the
    one asked. A flight search that drops the caller's cabin-class constraint
    and returns economy prices has not failed; it has silently changed what the
    price is a price for, which is worse.
    """

    BUDGET = "budget"
    """A spend ceiling or circuit breaker tripped.

    Route-terminal: the router aborts the whole invocation rather than failing
    over. Failing over here would spend *more* against the very ceiling that
    just tripped — a self-amplifying failure.
    """


@dataclass(frozen=True, slots=True)
class Failure:
    """A normalized provider failure."""

    kind: FailureKind
    message: str = ""
    retry_after: float | None = None
    """Seconds the provider asked us to wait. Only meaningful for RATE_LIMITED."""

    cause: BaseException | None = None

    @property
    def is_route_terminal(self) -> bool:
        """Whether this failure should abort the whole route, not just this provider."""
        return self.kind is FailureKind.BUDGET

    def __str__(self) -> str:  # pragma: no cover - trivial
        detail = f": {self.message}" if self.message else ""
        return f"{self.kind.value}{detail}"


def rate_limited(
    message: str = "", retry_after: float | None = None, cause: BaseException | None = None
) -> Failure:
    """Throttled, optionally with a server-advised wait in seconds."""
    return Failure(FailureKind.RATE_LIMITED, message, retry_after, cause)


def transient(message: str = "", cause: BaseException | None = None) -> Failure:
    """A blip worth retrying against another provider."""
    return Failure(FailureKind.TRANSIENT, message, None, cause)


def terminal(message: str = "", cause: BaseException | None = None) -> Failure:
    """A permanent failure for this provider."""
    return Failure(FailureKind.TERMINAL, message, None, cause)


def budget_exhausted(message: str = "", cause: BaseException | None = None) -> Failure:
    """A spend ceiling tripped — aborts the route rather than failing over."""
    return Failure(FailureKind.BUDGET, message, None, cause)
