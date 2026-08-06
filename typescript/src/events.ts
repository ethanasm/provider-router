/**
 * Structured events.
 *
 * Stable event names, because the point of a router is that you can no longer
 * tell from the outside which provider served you — so the log has to say.
 * Emission is a plain callback: no logging framework is imposed, and a listener
 * that throws is never allowed to break a route.
 */

/** The catalog. These strings are a public contract — treat renames as breaking. */
export const EventName = {
  ROUTE_STARTED: 'router.route.started',
  ROUTE_SELECTED: 'router.route.selected',
  ROUTE_EXHAUSTED: 'router.route.exhausted',
  ROUTE_ABORTED: 'router.route.aborted',
  PROVIDER_SKIPPED: 'router.provider.skipped',
  PROVIDER_FAILED: 'router.provider.failed',
  PROVIDER_DEGRADED: 'router.provider.degraded',
  FAILOVER_TRIGGERED: 'router.failover.triggered',
  CIRCUIT_OPENED: 'router.provider.circuit_open',
  CIRCUIT_PROBED: 'router.provider.circuit_probe',
  PACED: 'router.provider.paced',
} as const;

export type EventName = (typeof EventName)[keyof typeof EventName];

export interface Event {
  readonly name: string;
  readonly provider?: string;
  readonly fields: Record<string, unknown>;
}

export type EventSink = (event: Event) => void;

/**
 * Fire an event, swallowing listener errors.
 *
 * Observability must not be able to fail a request that would otherwise have
 * succeeded.
 */
export function emit(
  sink: EventSink | undefined,
  name: string,
  provider?: string,
  fields: Record<string, unknown> = {},
): void {
  if (!sink) return;
  try {
    sink({ name, provider, fields });
  } catch {
    // A broken listener is not the caller's problem.
  }
}
