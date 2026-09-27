/**
 * Reconnection delays: exponential growth with "full jitter" — a uniformly random delay between
 * zero and the exponential ceiling — so that thousands of dashboards dropped by the same server
 * restart do not come back in lock-step.
 */

export interface BackoffPolicy {
  baseMs: number;
  capMs: number;
  /** Floor for the delay (e.g. when the server said it is overloaded). */
  minMs?: number;
}

export const DEFAULT_BACKOFF: BackoffPolicy = { baseMs: 500, capMs: 20_000 };

export function backoffDelay(
  attempt: number,
  random: () => number,
  policy: BackoffPolicy = DEFAULT_BACKOFF,
): number {
  const ceiling = Math.min(policy.capMs, policy.baseMs * 2 ** Math.max(0, attempt));
  return Math.max(policy.minMs ?? 0, Math.round(random() * ceiling));
}
