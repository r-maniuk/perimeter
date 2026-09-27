/**
 * Offset between this browser's clock and the server's, from ping/pong round trips (the NTP
 * idea: trust the sample with the smallest round trip, whose midpoint is least ambiguous).
 * Device ages ("updated 4 s ago") are computed on the server's timeline, so a laptop whose clock
 * is a minute off still shows fresh devices as fresh.
 */

interface Sample {
  offsetMs: number;
  rttMs: number;
  at: number;
}

const WINDOW = 8;

export class ClockSync {
  #samples: Sample[] = [];
  #offsetMs = 0;

  /** Record a round trip: sent at local `sentAt`, answered with `serverMs`, received at `receivedAt`. */
  sample(sentAt: number, serverMs: number, receivedAt: number): void {
    const rttMs = Math.max(0, receivedAt - sentAt);
    const offsetMs = serverMs + rttMs / 2 - receivedAt;
    this.#samples.push({ offsetMs, rttMs, at: receivedAt });
    if (this.#samples.length > WINDOW) this.#samples.shift();
    let best = this.#samples[0] as Sample;
    for (const s of this.#samples) if (s.rttMs < best.rttMs) best = s;
    this.#offsetMs = best.offsetMs;
  }

  /** A one-way hint (e.g. `hello.server_time`) used until round trips are measured. */
  hint(serverMs: number, receivedAt: number): void {
    if (this.#samples.length === 0) this.#offsetMs = serverMs - receivedAt;
  }

  get offsetMs(): number {
    return this.#offsetMs;
  }

  /** Server time now, in epoch milliseconds. */
  now(localNow: number = Date.now()): number {
    return localNow + this.#offsetMs;
  }

  get lastRttMs(): number | null {
    return this.#samples.at(-1)?.rttMs ?? null;
  }
}
