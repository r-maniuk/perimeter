import {
  Activity,
  Bell,
  CircleDashed,
  CirclePlus,
  MonitorSmartphone,
  Navigation2,
  Search,
} from "lucide-react";
import { AnimatePresence, m } from "motion/react";
import type { ReactNode } from "react";
import { useAlerts } from "@/state/alerts";
import { type Panel, useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { IconButton } from "@/ui/IconButton";
import { Logo } from "@/ui/Logo";
import { UserMenu } from "./UserMenu";

export const PANELS: { id: Panel; label: string; shortcut: string; icon: ReactNode }[] = [
  { id: "zones", label: "Zones", shortcut: "Z", icon: <CircleDashed className="size-[18px]" /> },
  { id: "alerts", label: "Alerts", shortcut: "A", icon: <Bell className="size-[18px]" /> },
  { id: "fleet", label: "Fleet", shortcut: "F", icon: <Navigation2 className="size-[18px]" /> },
  {
    id: "sessions",
    label: "Sessions",
    shortcut: "S",
    icon: <MonitorSmartphone className="size-[18px]" />,
  },
  { id: "ops", label: "Pipeline", shortcut: "O", icon: <Activity className="size-[18px]" /> },
];

/** Floating command rail (desktop): panels, the primary "draw" action, search, account. */
export function Rail() {
  const panel = useUi((s) => s.panel);
  const drawing = useUi((s) => s.drawing);
  const unseen = useAlerts((s) => s.unseen);

  return (
    <nav
      aria-label="Workspace"
      className="glass fixed top-4 bottom-4 left-4 z-30 flex w-14 flex-col items-center rounded-[20px] py-3"
    >
      <div className="flex size-9 items-center justify-center" title="Perimeter">
        <Logo className="size-7" />
      </div>
      <div className="my-3 h-px w-7 bg-line" />
      <ul className="flex flex-col items-center gap-1.5">
        {PANELS.map((item) => (
          <li key={item.id} className="relative">
            <IconButton
              label={
                item.id === "alerts" && unseen > 0 ? `${item.label}, ${unseen} unseen` : item.label
              }
              shortcut={item.shortcut}
              active={panel === item.id}
              size="lg"
              onClick={() => useUi.getState().togglePanel(item.id)}
            >
              {item.icon}
            </IconButton>
            <AnimatePresence>
              {item.id === "alerts" && unseen > 0 && panel !== "alerts" && (
                <m.span
                  initial={{ scale: 0.4, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  exit={{ scale: 0.4, opacity: 0 }}
                  aria-hidden="true"
                  className="pointer-events-none absolute -top-1 -right-1 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-critical px-1 font-semibold text-[10px] text-white tabular-nums ring-2 ring-surface-solid"
                >
                  {unseen > 99 ? "99+" : unseen}
                </m.span>
              )}
            </AnimatePresence>
          </li>
        ))}
      </ul>
      <div className="my-3 h-px w-7 bg-line" />
      <IconButton
        label={drawing ? "Cancel drawing" : "Draw a zone"}
        shortcut="D"
        size="lg"
        active={drawing}
        onClick={() => useUi.getState().setDrawing(!drawing)}
        className={cx(!drawing && "text-accent")}
      >
        <CirclePlus className="size-[19px]" />
      </IconButton>
      <div className="flex-1" />
      <IconButton
        label="Search and commands"
        shortcut="⌘K"
        size="lg"
        onClick={() => useUi.getState().setCommandOpen(true)}
      >
        <Search className="size-[18px]" />
      </IconButton>
      <div className="mt-1.5">
        <UserMenu />
      </div>
    </nav>
  );
}
