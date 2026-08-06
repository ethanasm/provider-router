/**
 * Per-provider circuit breaker and outbound pacing.
 *
 * This is the *negative* cache — the only caching this library does.
 * Remembering "that provider asked for 90 seconds, don't call it again until
 * then" is failover state. Caching successful responses is the application's
 * business and is deliberately out of scope.
 *
 * State lives behind `BreakerStore` so it can be process-local (the default,
 * zero infrastructure) or shared across processes later. No Redis, no database,
 * no import-time dependency on either.
 */

import type { Clock } from './clock.js';

/** Tuning for one provider's breaker and pacer. */
export interface BreakerConfig {
  /** Consecutive transient failures before the circuit opens. */
  readonly failureThreshold: number;
  /** Cooldown for the first open. Doubles per consecutive open. */
  readonly baseCooldown: number;
  readonly maxCooldown: number;
  /**
   * Minimum seconds between calls to this provider (politeness pacing).
   *
   * Some public endpoints require it — Nominatim's usage policy is one call per
   * second — and a router that fans out across providers is exactly the thing
   * likely to breach it.
   */
  readonly minInterval: number;
}

export const DEFAULT_BREAKER_CONFIG: BreakerConfig = {
  failureThreshold: 3,
  baseCooldown: 5,
  maxCooldown: 300,
  minInterval: 0,
};

export function breakerConfig(overrides: Partial<BreakerConfig> = {}): BreakerConfig {
  return { ...DEFAULT_BREAKER_CONFIG, ...overrides };
}

/** Immutable snapshot of one provider's health. */
export interface BreakerState {
  readonly consecutiveFailures: number;
  readonly consecutiveOpens: number;
  readonly openUntil: number;
  readonly lastCallAt: number;
  /** True when the circuit has re-closed for a single trial call. */
  readonly probing: boolean;
}

export const INITIAL_STATE: BreakerState = {
  consecutiveFailures: 0,
  consecutiveOpens: 0,
  openUntil: 0,
  lastCallAt: Number.NEGATIVE_INFINITY,
  probing: false,
};

export function isOpen(state: BreakerState, now: number): boolean {
  return now < state.openUntil;
}

/** Where breaker state lives. Swap for a shared store to coordinate processes. */
export interface BreakerStore {
  get(provider: string): BreakerState;
  set(provider: string, state: BreakerState): void;
}

/** Process-local state. The default, and enough for a single-process app. */
export class InMemoryBreakerStore implements BreakerStore {
  private readonly states = new Map<string, BreakerState>();

  get(provider: string): BreakerState {
    return this.states.get(provider) ?? INITIAL_STATE;
  }

  set(provider: string, state: BreakerState): void {
    this.states.set(provider, state);
  }

  clear(): void {
    this.states.clear();
  }
}

/** Decides whether a provider may be called, and records how it went. */
export class Breaker {
  private readonly store: BreakerStore;
  private readonly clock: Clock;
  private readonly configs: Record<string, BreakerConfig>;
  private readonly fallback: BreakerConfig;

  constructor(
    store: BreakerStore,
    clock: Clock,
    configs: Record<string, BreakerConfig> = {},
    fallback: BreakerConfig = DEFAULT_BREAKER_CONFIG,
  ) {
    this.store = store;
    this.clock = clock;
    this.configs = configs;
    this.fallback = fallback;
  }

  configFor(provider: string): BreakerConfig {
    return this.configs[provider] ?? this.fallback;
  }

  stateFor(provider: string): BreakerState {
    return this.store.get(provider);
  }

  /** `null` if the provider may be called, else why it is being skipped. */
  skipReason(provider: string): string | null {
    const state = this.store.get(provider);
    const now = this.clock.monotonic();
    if (isOpen(state, now)) {
      return `circuit open for ${(state.openUntil - now).toFixed(1)}s`;
    }
    return null;
  }

  /** Seconds to wait before calling, to respect `minInterval`. */
  paceDelay(provider: string): number {
    const config = this.configFor(provider);
    if (config.minInterval <= 0) return 0;
    const elapsed = this.clock.monotonic() - this.store.get(provider).lastCallAt;
    return Math.max(0, config.minInterval - elapsed);
  }

  /** Record that a call is being made now (drives pacing). */
  noteCall(provider: string): void {
    this.store.set(provider, {
      ...this.store.get(provider),
      lastCallAt: this.clock.monotonic(),
    });
  }

  /**
   * Mark that this call is the trial after a cooldown expired.
   *
   * A half-open probe that fails must not be treated as one ordinary failure
   * among many — it re-opens the circuit immediately, with a longer cooldown,
   * rather than needing another `failureThreshold` failures.
   */
  halfOpen(provider: string): void {
    const state = this.store.get(provider);
    if (state.consecutiveOpens > 0 && !isOpen(state, this.clock.monotonic())) {
      this.store.set(provider, { ...state, probing: true });
    }
  }

  /** A good answer closes the circuit and forgets the failure history. */
  recordSuccess(provider: string): void {
    this.store.set(provider, {
      ...this.store.get(provider),
      consecutiveFailures: 0,
      consecutiveOpens: 0,
      openUntil: 0,
      probing: false,
    });
  }

  /**
   * Record a failure. Returns the cooldown applied (0 if still closed).
   *
   * `retryAfter` — a provider-advertised wait — opens the circuit at once for
   * exactly that long. We believe a provider that tells us its own limit; there
   * is no reason to make it say so three times.
   */
  recordFailure(provider: string, retryAfter?: number): number {
    const config = this.configFor(provider);
    const state = this.store.get(provider);
    const now = this.clock.monotonic();

    const failures = state.consecutiveFailures + 1;
    const advertised = retryAfter !== undefined && retryAfter !== null;
    const trip = advertised || state.probing || failures >= config.failureThreshold;

    if (!trip) {
      this.store.set(provider, { ...state, consecutiveFailures: failures });
      return 0;
    }

    const opens = state.consecutiveOpens + 1;
    const cooldown = advertised
      ? (retryAfter as number)
      : Math.min(config.baseCooldown * 2 ** (opens - 1), config.maxCooldown);

    this.store.set(provider, {
      ...state,
      consecutiveFailures: failures,
      consecutiveOpens: opens,
      openUntil: now + cooldown,
      probing: false,
    });
    return cooldown;
  }
}
