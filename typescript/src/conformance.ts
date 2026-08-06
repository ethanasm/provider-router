/**
 * A contract test any adapter can run against.
 *
 * The router is only as good as its adapters' honesty. An adapter that returns
 * nothing from `classify`, throws out of `supports`, or reports a rate limit as
 * terminal will produce routing bugs that look like provider bugs and are
 * miserable to trace back.
 *
 * Point this at your adapter in your own test suite:
 *
 * ```ts
 * import { assertProviderContract, FailureKind } from 'provider-router';
 *
 * test('geocoder contract', async () => {
 *   await assertProviderContract(new NominatimGeocoder(), {
 *     sampleRequest: { address: '1600 Amphitheatre Pkwy' },
 *     sampleResult: { lat: 37.42, lon: -122.08 },
 *     expectedClassifications: [
 *       [new HttpError(429), FailureKind.RATE_LIMITED],
 *       [new TypeError('fetch failed'), FailureKind.TRANSIENT],
 *       [new Error('unparseable address'), FailureKind.TERMINAL],
 *     ],
 *   });
 * });
 * ```
 *
 * Adapters are *your* code — they encode your providers and your failure
 * dialects, so this library never ships them. What it ships is the contract
 * they implement and this test that holds them to it.
 */

import { FailureKind, Outcome } from './outcomes.js';
import type { Attempt, Provider } from './provider.js';

export class ContractViolation extends Error {
  constructor(problems: readonly string[]) {
    super(problems.map((p) => `  - ${p}`).join('\n'));
    this.name = 'ContractViolation';
  }
}

export interface ContractCheck<Req, Res> {
  sampleRequest: Req;
  sampleResult: Res;
  /** Error → the kind your adapter must classify it as. */
  expectedClassifications?: readonly (readonly [unknown, FailureKind])[];
}

const KINDS = new Set<string>(Object.values(FailureKind));
const OUTCOMES = new Set<string>(Object.values(Outcome));

/** Returns a list of contract problems; empty means the adapter is well-behaved. */
export function checkProviderContract<Req, Res>(
  provider: Provider<Req, Res>,
  check: ContractCheck<Req, Res>,
): string[] {
  const problems: string[] = [];

  if (typeof provider.name !== 'string' || provider.name.length === 0) {
    problems.push('provider.name must be a non-empty string — it is the routing identity');
  }

  try {
    const supported = provider.supports(check.sampleRequest);
    if (typeof supported !== 'boolean') {
      problems.push(`supports() returned ${typeof supported}, expected a boolean`);
    }
  } catch (error) {
    problems.push(
      `supports() threw (${String(error)}) — it runs before any I/O and must not throw; ` +
        'return false instead',
    );
  }

  const attempt: Attempt = { provider: provider.name, elapsed: 0.1, notes: [] };
  try {
    const outcome = provider.assess(check.sampleResult, attempt);
    if (!OUTCOMES.has(outcome as string)) {
      problems.push(`assess() returned ${JSON.stringify(outcome)}, expected an Outcome`);
    }
  } catch (error) {
    problems.push(`assess() threw (${String(error)}) — judge your own result without throwing`);
  }

  for (const [error, expected] of check.expectedClassifications ?? []) {
    const label = error instanceof Error ? error.constructor.name : typeof error;
    let failure;
    try {
      failure = provider.classify(error);
    } catch (thrown) {
      problems.push(`classify(${label}) threw (${String(thrown)}) — it must always return`);
      continue;
    }
    if (!failure || !KINDS.has(failure.kind as string)) {
      problems.push(
        `classify(${label}) returned ${JSON.stringify(failure)}; an unclassified error ` +
          'cannot be routed safely',
      );
      continue;
    }
    if (failure.kind !== expected) {
      problems.push(
        `classify(${label}) gave ${failure.kind}, expected ${expected}` +
          (expected === FailureKind.RATE_LIMITED
            ? ' — misreporting a rate limit means the breaker never opens and you ' +
              'hammer a throttled provider'
            : ''),
      );
    }
  }

  return problems;
}

/** Throws {@link ContractViolation} if the adapter breaks the contract. */
export function assertProviderContract<Req, Res>(
  provider: Provider<Req, Res>,
  check: ContractCheck<Req, Res>,
): void {
  const problems = checkProviderContract(provider, check);
  if (problems.length > 0) throw new ContractViolation(problems);
}
