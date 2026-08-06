/**
 * Shared result shapes.
 *
 * Split out from `router.ts` only to keep `errors.ts` from importing the
 * router — the router throws those errors, so the dependency has to run one
 * way.
 */

import type { Failure, Outcome } from './outcomes.js';

/** What happened with one provider during one route. */
export interface AttemptRecord {
  readonly provider: string;
  readonly outcome?: Outcome;
  readonly failure?: Failure;
  readonly skipped?: string;
  readonly elapsed: number;
}

export function succeeded(record: AttemptRecord): boolean {
  return record.outcome !== undefined;
}

/** A result plus the provenance the caller needs to interpret it. */
export interface RouteResult<Res> {
  readonly value: Res;
  readonly provider: string;
  readonly outcome: Outcome;
  readonly attempts: readonly AttemptRecord[];
  /** Whether the answer is the thin version. */
  readonly degraded: boolean;
  /** Whether a provider ahead of this one was tried and did not serve it. */
  readonly failedOver: boolean;
}
