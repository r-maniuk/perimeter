/**
 * Exactly-once delivery of user events on the client.
 *
 * Every event frame carries its sequence number in the user's event stream (`seq`) and the
 * sequence of the previous event of that same user (`prev`). A session that has seen `last` can
 * therefore tell a duplicate (`seq <= last`, e.g. the tail of a replay racing the live feed) from
 * a gap (`prev > last`: something between them never arrived). A gap is healed by reconnecting
 * with `resume_after=last`, which makes the server replay exactly the missing range.
 */

export type Verdict = "deliver" | "duplicate" | "gap";

export class EventSequencer {
  #last: number | null;

  constructor(last: number | null = null) {
    this.#last = last;
  }

  get last(): number | null {
    return this.#last;
  }

  judge(seq: number, prev: number): Verdict {
    if (this.#last === null) return "deliver";
    if (seq <= this.#last) return "duplicate";
    if (prev > this.#last) return "gap";
    return "deliver";
  }

  commit(seq: number): void {
    if (this.#last === null || seq > this.#last) this.#last = seq;
  }

  reset(last: number | null = null): void {
    this.#last = last;
  }
}
