/**
 * The router: ordered preference, automatic failover, one normalized result.
 *
 * Semantics, in one place, because every one of these was a decision:
 *
 * - **Order is preference.** Providers are tried in the order given. There is
 *   no hidden scoring; if you want a different order, pass a different list.
 * - **Failover happens at the whole-invocation boundary**, never mid-call. If a
 *   provider paginates or fans out internally, that is one invocation it owns.
 *   A router that could resume someone else's half-finished pagination would
 *   have to understand the payload, and then it would not be domain-agnostic.
 * - **A `DEGRADED` result is kept but not settled for.** The router holds it and
 *   keeps trying lower-preference providers for an `OK` one. If none appears,
 *   the best degraded result is returned rather than an error — thin beats
 *   nothing, and the caller is told which it got.
 * - **`BUDGET` aborts the route.** Failing over after a spend ceiling trips
 *   would spend more against the ceiling that just tripped.
 * - **The answering provider is always reported.** Callers that compare results
 *   across time — price history, anything with a threshold — need to know the
 *   source changed, or a failover reads as a real change in the data.
 */

import {
  Breaker,
  type BreakerConfig,
  type BreakerStore,
  InMemoryBreakerStore,
} from './breaker.js';
import { type Clock, Deadline, SystemClock } from './clock.js';
import { AllProvidersFailed, RouteAborted } from './errors.js';
import { emit, EventName, type EventSink } from './events.js';
import {
  type Failure,
  FailureKind,
  isRouteTerminal,
  Outcome,
  terminal,
} from './outcomes.js';
import type { Attempt, Provider } from './provider.js';
import type { AttemptRecord, RouteResult } from './types.js';

export interface RouterOptions {
  clock?: Clock;
  store?: BreakerStore;
  breakerConfig?: BreakerConfig;
  providerConfigs?: Record<string, BreakerConfig>;
  events?: EventSink;
  defaultTimeout?: number;
}

export interface InvokeOptions {
  deadline?: Deadline;
  timeout?: number;
}

/** Calls one capability through several interchangeable providers. */
export class Router<Req, Res> {
  readonly providers: readonly Provider<Req, Res>[];
  readonly breaker: Breaker;
  private readonly clock: Clock;
  private readonly events?: EventSink;
  private readonly defaultTimeout?: number;

  constructor(providers: readonly Provider<Req, Res>[], options: RouterOptions = {}) {
    if (providers.length === 0) {
      throw new Error('Router needs at least one provider');
    }
    const names = providers.map((p) => p.name);
    if (new Set(names).size !== names.length) {
      throw new Error(`provider names must be unique, got ${JSON.stringify(names)}`);
    }

    this.providers = [...providers];
    this.clock = options.clock ?? new SystemClock();
    this.breaker = new Breaker(
      options.store ?? new InMemoryBreakerStore(),
      this.clock,
      options.providerConfigs,
      options.breakerConfig,
    );
    this.events = options.events;
    this.defaultTimeout = options.defaultTimeout;
  }

  /**
   * Route `request` to the first provider that answers well.
   *
   * Throws {@link AllProvidersFailed} if none produced a result, or
   * {@link RouteAborted} if a route-terminal failure (a spend ceiling) stopped
   * the attempt early.
   */
  async invoke(request: Req, options: InvokeOptions = {}): Promise<RouteResult<Res>> {
    const deadline = this.resolveDeadline(options);
    const attempts: AttemptRecord[] = [];
    let best: { value: Res; provider: string } | undefined;

    emit(this.events, EventName.ROUTE_STARTED, undefined, { providers: this.providers.length });

    for (const provider of this.providers) {
      if (deadline?.expired()) {
        attempts.push({ provider: provider.name, skipped: 'deadline expired', elapsed: 0 });
        emit(this.events, EventName.PROVIDER_SKIPPED, provider.name, {
          reason: 'deadline_expired',
        });
        continue;
      }

      const skip = this.preflightSkip(provider, request);
      if (skip !== null) {
        attempts.push({ provider: provider.name, skipped: skip, elapsed: 0 });
        continue;
      }

      if (attempts.length > 0) {
        emit(this.events, EventName.FAILOVER_TRIGGERED, provider.name, {
          after: attempts[attempts.length - 1]!.provider,
        });
      }

      const [record, value] = await this.tryProvider(provider, request, deadline);
      attempts.push(record);

      if (record.failure && isRouteTerminal(record.failure)) {
        emit(this.events, EventName.ROUTE_ABORTED, provider.name, {
          reason: record.failure.kind,
        });
        throw new RouteAborted(record.failure, [...attempts]);
      }

      if (record.outcome === Outcome.OK) {
        emit(this.events, EventName.ROUTE_SELECTED, provider.name, { outcome: 'ok' });
        return this.result(value as Res, provider.name, Outcome.OK, attempts);
      }

      if (record.outcome === Outcome.DEGRADED && best === undefined) {
        // Hold it, but keep looking for something better.
        best = { value: value as Res, provider: provider.name };
        emit(this.events, EventName.PROVIDER_DEGRADED, provider.name);
      }
    }

    if (best !== undefined) {
      emit(this.events, EventName.ROUTE_SELECTED, best.provider, { outcome: 'degraded' });
      return this.result(best.value, best.provider, Outcome.DEGRADED, attempts);
    }

    emit(this.events, EventName.ROUTE_EXHAUSTED, undefined, { attempted: attempts.length });
    throw new AllProvidersFailed([...attempts]);
  }

