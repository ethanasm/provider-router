"""Exceptions raised by the router itself.

Both carry the full attempt list. When a route fails you need to know what each
provider did — "everything failed" without the per-provider reasons is the
error message that makes people take routers out again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .outcomes import Failure

if TYPE_CHECKING:  # pragma: no cover
    from .router import AttemptRecord

__all__ = ["AllProvidersFailed", "RouteAborted", "RouterError"]


class RouterError(Exception):
    """Base for routing failures."""


class AllProvidersFailed(RouterError):
    """Every provider was skipped or failed."""

    def __init__(self, attempts: tuple[AttemptRecord, ...]) -> None:
        self.attempts = attempts
        super().__init__(self._describe())

    def _describe(self) -> str:
        if not self.attempts:
            return "no providers were attempted"
        parts = []
        for a in self.attempts:
            if a.skipped is not None:
                parts.append(f"{a.provider}: skipped ({a.skipped})")
            elif a.failure is not None:
                parts.append(f"{a.provider}: {a.failure}")
            else:  # pragma: no cover - defensive
                parts.append(f"{a.provider}: unknown")
        return "all providers failed — " + "; ".join(parts)


class RouteAborted(RouterError):
    """A route-terminal failure stopped the attempt before other providers were tried.

    Raised for spend ceilings and circuit breakers, where trying the next
    provider would make the situation worse rather than better.
    """

    def __init__(self, failure: Failure, attempts: tuple[AttemptRecord, ...]) -> None:
        self.failure = failure
        self.attempts = attempts
        super().__init__(f"route aborted by {failure}")
