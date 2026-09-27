import type { ReactNode } from "react";
import { cx } from "./cx";

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "enter" | "exit" | "dwell" | "good" | "warning" | "critical";
  className?: string;
}) {
  const tones = {
    neutral: "bg-surface-2 text-ink-2 ring-line",
    accent: "bg-accent-soft text-accent ring-transparent",
    enter: "bg-enter/12 text-enter ring-enter/20",
    exit: "bg-exit/12 text-exit ring-exit/20",
    dwell: "bg-dwell/12 text-dwell ring-dwell/20",
    good: "bg-good/12 text-good ring-good/20",
    warning: "bg-warning/15 text-ink ring-warning/30",
    critical: "bg-critical/12 text-critical ring-critical/25",
  } as const;
  return (
    <span
      className={cx(
        "inline-flex h-5 items-center gap-1 rounded-md px-1.5 font-medium text-[11px] ring-1 ring-inset",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
