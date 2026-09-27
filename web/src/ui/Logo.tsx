import { cx } from "./cx";

/** The mark: a dashed perimeter around a live point. */
export function Logo({ className, animated = false }: { className?: string; animated?: boolean }) {
  return (
    <svg viewBox="0 0 32 32" className={cx("text-accent", className)} aria-hidden="true">
      <circle
        cx="16"
        cy="16"
        r="12.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.6"
        strokeLinecap="round"
        strokeDasharray="4.2 3.4"
        className={animated ? "origin-center animate-[spin_24s_linear_infinite]" : undefined}
      />
      <circle cx="16" cy="16" r="5" fill="currentColor" />
    </svg>
  );
}
