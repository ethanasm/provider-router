# provider-router

Route one capability across interchangeable providers: preferred ordering,
automatic failover on rate limits and transient errors, and a single normalized
result contract.

Zero runtime dependencies. No Redis, no database, no HTTP client.

```bash
pip install provider-router
```

A TypeScript port with identical semantics ships as
[`@ethanasm/provider-router`](https://www.npmjs.com/package/@ethanasm/provider-router).
Both are driven by the same shared test vectors, so they cannot quietly drift
apart.

## The problem

You have three ways to get the same thing — one public API, one that needs no
key, one that is a scraper. They return different shapes, they fail in different
ways, and the good one starts returning `429` on a Tuesday.

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

A router can't parse those. Your adapter can. That translation — provider
dialect into one shared vocabulary — is what this library is actually for.

## Adapters are your code

This package ships no adapters, and that is deliberate. An adapter encodes
*your* providers, *your* failure dialects, and *your* normalization choices;
shipping a catalog of them would mean a dependency per provider and a library
that ages at the speed of other people's APIs.

What ships instead is the contract they implement, the router that drives them,
and a conformance test that holds them honest. Adapters live in your app,
usually 100–200 lines each.

Reference implementations against three real keyless providers live in
[`examples/`](https://github.com/ethanasm/provider-router/tree/main/examples) —
copy them, don't install them.

## Quickstart

```python
import asyncio
from provider_router import (
    Attempt,
    Deadline,
    FailureKind,
    Outcome,
    Router,
    rate_limited,
    terminal,
    transient,
)


class Nominatim:
    name = "nominatim"

    def supports(self, request) -> bool:
        return True

    async def invoke(self, request, deadline: Deadline):
        return await geocode(request, timeout=deadline.remaining())

    def classify(self, exc: BaseException):
        if isinstance(exc, HTTPError) and exc.status == 429:
            return rate_limited(str(exc), retry_after=exc.retry_after, cause=exc)
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return transient(str(exc), cause=exc)
        return terminal(str(exc), cause=exc)

    def assess(self, result, attempt: Attempt) -> Outcome:
        # 200 with no coordinates is a success by every transport measure
        # and useless to the caller.
        return Outcome.OK if result.lat is not None else Outcome.DEGRADED


router = Router([GooglePlaces(), Nominatim()], default_timeout=20)
result = await router.invoke("1600 Amphitheatre Pkwy")

result.value  # the answer
result.provider  # who served it — callers comparing over time need this
result.degraded  # whether you got the good version
```

## What a provider owes the router

Four methods. The router never learns what the provider *is*.

| Method | Contract |
|:---|:---|
| `supports(request)` | Can you honor **every** constraint? Return `False` rather than dropping one. |
| `invoke(request, deadline)` | Do the work or raise. Bound your own retries by the deadline. |
| `classify(exc)` | Translate your exception into `RATE_LIMITED` / `TRANSIENT` / `TERMINAL` / `UNSUPPORTED` / `BUDGET`. |
| `assess(result, attempt)` | Judge your own success. `DEGRADED` if it's thin. |

`classify` deliberately has no default. Guessing that an unrecognized exception
is transient is how a permanent misconfiguration becomes an infinite failover
loop.

## Design decisions, and why

**Order is preference.** No hidden scoring. Want a different order, pass a
different list.

**Failover happens at the whole-invocation boundary**, never mid-call. A router
that could resume someone else's half-finished pagination would have to
understand the payload — and then it isn't domain-agnostic any more.

**A `DEGRADED` result is kept but not settled for.** The router holds it and
keeps trying lower-preference providers for an `OK` one. If none appears, the
degraded result is returned rather than an error — thin beats nothing, and the
caller is told which it got.

**`BUDGET` aborts the route.** Failing over after a spend ceiling trips would
spend more against the ceiling that just tripped.

**The answering provider is always reported.** Callers that compare results
over time — price history, anything with a threshold — need to know the source
changed, or a failover reads as a real change in the data.

**`supports()` returning `False` is not a failure.** It's the router refusing
to let a provider answer a *different question* than the one asked. A search
that silently drops your filter hasn't failed; it has changed what the answer
is an answer to, which is worse, because nothing downstream can tell.

## Circuit breaker

Per-provider, `Retry-After`-driven, with exponential backoff and half-open
probes.

```python
from provider_router import BreakerConfig, Router

router = Router(
    [primary, backup],
    breaker_config=BreakerConfig(failure_threshold=3, base_cooldown=5.0),
    provider_configs={"nominatim": BreakerConfig(min_interval=1.0)},  # usage policy
)
```

A provider that advertises `Retry-After` opens the circuit immediately for
exactly that long — no reason to make it say so three times. A failed half-open
probe re-opens with a longer cooldown rather than needing another
`failure_threshold` failures.

State lives behind a `BreakerStore` protocol. The default is process-local;
implement `get`/`set` against a shared store to coordinate several processes.

## Testing your adapters

The contract test is reusable — point it at your own adapter:

```python
from provider_router import FailureKind
from provider_router.conformance import assert_provider_contract


def test_my_adapter_contract():
    assert_provider_contract(
        MyAdapter(),
        sample_request=Query("..."),
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

`ManualClock` makes breaker cooldowns instant and deterministic — no `sleep`,
no flake:

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

`0.1.0` — the API is not frozen yet.

Idempotency is deliberately not addressed: failover re-issues a request against
a different provider, which is safe for reads and *not* safe for writes. This
library is built for read paths.

## License

MIT