  // ---------------------------------------------------------------- internals

  private result(
    value: Res,
    provider: string,
    outcome: Outcome,
    attempts: AttemptRecord[],
  ): RouteResult<Res> {
    const frozen = [...attempts];
    return {
      value,
      provider,
      outcome,
      attempts: frozen,
      degraded: outcome === Outcome.DEGRADED,
      failedOver: frozen.some((a) => a.provider !== provider),
    };
  }

  private resolveDeadline(options: InvokeOptions): Deadline | undefined {
    if (options.deadline) return options.deadline;
    const seconds = options.timeout ?? this.defaultTimeout;
    if (seconds === undefined) return undefined;
    return Deadline.inSeconds(seconds, this.clock);
  }

  /** Reasons not to call a provider at all, checked before any I/O. */
  private preflightSkip(provider: Provider<Req, Res>, request: Req): string | null {
    if (!provider.supports(request)) {
      emit(this.events, EventName.PROVIDER_SKIPPED, provider.name, { reason: 'unsupported' });
      return 'unsupported request';
    }
    const openReason = this.breaker.skipReason(provider.name);
    if (openReason !== null) {
      emit(this.events, EventName.PROVIDER_SKIPPED, provider.name, { reason: 'circuit_open' });
      return openReason;
    }
    return null;
  }

  /** Call one provider, translating whatever happens into an AttemptRecord. */
  private async tryProvider(
    provider: Provider<Req, Res>,
    request: Req,
    deadline: Deadline | undefined,
  ): Promise<[AttemptRecord, Res | undefined]> {
    const delay = this.breaker.paceDelay(provider.name);
    if (delay > 0) {
      if (deadline && delay > deadline.remaining()) {
        emit(this.events, EventName.PROVIDER_SKIPPED, provider.name, {
          reason: 'pace_exceeds_deadline',
        });
        return [
          { provider: provider.name, skipped: 'pacing exceeds deadline', elapsed: 0 },
          undefined,
        ];
      }
      emit(this.events, EventName.PACED, provider.name, { delay });
      await this.clock.sleep(delay);
    }

    this.breaker.halfOpen(provider.name);
    this.breaker.noteCall(provider.name);
    const started = this.clock.monotonic();

    const callDeadline = deadline ?? Deadline.inSeconds(Number.POSITIVE_INFINITY, this.clock);
    let value: Res;
    try {
      value = await provider.invoke(request, callDeadline);
    } catch (error) {
      const elapsed = this.clock.monotonic() - started;
      const failure = this.classify(provider, error);
      if (isRouteTerminal(failure)) {
        return [{ provider: provider.name, failure, elapsed }, undefined];
      }
      this.recordFailure(provider.name, failure);
      emit(this.events, EventName.PROVIDER_FAILED, provider.name, {
        kind: failure.kind,
        elapsed,
      });
      return [{ provider: provider.name, failure, elapsed }, undefined];
    }

    const elapsed = this.clock.monotonic() - started;
    const attempt: Attempt = { provider: provider.name, elapsed, notes: [] };
    const outcome = provider.assess(value, attempt);
    this.breaker.recordSuccess(provider.name);
    return [{ provider: provider.name, outcome, elapsed }, value];
  }

  /** Ask the adapter what went wrong; a broken `classify` must not mask the cause. */
  private classify(provider: Provider<Req, Res>, error: unknown): Failure {
    try {
      const failure = provider.classify(error);
      if (!failure || typeof failure.kind !== 'string') {
        return terminal(`${provider.name}.classify returned no Failure`, error);
      }
      return failure;
    } catch {
      const name = error instanceof Error ? error.constructor.name : typeof error;
      return terminal(`${provider.name}.classify threw on ${name}`, error);
    }
  }

  private recordFailure(name: string, failure: Failure): void {
    const retryAfter =
      failure.kind === FailureKind.RATE_LIMITED ? failure.retryAfter : undefined;
    const cooldown = this.breaker.recordFailure(name, retryAfter);
    if (cooldown > 0) {
      emit(this.events, EventName.CIRCUIT_OPENED, name, { cooldown, kind: failure.kind });
    }
  }
}
