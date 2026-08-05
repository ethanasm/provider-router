"""The provider contract.

A provider is *anything* that can service the request: an MCP server, an HTTP
API, a scraper, a local model. The router never learns which. What it needs
from each is four things — can you handle this request, do the work, tell me
what went wrong in normalized terms, and tell me whether what you returned is
actually any good.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

from .clock import Deadline
from .outcomes import Failure, FailureKind, Outcome

__all__ = ["Attempt", "BaseProvider", "Provider", "Req", "Res"]

Req = TypeVar("Req")
Res = TypeVar("Res")

# The protocol only ever *consumes* a request, so it is contravariant there: a
# provider that accepts any `SearchQuery` is usable wherever a provider of some
# narrower query type is expected. `Res` stays invariant because `invoke`
# returns it while `assess` consumes it.
Req_contra = TypeVar("Req_contra", contravariant=True)


@dataclass(frozen=True, slots=True)
class Attempt:
    """Context handed to ``assess`` so it can judge a result it cannot judge alone.

    Some degradation is invisible in the returned object. A search that unions
    several upstream queries and loses one of them returns a perfectly
    well-formed, merely *incomplete* result; the fact that it is incomplete
    lives in the attempt, not the payload. Adapters record that here.
    """

    provider: str
    elapsed: float
    notes: tuple[str, ...] = ()
    """Adapter-supplied markers, e.g. ``("partial_coverage",)``."""


@runtime_checkable
class Provider(Protocol[Req_contra, Res]):
    """One implementation of a capability."""

    name: str

    def supports(self, request: Req_contra) -> bool:
        """Whether this provider can honor *every* constraint in the request.

        Return ``False`` rather than silently dropping one. A provider that
        ignores a constraint does not fail — it answers a different question,
        and the router has no way to tell that apart from a good answer.
        """
        ...

    async def invoke(self, request: Req_contra, deadline: Deadline) -> Res:
        """Do the work, or raise.

        Bound internal retries by ``deadline``; the router will not interrupt a
        provider that overruns it, it will only decline to try the next one.
        """
        ...

    def classify(self, exc: BaseException) -> Failure:
        """Translate an exception from ``invoke`` into the shared vocabulary."""
        ...

    def assess(self, result: Res, attempt: Attempt) -> Outcome:
        """Judge a successful result. Return ``DEGRADED`` if it is thin."""
        ...


class BaseProvider(Generic[Req, Res]):
    """Optional convenience base with sane defaults.

    Implementing the ``Provider`` protocol directly is fine — this exists so an
    adapter that has nothing interesting to say about ``supports`` or ``assess``
    does not have to write them out. ``classify`` deliberately has *no* default:
    guessing that an unrecognized exception is transient is how a permanent
    misconfiguration turns into an infinite failover loop.
    """

    name: str = "unnamed"

    def supports(self, request: Req) -> bool:
        return True

    async def invoke(self, request: Req, deadline: Deadline) -> Res:
        raise NotImplementedError

    def classify(self, exc: BaseException) -> Failure:
        raise NotImplementedError(
            f"{type(self).__name__}.classify must map exceptions to a Failure; "
            "an unclassified error cannot be routed safely"
        )

    def assess(self, result: Res, attempt: Attempt) -> Outcome:
        return Outcome.DEGRADED if attempt.notes else Outcome.OK

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<{type(self).__name__} {self.name!r}>"


def is_route_terminal(failure: Failure) -> bool:
    """Whether a failure aborts the whole route rather than just this provider."""
    return failure.kind is FailureKind.BUDGET
