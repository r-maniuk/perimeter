import { Tooltip } from "radix-ui";
import type { ReactNode } from "react";
import { Kbd } from "./Kbd";

/** Tooltip for icon-only controls (names, shortcuts). Never the only place information lives. */
export function Hint({
  label,
  shortcut,
  side = "top",
  children,
}: {
  label: string;
  shortcut?: string | undefined;
  side?: "top" | "right" | "bottom" | "left";
  children: ReactNode;
}) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          side={side}
          sideOffset={8}
          className="z-50 flex animate-rise items-center gap-2 rounded-lg bg-ink px-2.5 py-1.5 font-medium text-[12px] text-bg shadow-pop"
        >
          {label}
          {shortcut && <Kbd tone="inverse">{shortcut}</Kbd>}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
