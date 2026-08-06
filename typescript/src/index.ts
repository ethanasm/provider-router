/**
 * provider-router — route one capability across interchangeable providers.
 *
 * Zero runtime dependencies. Semantics match the Python package of the same
 * name; both are driven by the shared vectors in `spec/vectors/`.
 */

export {
  Breaker,
  breakerConfig,
  DEFAULT_BREAKER_CONFIG,
  InMemoryBreakerStore,
  INITIAL_STATE,
  isOpen,
  type BreakerConfig,
  type BreakerState,
  type BreakerStore,
} from './breaker.js';
export { type Clock, Deadline, ManualClock, SystemClock } from './clock.js';
export {
  assertProviderContract,
  checkProviderContract,
  ContractViolation,
  type ContractCheck,
} from './conformance.js';
export { AllProvidersFailed, RouteAborted, RouterError } from './errors.js';
export { emit, EventName, type Event, type EventSink } from './events.js';
export {
  budgetExhausted,
  describeFailure,
  FailureKind,
  isRouteTerminal,
  Outcome,
  rateLimited,
  terminal,
  transient,
  unsupported,
  type Failure,
} from './outcomes.js';
export { BaseProvider, type Attempt, type Provider } from './provider.js';
export { Router, type InvokeOptions, type RouterOptions } from './router.js';
export { succeeded, type AttemptRecord, type RouteResult } from './types.js';
