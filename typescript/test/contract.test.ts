/**
 * The parts the shared vectors don't reach.
 *
 * Vectors cover routing decisions — the behaviour both ports must agree on.
 * These cover the surface a *user* touches: the conformance suite, deadlines,
 * and the guards that turn a mistake into a clear error instead of a confusing
 * one three frames away.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  assertProviderContract,
  BaseProvider,
  checkProviderContract,
  ContractViolation,
  Deadline,
  FailureKind,
  ManualClock,
  Outcome,
  rateLimited,
  Router,
  terminal,
  transient,
} from '../src/index.js';

class Throttled extends Error {}
class Blip extends Error {}

class WellBehaved {
  readonly name = 'good';
  supports(): boolean {
    return true;
  }
  async invoke(): Promise<string> {
    return 'value';
  }
  classify(error: unknown) {
    if (error instanceof Throttled) return rateLimited('slow down', 30, error);
    if (error instanceof Blip) return transient('blip', error);
    return terminal(String(error), error);
  }
  assess(): Outcome {
    return Outcome.OK;
  }
}

test('a well-behaved adapter passes the contract', () => {
  assertProviderContract(new WellBehaved(), {
    sampleRequest: 'q',
    sampleResult: 'value',
    expectedClassifications: [
      [new Throttled(), FailureKind.RATE_LIMITED],
      [new Blip(), FailureKind.TRANSIENT],
      [new Error('nope'), FailureKind.TERMINAL],
    ],
  });
});

test('a rate limit reported as terminal is caught, with the consequence named', () => {
  const adapter = new WellBehaved();
  adapter.classify = (error: unknown) => terminal(String(error), error);

  const problems = checkProviderContract(adapter, {
    sampleRequest: 'q',
    sampleResult: 'value',
    expectedClassifications: [[new Throttled(), FailureKind.RATE_LIMITED]],
  });

  assert.equal(problems.length, 1);
  // The point isn't that a string mismatched — it's that the breaker never
  // opens and you hammer a throttled provider forever.
  assert.match(problems[0]!, /breaker never opens/);
});

test('classify returning nothing is a contract violation, not a crash', () => {
  const adapter = new WellBehaved();
  adapter.classify = () => undefined as never;

  const problems = checkProviderContract(adapter, {
    sampleRequest: 'q',
    sampleResult: 'value',
    expectedClassifications: [[new Blip(), FailureKind.TRANSIENT]],
  });

  assert.match(problems[0]!, /cannot be routed safely/);
});

test('supports() throwing is a violation — it runs before any IO', () => {
  const adapter = new WellBehaved();
  adapter.supports = () => {
    throw new Error('boom');
  };

  const problems = checkProviderContract(adapter, { sampleRequest: 'q', sampleResult: 'v' });
  assert.match(problems[0]!, /return false instead/);
});

test('assertProviderContract throws ContractViolation listing every problem', () => {
  const adapter = new WellBehaved();
  adapter.supports = () => {
    throw new Error('boom');
  };
  adapter.assess = () => {
    throw new Error('boom');
  };

  assert.throws(
    () => assertProviderContract(adapter, { sampleRequest: 'q', sampleResult: 'v' }),
    (error: unknown) => {
      assert.ok(error instanceof ContractViolation);
      assert.equal(error.message.split('\n').length, 2);
      return true;
    },
  );
});

test('BaseProvider refuses to guess at classify', () => {
  class Half extends BaseProvider<string, string> {
    readonly name = 'half';
    async invoke(): Promise<string> {
      return 'v';
    }
  }
  // Guessing that an unrecognized error is transient is how a permanent
  // misconfiguration becomes an infinite failover loop.
  assert.throws(() => new Half().classify(new Error('x')), /must map errors to a Failure/);
});

test('BaseProvider degrades a result whose attempt carries notes', () => {
  class Noted extends BaseProvider<string, string> {
    readonly name = 'noted';
    async invoke(): Promise<string> {
      return 'v';
    }
  }
  const provider = new Noted();
  const base = { provider: 'noted', elapsed: 0.1 };
  assert.equal(provider.assess('v', { ...base, notes: [] }), Outcome.OK);
  assert.equal(provider.assess('v', { ...base, notes: ['partial'] }), Outcome.DEGRADED);
});

test('a deadline answers remaining() against its own clock', () => {
  const clock = new ManualClock();
  const deadline = Deadline.inSeconds(30, clock);

  assert.equal(deadline.remaining(), 30);
  assert.equal(deadline.expired(), false);

  clock.advance(31);
  assert.equal(deadline.remaining(), 0, 'floored at zero, never negative');
  assert.equal(deadline.expired(), true);
});

test('a router needs at least one provider, with unique names', () => {
  assert.throws(() => new Router([]), /at least one provider/);
  assert.throws(
    () => new Router([new WellBehaved(), new WellBehaved()]),
    /names must be unique/,
  );
});

test('an event sink that throws cannot fail a route', async () => {
  const router = new Router<string, string>([new WellBehaved()], {
    clock: new ManualClock(),
    events: () => {
      throw new Error('logging is down');
    },
  });

  const result = await router.invoke('q');
  assert.equal(result.value, 'value');
});
