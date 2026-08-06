# provider-router

Route one capability across interchangeable providers: preferred ordering,
automatic failover on rate limits and transient errors, and a single normalized
result contract.

Zero runtime dependencies. ESM, types included, Node 20+.

```bash
npm install provider-router
```

A Python port with identical semantics ships as
[`provider-router`](https://pypi.org/project/provider-router/) on PyPI. Both are
driven by the same shared test vectors, so they cannot quietly drift apart.

## The problem

You have three ways to get the same thing — one public API, one that needs no
key, one that is a scraper. They return different shapes, they fail in different
ways, and the good one starts returning `429` on a Tuesday.

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
usually 100–200 lines each. Reference implementations against three real
keyless providers live in
[`examples/`](https://github.com/ethanasm/provider-router/tree/main/examples) —
copy them, don't install them.

## Quickstart

```ts
import {
  type Attempt,
  type Deadline,
  type Failure,
  Outcome,
  rateLimited,
  Router,
  terminal,
  transient,
} from 'provider-router';

class Nominatim implements Provider<string, Coordinates> {
  readonly name = 'nominatim';

  supports(_address: string): boolean {
    return true;
  }

  async invoke(address: string, deadline: Deadline): Promise<Coordinates> {
    return geocode(address, { timeoutMs: deadline.remaining() * 1000 });
  }

  classify(error: unknown): Failure {
    if (error instanceof HttpError && error.status === 429) {
      return rateLimited(error.message, error.retryAfter, error);
    }
    if (error instanceof TypeError) return transient('fetch failed', error);
    return terminal(String(error), error);
  }

  assess(result: Coordinates, _attempt: Attempt): Outcome {
    // 200 with no coordinates is a success by every transport measure
    // and useless to the caller.
    return result.lat !== undefined ? Outcome.OK : Outcome.DEGRADED;
  }
}

const router = new Router([new GooglePlaces(), new Nominatim()], {
  defaultTimeout: 20,
});
const result = await router.invoke('1600 Amphitheatre Pkwy');

result.value; // the answer
result.provider; // who served it — callers comparing over time need this
result.degraded; // whether you got the good version
```

## What a provider owes the router

Four methods. The router never learns what the provider *is*.

| Method | Contract |
|:---|:---|
| `supports(request)` | Can you honor **every** constraint? Return `false` rather than dropping one. |
| `invoke(request, deadline)` | Do the work or throw. Bound your own retries by the deadline. |
| `classify(error)` | Translate your error into `RATE_LIMITED` / `TRANSIENT` / `TERMINAL` / `UNSUPPORTED` / `BUDGET`. |
| `assess(result, attempt)` | Judge your own success. `DEGRADED` if it's thin. |

`classify` deliberately has no default on `BaseProvider`. Guessing that an
unrecognized error is transient is how a permanent misconfiguration becomes an
infinite failover loop.

## Design decisions, and why

**Order is preference.** No hidden scoring. Want a different order, pass a
different array.

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

**`supports()` returning `false` is not a failure.** It's the router refusing
to let a provider answer a *different question* than the one asked. A search
that silently drops your filter hasn't failed; it has changed what the answer
is an answer to, which is worse, because nothing downstream can tell.

## Circuit breaker

Per-provider, `Retry-After`-driven, with exponential backoff and half-open
probes.

```ts
import { breakerConfig, Router } from 'provider-router';

const router = new Router([primary, backup], {
  breakerConfig: breakerConfig({ failureThreshold: 3, baseCooldown: 5 }),
  providerConfigs: {
    nominatim: breakerConfig({ minInterval: 1 }), // usage policy
  },
});
```

A provider that advertises `Retry-After` opens the circuit immediately for
exactly that long — no reason to make it say so three times. A failed half-open
probe re-opens with a longer cooldown rather than needing another
`failureThreshold` failures.

State lives behind a `BreakerStore` interface. The default is process-local;
implement `get`/`set` against a shared store to coordinate several processes.

## Testing your adapters

The contract test is reusable — point it at your own adapter:

```ts
import { assertProviderContract, FailureKind } from 'provider-router';

test('my adapter satisfies the contract', () => {
  assertProviderContract(new MyAdapter(), {
    sampleRequest: query,
    sampleResult: results,
    expectedClassifications: [
      [new HttpError(429), FailureKind.RATE_LIMITED],
      [new TypeError('fetch failed'), FailureKind.TRANSIENT],
      [new Error('bad input'), FailureKind.TERMINAL],
    ],
  });
});
```

It catches the failures that produce routing bugs which look like provider bugs:
`classify` throwing or returning nothing, `supports` throwing, a rate limit
reported as terminal (so the breaker never opens and you hammer a throttled
provider forever).

`ManualClock` makes breaker cooldowns instant and deterministic — no timers, no
flake:

```ts
import { ManualClock, Router } from 'provider-router';

const clock = new ManualClock();
const router = new Router([a, b], { clock });
await router.invoke(query);
clock.advance(31); // skip a 30-second cooldown
```

## Observability

Events are a plain callback — no logging framework imposed. A sink that throws
can never fail a route.

```ts
const router = new Router([a, b], {
  events: (e) => logger.info({ event: e.name, ...e.fields }, 'route'),
});
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
