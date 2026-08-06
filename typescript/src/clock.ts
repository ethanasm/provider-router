/**
 * Clock and deadline primitives.
 *
 * Time is injected rather than imported so that breaker cooldowns, pacing, and
 * deadlines are testable without waiting. Everything uses a *monotonic* clock:
 * wall-clock jumps (NTP, DST) must never open or close a circuit breaker —
 * which is why this is `performance.now()` and not `Date.now()`.
 *
 * Seconds, not milliseconds, throughout — matching the Python port so the
 * shared test vectors mean the same thing in both.
 */

/** A monotonic clock the router can await against. */
export interface Clock {
  /** Seconds from an arbitrary fixed point. Never goes backwards. */
  monotonic(): number;
  /** Wait for `seconds`. */
  sleep(seconds: number): Promise<void>;
}

/** The real clock. */
export class SystemClock implements Clock {
  monotonic(): number {
    return performance.now() / 1000;
  }

  async sleep(seconds: number): Promise<void> {
    if (seconds > 0) {
      await new Promise<void>((resolve) => setTimeout(resolve, seconds * 1000));
    }
  }
}

/**
 * A clock tests drive by hand.
 *
 * `sleep` advances time instead of waiting, so a test can exercise an hour of
 * breaker cooldown instantly and deterministically.
 */
export class ManualClock implements Clock {
  now: number;
  readonly slept: number[] = [];

  constructor(now = 0) {
    this.now = now;
  }

  monotonic(): number {
    return this.now;
  }

  async sleep(seconds: number): Promise<void> {
    if (seconds > 0) {
      this.slept.push(seconds);
      this.now += seconds;
    }
  }

  /** Move time forward without recording a sleep. */
  advance(seconds: number): void {
    this.now += seconds;
  }
}

/**
 * An absolute point in monotonic time by which a route must finish.
 *
 * Passed down into `Provider.invoke` so adapters can bound their own internal
 * retries. Adapters own transport retry; the router owns failover. Without a
 * shared deadline those two nest and multiply: an adapter that retries three
 * times inside a router that tries three providers is nine upstream calls and
 * nine times the latency.
 */
export class Deadline {
  /** Monotonic timestamp the route must finish by. */
  readonly at: number;

  /**
   * The clock this deadline is measured against.
   *
   * Carried on the deadline rather than asked of the caller: the adapter that
   * receives it in `invoke` wants `deadline.remaining()`, and making it hunt
   * down the router's clock to answer that is friction with no upside.
   */
  readonly clock: Clock;

  constructor(at: number, clock: Clock = new SystemClock()) {
    this.at = at;
    this.clock = clock;
  }

  static inSeconds(seconds: number, clock: Clock = new SystemClock()): Deadline {
    return new Deadline(clock.monotonic() + seconds, clock);
  }

  /** Seconds left, floored at zero. */
  remaining(): number {
    return Math.max(0, this.at - this.clock.monotonic());
  }

  expired(): boolean {
    return this.remaining() <= 0;
  }
}
