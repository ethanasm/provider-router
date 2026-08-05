# provider-router

Route one capability across interchangeable providers: preferred ordering,
automatic failover on rate limits and transient errors, and a single normalized
result contract.

Zero runtime dependencies. No Redis, no database, no HTTP client.

```bash
pip install provider-router
```

## The problem

You have three ways to get the same thing. Say flight prices: one public API,
one that needs no key, one that is a scraper. They return different shapes, they
fail in different ways, and the good one starts returning `429` on a Tuesday.

So you write the fallback by hand. Then you write it again in the worker,
slightly differently. Then a fourth source shows up.

The failover part is easy — it's a list. The part that isn't easy is that every
provider says "I'm rate limited" in a different language:

| Provider | How it says *rate limited* |
|:---|:---|
| One API | HTTP 429 with a `Retry-After` header |
| Another | HTTP 200, and the payload says `"rate limit exceeded"` |
| A scraper | an unparseable block page |
| A geocoder | HTTP 200, and the field you needed is just missing |

A router can't parse those. Your adapter can. That translation — provider dialect
into one shared vocabulary — is what this library is actually for.

## Quickstart

```python
import asyncio
from provider_router import (
    Attempt,
    Deadline,
    Outcome,
    Router,
    rate_limited,
    terminal,
    transient,
)


class PrimaryAPI:
    name = "primary"

    def supports(self, request) -> bool:
        return True

    async def invoke(self, request, deadline: Deadline):
        return await fetch_from_primary(request, timeout=deadline.remaining())

    def classify(self, exc: BaseException):
        if isinstance(exc, HTTPError) and exc.status == 429:
            return rate_limited("throttled", retry_after=exc.headers.get("retry-after"))
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return transient(str(exc))
        return terminal(str(exc))

    def assess(self, result, attempt: Attempt) -> Outcome:
        return Outcome.OK if result.items else Outcome.DEGRADED


router = Router([PrimaryAPI(), BackupAPI(), LastResort()])

result = await router.invoke(query, timeout=20)

result.value  # your response type, unchanged
result.provider  # who actually answered
result.degraded  # whether the answer was thin
result.failed_over  # whether someone ahead of them was tried first
```

The router is generic over *your* request and response types. It never inspects
either. A provider can be an HTTP API, an MCP server, a scraper, a local model —
the library has no opinion.

## What a provider owes the router

Four methods. Two of them usually one-liners.

```python
def supports(self, request) -> bool         # can you honor every constraint?
async def invoke(self, request, deadline)   # do the work, or raise
def classify(self, exc) -> Failure          # what kind of failure was that?
def assess(self, result, attempt) -> Outcome  # is what you returned any good?
```

`BaseProvider` supplies defaults for `supports` and `assess`. It deliberately
does **not** supply one for `classify`.

## Design decisions, and why

**`supports()` exists so a provider never answers a different question.**
If your request asks for business class and a provider silently ignores cabin
class, it hasn't failed — it returned an economy price, and nothing downstream
can tell that apart from a good answer. Return `False` and be skipped.

**`DEGRADED` exists because "returned 200" isn't "answered well."**
Two real cases this was built from: a search whose results silently omit whole
carriers because an internal query failed mid-merge, and a geocoder returning
200 with no coordinates. Both succeed by every transport measure. A degraded
result is *kept* — thin beats nothing — but the router keeps looking for a good
one from a lower-preference provider before settling, and tells you which you got.

**Budget failures abort the route instead of failing over.**
If a spend ceiling or breaker trips, trying the next provider spends *more*
against the ceiling that just tripped. That's self-amplifying, so it raises
`RouteAborted` immediately.

**Adapters own retry; the router owns failover.**
Without that split they nest and multiply — an adapter retrying 3× inside a
router trying 3 providers is 9 upstream calls. `invoke` receives a `Deadline` so
adapters can bound themselves.

**Failover happens at the whole-invocation boundary, never mid-call.**
If your adapter paginates or fans out internally, that's one invocation it owns.
A router that could resume someone else's half-finished pagination would have to
understand the payload — and then it wouldn't be domain-agnostic.

**The answering provider is always reported.**
This one is load-bearing. If you compare results over time — a price series, any
threshold alert — a silent provider change reads as a real change in the data.
Provider A's price replaced by provider B's looks exactly like a price drop.

**A cooldown is never waited on.**
A provider asking for ten minutes shouldn't block a worker. The circuit stays
open for the full window, but the router doesn't sleep through it — it skips
that provider and tries the next one. If they're all cooling down you get
`AllProvidersFailed` immediately, and your orchestrator can reschedule, rather
than a request hanging for ten minutes.

## Circuit breaker

Per provider, in-process by default:

```python
from provider_router import BreakerConfig, Router

router = Router(
    [primary, backup],
    breaker_config=BreakerConfig(failure_threshold=3, base_cooldown=5.0),
    provider_configs={
        # Nominatim's usage policy is one request per second.
        "nominatim": BreakerConfig(min_interval=1.1),
    },
)
```

An advertised `Retry-After` opens the circuit immediately for exactly that long —
a provider that tells you its own limit is believed the first time. Otherwise the
circuit opens after `failure_threshold` consecutive failures, with exponential
backoff per re-open, and one half-open probe on expiry.

State lives behind a `BreakerStore` protocol. The default is process-local;
implement `get`/`set` against a shared store to coordinate several processes.

## Testing your adapters

The contract test is reusable — point it at your own adapter:

```python
from provider_router.conformance import assert_provider_contract
from provider_router import FailureKind


def test_my_adapter_contract():
    assert_provider_contract(
        MyAdapter(),
        sample_request=Query("SFO", "JFK"),
        sample_result=Results(items=[...]),
        expected_classifications={
            HTTPError(429): FailureKind.RATE_LIMITED,
            ConnectionResetError(): FailureKind.TRANSIENT,
            ValueError("bad input"): FailureKind.TERMINAL,
        },
    )
```

It catches the failures that produce routing bugs which look like provider bugs:
`classify` raising or returning `None`, `supports` throwing, a rate limit
reported as terminal (so the breaker never opens and you hammer a throttled
provider forever).

For your own tests, `ManualClock` makes breaker cooldowns instant and
deterministic — no `sleep`, no flake:

```python
from provider_router import ManualClock, Router

clock = ManualClock()
router = Router([a, b], clock=clock)
await router.invoke(query)
clock.advance(31)  # skip a 30-second cooldown
```

## Observability

Events are a plain callback — no logging framework imposed. A sink that raises
can never fail a route.

```python
router = Router([a, b], events=lambda e: logger.info(e.name, extra=e.fields))
```

Names are a stable contract: `router.route.selected`, `router.failover.triggered`,
`router.provider.circuit_open`, `router.provider.skipped`, `router.route.aborted`,
and others on `EventName`.

## Status

`0.1.0` — the routing core, and the API is not frozen yet.

Working: ordered failover, the outcome taxonomy, circuit breaker with
`Retry-After` cooldowns and half-open probes, per-provider pacing, deadlines,
degraded-result preference, route-terminal budget failures, events, and the
conformance suite.

Planned: a flight-search adapter pack (three real keyless sources — the code
this was extracted from), and a budget-governor hook.

Idempotency is deliberately not addressed: failover re-issues a request against
a different provider, which is safe for reads and *not* safe for writes. This
library is built for read paths.

## License

MIT
