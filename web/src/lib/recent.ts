/** A set that keeps only the `capacity` values added last (the oldest are forgotten first). */
export class RecentSet<T> {
  readonly capacity: number;
  #values = new Set<T>();

  constructor(capacity: number) {
    if (capacity < 1) throw new Error("capacity must be at least 1");
    this.capacity = capacity;
  }

  get size(): number {
    return this.#values.size;
  }

  has(value: T): boolean {
    return this.#values.has(value);
  }

  /** Remember `value`; false when it is remembered already. */
  add(value: T): boolean {
    if (this.#values.has(value)) return false;
    this.#values.add(value);
    if (this.#values.size > this.capacity) {
      // Sets iterate in insertion order: the first value is the oldest.
      for (const oldest of this.#values) {
        this.#values.delete(oldest);
        break;
      }
    }
    return true;
  }
}
