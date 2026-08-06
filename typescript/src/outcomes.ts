/**
 * The outcome taxonomy — the vocabulary adapters use to describe what happened.
 *
 * The reusable kernel of this library is not the ordering (that is a list); it
 * is that every provider gets to describe success and failure in *the same
 * terms*, so the router can act on them without knowing anything about the
 * domain.
 *
 * Real providers signal the same condition in incompatible ways: an HTTP 429
 * with a `Retry-After` header, a 200 response whose payload says "rate limit
 * exceeded", an unparseable block page, or a 200 that is simply missing the
 * field you needed. An adapter's job is to translate its provider's dialect
 * into the types below; the router's job is everything downstream.
 */

/** How good a *successful* result is. */
export const Outcome = {
  OK: 'ok',
  /**
   * `DEGRADED` exists because "the call returned 200" is not the same as "the
   * provider answered well". A geocoder that returns 200 with no latitude or
   * longitude; a search whose result set silently drops a whole category
   * because one internal query failed mid-union. Both are successes by every
   * transport measure and useless to the caller.
   *
   * A degraded result is *kept* — it beats nothing — but the router keeps
   * looking for an `OK` one from a lower-preference provider before settling.
   */
  DEGRADED: 'degraded',
} as const;

export type Outcome = (typeof Outcome)[keyof typeof Outcome];

/** Why a provider could not answer. */
export const FailureKind = {
  /** Throttled. Carries `retryAfter` when the provider advertised one. */
  RATE_LIMITED: 'rate_limited',
  /** Worth trying again later — 5xx, connection reset, timeout. */
  TRANSIENT: 'transient',
  /** This provider cannot serve this request, and retrying will not help. */
  TERMINAL: 'terminal',
  /**
   * The provider cannot honor a constraint in the request.
   *
   * Distinct from `TERMINAL` because it is not a failure at all — it is the
   * router declining to let a provider answer a *different question* than the
   * one asked. A search that drops the caller's filter and returns unfiltered
   * results has not failed; it has silently changed what the answer is an
   * answer *to*, which is worse — nothing downstream can tell.
   */
  UNSUPPORTED: 'unsupported',
  /**
   * A spend ceiling or circuit breaker tripped.
   *
   * Route-terminal: the router aborts the whole invocation rather than failing
   * over. Failing over here would spend *more* against the very ceiling that
   * just tripped — a self-amplifying failure.
   */
  BUDGET: 'budget',
} as const;

export type FailureKind = (typeof FailureKind)[keyof typeof FailureKind];

/** A normalized provider failure. */
export interface Failure {
  readonly kind: FailureKind;
  readonly message: string;
  /** Seconds the provider asked us to wait. Only meaningful for RATE_LIMITED. */
  readonly retryAfter?: number;
  readonly cause?: unknown;
}

/** Whether a failure should abort the whole route, not just this provider. */
export function isRouteTerminal(failure: Failure): boolean {
  return failure.kind === FailureKind.BUDGET;
}

export function describeFailure(failure: Failure): string {
  return failure.message ? `${failure.kind}: ${failure.message}` : failure.kind;
}

/** Throttled, optionally with a server-advised wait in seconds. */
export function rateLimited(message = '', retryAfter?: number, cause?: unknown): Failure {
  return { kind: FailureKind.RATE_LIMITED, message, retryAfter, cause };
}

/** A blip worth retrying against another provider. */
export function transient(message = '', cause?: unknown): Failure {
  return { kind: FailureKind.TRANSIENT, message, cause };
}

/** A permanent failure for this provider. */
export function terminal(message = '', cause?: unknown): Failure {
  return { kind: FailureKind.TERMINAL, message, cause };
}

/** The provider cannot honor a constraint in the request. */
export function unsupported(message = '', cause?: unknown): Failure {
  return { kind: FailureKind.UNSUPPORTED, message, cause };
}

/** A spend ceiling tripped — aborts the route rather than failing over. */
export function budgetExhausted(message = '', cause?: unknown): Failure {
  return { kind: FailureKind.BUDGET, message, cause };
}
