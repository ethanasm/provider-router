# provider-router

Route one capability across interchangeable providers: preferred ordering,
automatic failover on rate limits and transient errors, and a single normalized
result contract.

Zero runtime dependencies, in **Python and TypeScript**, held to the same
behaviour by a shared set of test vectors.

```bash
pip install provider-router
npm install provider-router
```

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

This library ships **no adapters**, and that is the design.

An adapter encodes *your* providers, *your* failure dialects, and *your*
normalization choices. Shipping a catalog of them would mean a dependency per
provider, a package that ages at the speed of other people's APIs, and an
install step for every source you touch. What ships instead is the contract
adapters implement, the router that drives them, and a conformance test that
holds them honest. One install per language, whatever you're routing.

Adapters run 100–200 lines and live in your app. Reference implementations
against three real keyless providers are in [`examples/`](examples/) — copy
them, don't install them.

## Layout

| Path | What it is |
|:---|:---|
| [`python/`](python/) | `pip install provider-router` — the core, zero dependencies |
| [`typescript/`](typescript/) | `npm i provider-router` — the same core, zero dependencies |
| [`spec/`](spec/) | The contract in prose, and the shared test vectors both ports run |
| [`examples/`](examples/) | Reference adapters. Not published, not installable |

Full API docs live with each package: [Python](python/README.md) ·
[TypeScript](typescript/README.md).

## One contract, two runtimes

The ports are not "a port and its translation" — they're two implementations
held to one specification. [`spec/vectors/routing.json`](spec/vectors/routing.json)
describes twenty routing scenarios as data (provider behaviour in, expected
decisions out) and **both test suites read that same file**. A behavioural
change lands in both or CI goes red in one.

That isn't theoretical rigor. The vectors' first run caught a real divergence:
TypeScript guarded against an adapter whose `classify` returns nothing, Python
raised `AttributeError` from inside the router — a bug that reads as a router
fault and sends you to the wrong file. Python was fixed to match.

## What a provider owes the router

Four methods. The router never learns what the provider *is* — an HTTP API, an
MCP server, a scraper, a local model.

| Method | Contract |
|:---|:---|
| `supports(request)` | Can you honor **every** constraint? Return `false` rather than dropping one. |
| `invoke(request, deadline)` | Do the work or throw. Bound your own retries by the deadline. |
| `classify(error)` | Translate your exception into `RATE_LIMITED` / `TRANSIENT` / `TERMINAL` / `UNSUPPORTED` / `BUDGET`. |
| `assess(result, attempt)` | Judge your own success. `DEGRADED` if it's thin. |

`classify` deliberately has no default. Guessing that an unrecognized error is
transient is how a permanent misconfiguration becomes an infinite failover loop.

## The decisions worth knowing

**A `DEGRADED` result is kept but not settled for.** "The call returned 200" is
not "the provider answered well" — a geocoder can return 200 with no
coordinates. The router holds a degraded result and keeps trying
lower-preference providers for a good one. If none appears it returns the thin
answer rather than an error, and tells you which you got.

**`supports()` returning false is not a failure.** It's the router refusing to
let a provider answer a *different question* than the one asked. A search that
silently drops your filter hasn't failed; it has changed what the answer is an
answer to, which is worse, because nothing downstream can tell.

**A spend ceiling aborts the route.** Failing over after a budget trips would
spend more against the ceiling that just tripped.

**The answering provider is always reported.** Callers comparing results over
time — price history, anything with a threshold — need to know the source
changed, or a failover reads as a real change in the data.

**Failover happens at the whole-invocation boundary**, never mid-call. A router
that could resume someone else's half-finished pagination would have to
understand the payload, and then it isn't domain-agnostic any more.

**Idempotency is deliberately not addressed.** Failover re-issues a request
against a different provider: safe for reads, not safe for writes. This is a
library for read paths.

## Status

`0.1.0` in both languages. The API is not frozen yet.

## License

MIT
