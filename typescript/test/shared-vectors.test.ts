/**
 * Run the shared routing vectors from `spec/vectors/routing.json`.
 *
 * The Python port runs this same file through an equivalent harness. That is
 * the only mechanism keeping two implementations of one contract honest: a
 * change to routing behaviour either lands in both ports or turns this red in
 * one of them.
 *
 * The harness deliberately builds providers from a tiny script language rather
 * than from mocks — a vector has to mean the same thing in both languages, and
 * "a list of strings" survives that translation where a mock framework does
 * not.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  AllProvidersFailed,
  type Attempt,
  type AttemptRecord,
  type BreakerConfig,
  budgetExhausted,
  type Deadline,
  type Event,
  type Failure,
  isOpen,
  ManualClock,
  Outcome,
  rateLimited,
  RouteAborted,
  type RouteResult,
  Router,
  terminal,
  transient,
} from '../dist/index.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const VECTORS = JSON.parse(
  readFileSync(join(HERE, '..', '..', 'spec', 'vectors', 'routing.json'), 'utf8'),
) as VectorFile;

interface ProviderSpec {
  name: string;
  script: string[];
  supports?: boolean;
  duration?: number;
}

interface VectorCase {
  name: string;
  why?: string;
  providers: ProviderSpec[];
  breaker?: Record<string, number>;
  routes?: number;
  timeout?: number;
  advance_between_routes?: number;
  expect: {
    served_by?: string;
    outcome?: string;
    attempts?: string[];
    failed_over?: boolean;
    raises?: string;
    calls?: Record<string, number>;
    circuit_open?: Record<string, boolean>;
    cooldown?: Record<string, number>;
    slept?: number[];
  };
}

interface VectorFile {
  version: number;
  cases: VectorCase[];
}

class ScriptedError extends Error {
  readonly behaviour: string;
  constructor(behaviour: string) {
    super(behaviour);
    this.behaviour = behaviour;
  }
}

/** A provider whose every call is dictated by a list of strings. */
class ScriptedProvider {
  readonly name: string;
  calls = 0;
  private readonly script: string[];
  private readonly supported: boolean;
  private readonly duration: number;
  private readonly clock: ManualClock;

  constructor(spec: ProviderSpec, clock: ManualClock) {
    this.name = spec.name;
    this.script = [...spec.script];
    this.supported = spec.supports ?? true;
    this.duration = spec.duration ?? 0;
    this.clock = clock;
  }

  private behaviour(): string {
    // Past the end of the script the last entry repeats, so a vector only has
    // to spell out the part that varies.
    return this.script[Math.min(this.calls - 1, this.script.length - 1)]!;
  }

  supports(_request: string): boolean {
    return this.supported;
  }

  async invoke(_request: string, _deadline: Deadline): Promise<string> {
    this.calls += 1;
    if (this.duration) this.clock.advance(this.duration);
    const behaviour = this.behaviour();
    if (behaviour === 'ok' || behaviour === 'degraded') return behaviour;
    throw new ScriptedError(behaviour);
  }

  classify(error: unknown): Failure {
    const behaviour = error instanceof ScriptedError ? error.behaviour : 'terminal';
    if (behaviour === 'classify_throws') throw new Error("this adapter's classify is broken");
    if (behaviour === 'classify_returns_nothing') return undefined as unknown as Failure;
    if (behaviour.startsWith('rate_limited')) {
      const [, after] = behaviour.split(':');
      return rateLimited('throttled', after ? Number(after) : undefined);
    }
    if (behaviour === 'transient') return transient('blip');
    if (behaviour === 'budget') return budgetExhausted('daily ceiling reached');
    return terminal(behaviour);
  }

  assess(result: string, _attempt: Attempt): Outcome {
    return result === 'degraded' ? Outcome.DEGRADED : Outcome.OK;
  }
}

function breakerConfigFor(spec: Record<string, number> = {}): BreakerConfig {
  return {
    failureThreshold: spec.failure_threshold ?? 3,
    baseCooldown: spec.base_cooldown ?? 5,
    maxCooldown: spec.max_cooldown ?? 300,
    minInterval: spec.min_interval ?? 0,
  };
}

function describe(record: AttemptRecord): string {
  if (record.skipped) return `${record.provider}:skipped`;
  if (record.failure) return `${record.provider}:${record.failure.kind}`;
  return `${record.provider}:${record.outcome}`;
}

for (const testCase of VECTORS.cases) {
  test(`vector: ${testCase.name}`, async () => {
    const clock = new ManualClock();
    const providers = testCase.providers.map((p) => new ScriptedProvider(p, clock));
    const cooldowns = new Map<string, number>();

    const sink = (event: Event): void => {
      if (event.name === 'router.provider.circuit_open' && event.provider) {
        cooldowns.set(event.provider, event.fields.cooldown as number);
      }
    };

    const router = new Router<string, string>(providers, {
      clock,
      breakerConfig: breakerConfigFor(testCase.breaker),
      events: sink,
      defaultTimeout: testCase.timeout,
    });

    const expected = testCase.expect;
    const routes = testCase.routes ?? 1;
    const advance = testCase.advance_between_routes ?? 0;

    let result: RouteResult<string> | undefined;
    let raised: string | undefined;
    let attempts: readonly AttemptRecord[] = [];
    let slept: number[] = [];

    for (let index = 0; index < routes; index += 1) {
      if (index > 0) clock.advance(advance);
      const sleptBefore = clock.slept.length;
      try {
        result = await router.invoke('request');
        raised = undefined;
        attempts = result.attempts;
      } catch (error) {
        result = undefined;
        if (error instanceof AllProvidersFailed) {
          raised = 'all_providers_failed';
          attempts = error.attempts;
        } else if (error instanceof RouteAborted) {
          raised = 'route_aborted';
          attempts = error.attempts;
        } else {
          throw error;
        }
      }
      slept = clock.slept.slice(sleptBefore);
    }

    assert.equal(raised, expected.raises, `raises (${testCase.why ?? ''})`);

    if (expected.served_by !== undefined) {
      assert.equal(result?.provider, expected.served_by, 'served_by');
    }
    if (expected.outcome !== undefined) {
      assert.equal(result?.outcome, expected.outcome, 'outcome');
    }
    if (expected.failed_over !== undefined) {
      assert.equal(result?.failedOver, expected.failed_over, 'failed_over');
    }
    if (expected.attempts !== undefined) {
      assert.deepEqual(attempts.map(describe), expected.attempts, 'attempts');
    }
    if (expected.calls !== undefined) {
      const actual = new Map(providers.map((p) => [p.name, p.calls]));
      for (const [name, count] of Object.entries(expected.calls)) {
        assert.equal(actual.get(name), count, `calls[${name}]`);
      }
    }
    if (expected.circuit_open !== undefined) {
      for (const [name, shouldBeOpen] of Object.entries(expected.circuit_open)) {
        const state = router.breaker.stateFor(name);
        assert.equal(isOpen(state, clock.monotonic()), shouldBeOpen, `circuit_open[${name}]`);
      }
    }
    if (expected.cooldown !== undefined) {
      for (const [name, seconds] of Object.entries(expected.cooldown)) {
        assert.equal(cooldowns.get(name), seconds, `cooldown[${name}]`);
      }
    }
    if (expected.slept !== undefined) {
      assert.deepEqual(slept, expected.slept, 'slept');
    }
  });
}

test('every case asserts something', () => {
  // A vector with an empty `expect` would pass silently and prove nothing.
  for (const testCase of VECTORS.cases) {
    assert.ok(Object.keys(testCase.expect).length > 0, testCase.name);
  }
});
