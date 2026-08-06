# The shared spec

Two implementations of one contract drift. Not maliciously — someone fixes an
edge case in the language they happen to be in that day, and six months later
the two libraries named `provider-router` disagree about what a half-open probe
does.

`vectors/routing.json` is what stops that. Both test suites read it:

- `python/tests/test_shared_vectors.py`
- `typescript/test/shared-vectors.test.ts`

A behavioural change either lands in both ports or CI goes red in one of them.

It works, too. The vectors' very first run found a genuine divergence: the
TypeScript router guarded against an adapter whose `classify` returns nothing,
and the Python one raised `AttributeError` from inside the router — a bug that
reads as a router fault and sends you to the wrong file. Python was fixed to
match.

## The vector format

Each case builds providers from a small script language. Not mocks: a vector
has to mean the same thing in both languages, and "a list of strings" survives
that translation where a mock framework does not.

```json
{
  "name": "transient failure fails over",
  "why": "The whole reason the library exists.",
  "providers": [
    { "name": "a", "script": ["transient"] },
    { "name": "b", "script": ["ok"] }
  ],
  "expect": { "served_by": "b", "outcome": "ok", "attempts": ["a:transient", "b:ok"] }
}
```

### Provider spec

| Field | Meaning |
|:---|:---|
| `name` | Provider name (also the routing identity). |
| `script` | What each successive call does. Past the end, the last entry repeats — so a vector only spells out the part that varies. |
| `supports` | `false` makes the provider decline the request before any call. |
| `duration` | Seconds the clock advances during `invoke`, for deadline cases. |

Script values: `ok`, `degraded`, `transient`, `terminal`, `rate_limited`,
`rate_limited:<seconds>` (an advertised `Retry-After`), `budget`,
`classify_throws`, `classify_returns_nothing`.

### Case options

| Field | Meaning |
|:---|:---|
| `breaker` | Config overrides: `failure_threshold`, `base_cooldown`, `max_cooldown`, `min_interval`. |
| `routes` | How many times to invoke the router (default 1). Assertions apply to the **last** route. |
| `advance_between_routes` | Seconds to advance the clock between routes — how cooldowns are exercised without waiting. |
| `timeout` | Route timeout in seconds. |

### Assertions

Every key is optional; all present keys must hold. `calls` counts across *all*
routes, everything else describes the last one.

| Key | Checks |
|:---|:---|
| `served_by` | Which provider's result was returned. |
| `outcome` | `ok` or `degraded`. |
| `attempts` | Ordered `name:state`, where state is an outcome, a failure kind, or `skipped`. |
| `failed_over` | Whether a provider ahead of the winner was tried. |
| `raises` | `all_providers_failed` or `route_aborted`. |
| `calls` | Times each provider's `invoke` actually ran — this is what proves a skip was a skip. |
| `circuit_open` | Per-provider breaker state at the end. |
| `cooldown` | The last cooldown applied per provider, in seconds. |
| `slept` | Sleeps the clock recorded during the last route (pacing). |

## Adding a case

Add it to `vectors/routing.json` and run both suites. If only one goes red, you
have found either a bug or a place the two ports were never really the same —
both worth knowing, which is the point.

A case with an empty `expect` would pass silently and prove nothing; both
suites assert that no such case exists.
