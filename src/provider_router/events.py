"""Structured events.

Stable event names, because the point of a router is that you can no longer
tell from the outside which provider served you — so the log has to say.
Emission is a plain callback: no logging framework is imposed, and a listener
that raises is never allowed to break a route.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Event", "EventName", "EventSink", "emit"]


class EventName:
    """The catalog. These strings are a public contract — treat renames as breaking."""

    ROUTE_STARTED = "router.route.started"
    ROUTE_SELECTED = "router.route.selected"
    ROUTE_EXHAUSTED = "router.route.exhausted"
    ROUTE_ABORTED = "router.route.aborted"
    PROVIDER_SKIPPED = "router.provider.skipped"
    PROVIDER_FAILED = "router.provider.failed"
    PROVIDER_DEGRADED = "router.provider.degraded"
    FAILOVER_TRIGGERED = "router.failover.triggered"
    CIRCUIT_OPENED = "router.provider.circuit_open"
    CIRCUIT_PROBED = "router.provider.circuit_probe"
    PACED = "router.provider.paced"


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    provider: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[Event], None]


def emit(sink: EventSink | None, name: str, provider: str | None = None, **fields: Any) -> None:
    """Fire an event, swallowing listener errors.

    Observability must not be able to fail a request that would otherwise have
    succeeded.
    """
    if sink is None:
        return
    with contextlib.suppress(Exception):
        # A broken listener is not the caller's problem.
        sink(Event(name, provider, fields))
