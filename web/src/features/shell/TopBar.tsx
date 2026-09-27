import { Search } from "lucide-react";
import { formatCount, plural } from "@/lib/format";
import { useLive } from "@/state/live";
import { useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { IconButton } from "@/ui/IconButton";
import { ConnectionPill } from "./ConnectionPill";
import { UserMenu } from "./UserMenu";

/**
 * The status island: connection health and the size of the live picture. On phones it also
 * carries search and the account menu, which live on the rail on desktop.
 */
export function TopBar({ compact }: { compact: boolean }) {
  const devices = useLive((s) => s.devicesInView);
  const moving = useLive((s) => s.movingInView);

  return (
    <div
      className={cx(
        "pointer-events-none fixed z-30 flex justify-center",
        compact ? "inset-x-3 top-[max(12px,env(safe-area-inset-top))]" : "inset-x-0 top-4",
      )}
    >
      <div
        className={cx(
          "glass pointer-events-auto flex h-11 items-center gap-1 rounded-full pr-1.5 pl-1.5",
          compact && "w-full justify-between",
        )}
      >
        <ConnectionPill compact={compact} />
        <span className="h-4 w-px bg-line-strong" aria-hidden="true" />
        <div
          className="flex items-baseline gap-1.5 px-2.5 text-[12.5px]"
          title="Devices streaming in the current view"
        >
          <span className="font-semibold text-ink tabular-nums">{formatCount(devices)}</span>
          <span className="text-muted">{plural(devices, "device")}</span>
          {!compact && (
            <>
              <span className="text-muted/60">·</span>
              <span className="font-semibold text-ink tabular-nums">{formatCount(moving)}</span>
              <span className="text-muted">moving</span>
            </>
          )}
        </div>
        {compact && (
          <div className="flex items-center gap-1">
            <IconButton
              label="Search and commands"
              side="bottom"
              onClick={() => useUi.getState().setCommandOpen(true)}
            >
              <Search className="size-[18px]" />
            </IconButton>
            <UserMenu side="bottom" />
          </div>
        )}
      </div>
    </div>
  );
}
