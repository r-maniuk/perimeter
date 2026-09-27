import type { ReactNode } from "react";
import { cx } from "./cx";

export function Kbd({
  children,
  tone = "default",
}: {
  children: ReactNode;
  tone?: "default" | "inverse";
}) {
  return (
    <kbd
      className={cx(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-md px-1 font-medium font-sans text-[11px] leading-none",
        tone === "default"
          ? "bg-surface-2 text-muted ring-1 ring-line ring-inset"
          : "bg-bg/15 text-bg/80",
      )}
    >
      {children}
    </kbd>
  );
}
