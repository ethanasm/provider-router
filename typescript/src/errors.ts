/**
 * Errors thrown by the router itself.
 *
 * Both carry the full attempt list. When a route fails you need to know what
 * each provider did — "everything failed" without the per-provider reasons is
 * the error message that makes people take routers out again.
 */

import { describeFailure, type Failure } from './outcomes.js';
import type { AttemptRecord } from './types.js';

/** Base for routing failures. */
export class RouterError extends Error {}

/** Every provider was skipped or failed. */
export class AllProvidersFailed extends RouterError {
  readonly attempts: readonly AttemptRecord[];

  constructor(attempts: readonly AttemptRecord[]) {
    super(AllProvidersFailed.describe(attempts));
    this.name = 'AllProvidersFailed';
    this.attempts = attempts;
  }

  private static describe(attempts: readonly AttemptRecord[]): string {
    if (attempts.length === 0) return 'no providers were attempted';
    const parts = attempts.map((a) => {
      if (a.skipped) return `${a.provider}: skipped (${a.skipped})`;
      if (a.failure) return `${a.provider}: ${describeFailure(a.failure)}`;
      return `${a.provider}: unknown`;
    });
    return `all providers failed — ${parts.join('; ')}`;
  }
}

/**
 * A route-terminal failure stopped the attempt before other providers were tried.
 *
 * Thrown for spend ceilings and circuit breakers, where trying the next
 * provider would make the situation worse rather than better.
 */
export class RouteAborted extends RouterError {
  readonly failure: Failure;
  readonly attempts: readonly AttemptRecord[];

  constructor(failure: Failure, attempts: readonly AttemptRecord[]) {
    super(`route aborted by ${describeFailure(failure)}`);
    this.name = 'RouteAborted';
    this.failure = failure;
    this.attempts = attempts;
  }
}
