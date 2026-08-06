"""provider-router — ordered failover across interchangeable providers.

Call one capability through several implementations, in a preferred order,
falling back automatically when one rate-limits or fails, with every result
normalized to a single contract you define.

    router = Router([primary, secondary, fallback])
    result = await router.invoke(query)
    result.value      # your own response type
    result.provider   # who actually answered
    result.degraded   # whether the answer was thin

The router is generic over your request and response types and knows nothing
about your domain. Providers can be anything — an HTTP API, an MCP server, a
scraper, a local model.
"""

from __future__ import annotations

from .breaker import (
    Breaker,
    BreakerConfig,
    BreakerState,
    BreakerStore,
    InMemoryBreakerStore,
)
from .clock import Clock, Deadline, ManualClock, SystemClock
from .conformance import ContractViolation, assert_provider_contract, check_provider_contract
from .errors import AllProvidersFailed, RouteAborted, RouterError
from .events import Event, EventName, EventSink
from .outcomes import (
    Failure,
    FailureKind,
    Outcome,
    budget_exhausted,
    rate_limited,
    terminal,
    transient,
)
from .provider import Attempt, BaseProvider, Provider
from .router import AttemptRecord, Router, RouteResult

__version__ = "0.1.0"

__all__ = [
    "AllProvidersFailed",
    "Attempt",
    "AttemptRecord",
    "BaseProvider",
    # breaker
    "Breaker",
    "BreakerConfig",
    "BreakerState",
    "BreakerStore",
    # time
    "Clock",
    "ContractViolation",
    "Deadline",
    # events
    "Event",
    "EventName",
    "EventSink",
    "Failure",
    "FailureKind",
    "InMemoryBreakerStore",
    "ManualClock",
    # outcomes
    "Outcome",
    "Provider",
    "RouteAborted",
    "RouteResult",
    # core
    "Router",
    # errors
    "RouterError",
    "SystemClock",
    "__version__",
    "assert_provider_contract",
    "budget_exhausted",
    # conformance
    "check_provider_contract",
    "rate_limited",
    "terminal",
    "transient",
]
