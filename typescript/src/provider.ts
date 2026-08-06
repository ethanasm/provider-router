/**
 * The provider contract.
 *
 * A provider is *anything* that can service the request: an HTTP API, an MCP
 * server, a scraper, a local model. The router never learns which. What it
 * needs from each is four things — can you handle this request, do the work,
 * tell me what went wrong in normalized terms, and tell me whether what you
 * returned is actually any good.
 */

import type { Deadline } from './clock.js';
import type { Failure, Outcome } from './outcomes.js';
import { Outcome as OutcomeValues } from './outcomes.js';

/**
 * Context handed to `assess` so it can judge a result it cannot judge alone.
 *
 * Some degradation is invisible in the returned object. A search that unions
 * several upstream queries and loses one of them returns a perfectly
 * well-formed, merely *incomplete* result; the fact that it is incomplete
 * lives in the attempt, not the payload. Adapters record that here.
 */
export interface Attempt {
  readonly provider: string;
  readonly elapsed: number;
  /** Adapter-supplied markers, e.g. `['partial_coverage']`. */
  readonly notes: readonly string[];
}

/** One implementation of a capability. */
export interface Provider<Req, Res> {
  readonly name: string;

  /**
   * Whether this provider can honor *every* constraint in the request.
   *
   * Return `false` rather than silently dropping one. A provider that ignores
   * a constraint does not fail — it answers a different question, and the
   * router has no way to tell that apart from a good answer.
   */
  supports(request: Req): boolean;

  /**
   * Do the work, or throw.
   *
   * Bound internal retries by `deadline`; the router will not interrupt a
   * provider that overruns it, it will only decline to try the next one.
   */
  invoke(request: Req, deadline: Deadline): Promise<Res>;

  /** Translate an error from `invoke` into the shared vocabulary. */
  classify(error: unknown): Failure;

  /** Judge a successful result. Return `DEGRADED` if it is thin. */
  assess(result: Res, attempt: Attempt): Outcome;
}

/**
 * Optional convenience base with sane defaults.
 *
 * Implementing `Provider` directly is fine — this exists so an adapter that has
 * nothing interesting to say about `supports` or `assess` does not have to
 * write them out. `classify` deliberately has *no* default: guessing that an
 * unrecognized error is transient is how a permanent misconfiguration turns
 * into an infinite failover loop.
 */
export abstract class BaseProvider<Req, Res> implements Provider<Req, Res> {
  abstract readonly name: string;

  supports(_request: Req): boolean {
    return true;
  }

  abstract invoke(request: Req, deadline: Deadline): Promise<Res>;

  classify(_error: unknown): Failure {
    throw new Error(
      `${this.constructor.name}.classify must map errors to a Failure; ` +
        'an unclassified error cannot be routed safely',
    );
  }

  assess(_result: Res, attempt: Attempt): Outcome {
    return attempt.notes.length > 0 ? OutcomeValues.DEGRADED : OutcomeValues.OK;
  }
}
