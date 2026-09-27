import { Command } from "cmdk";
import {
  Activity,
  Bell,
  CircleDashed,
  CirclePlus,
  House,
  Keyboard,
  LocateFixed,
  LogOut,
  Maximize2,
  MonitorSmartphone,
  Moon,
  Navigation2,
  Search,
  Sun,
} from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { getDevice } from "@/api/endpoints";
import { describeError, isApiError } from "@/api/http";
import { getRuntime } from "@/app/runtime";
import { signOutHere } from "@/features/auth/signOut";
import { notify } from "@/features/shell/notices";
import { useZones } from "@/features/zones/useZones";
import { formatDistance } from "@/lib/format";
import { HOME, mapController } from "@/map/controller";
import { type Panel, useUi } from "@/state/ui";
import { Kbd } from "@/ui/Kbd";

interface Action {
  id: string;
  label: string;
  icon: ReactNode;
  shortcut?: string;
  keywords?: string;
  run: () => void;
}

const MAX_DEVICES = 30;

function panelAction(panel: Panel, label: string, icon: ReactNode, shortcut: string): Action {
  return {
    id: `panel:${panel}`,
    label: `Open ${label}`,
    icon,
    shortcut,
    keywords: label,
    run: () => useUi.getState().openPanel(panel),
  };
}

function matches(text: string, query: string): boolean {
  return text.toLowerCase().includes(query);
}

/** ⌘K: jump to a zone or device, or run any action, from the keyboard. */
export function CommandPalette() {
  const open = useUi((s) => s.commandOpen);
  const dark = useUi((s) => s.dark);
  const [query, setQuery] = useState("");
  const { data: zones } = useZones();
  const needle = query.trim().toLowerCase();

  const actions = useMemo<Action[]>(
    () => [
      {
        id: "draw",
        label: "Draw a zone",
        icon: <CirclePlus className="size-4" />,
        shortcut: "D",
        keywords: "create new geofence circle",
        run: () => useUi.getState().setDrawing(true),
      },
      {
        id: "draw-centre",
        label: "New zone at map centre",
        icon: <LocateFixed className="size-4" />,
        keywords: "create add place geofence circle here center keyboard",
        run: () => mapController.drawAtCentre(),
      },
      {
        id: "theme",
        label: dark ? "Switch to light theme" : "Switch to dark theme",
        icon: dark ? <Sun className="size-4" /> : <Moon className="size-4" />,
        shortcut: "T",
        keywords: "toggle theme appearance dark light",
        run: () => useUi.getState().setTheme(dark ? "light" : "dark"),
      },
      panelAction("zones", "zones", <CircleDashed className="size-4" />, "Z"),
      panelAction("alerts", "alerts", <Bell className="size-4" />, "A"),
      panelAction("fleet", "fleet", <Navigation2 className="size-4" />, "F"),
      panelAction("sessions", "sessions", <MonitorSmartphone className="size-4" />, "S"),
      panelAction("ops", "pipeline", <Activity className="size-4" />, "O"),
      {
        id: "fit",
        label: "Show all zones",
        icon: <Maximize2 className="size-4" />,
        keywords: "fit zoom zones",
        run: () => zones && mapController.fitZones(zones),
      },
      {
        id: "home",
        label: "Fly to Amsterdam",
        icon: <House className="size-4" />,
        keywords: "home city centre",
        run: () => useUi.getState().flyTo(HOME.lat, HOME.lon, HOME.zoom),
      },
      {
        id: "shortcuts",
        label: "Keyboard shortcuts",
        icon: <Keyboard className="size-4" />,
        shortcut: "?",
        keywords: "help keys",
        run: () => useUi.getState().setShortcutsOpen(true),
      },
      {
        id: "signout",
        label: "Sign out",
        icon: <LogOut className="size-4" />,
        keywords: "log out leave",
        run: () => void signOutHere(),
      },
    ],
    [dark, zones],
  );

  const shownActions = needle
    ? actions.filter((a) => matches(`${a.label} ${a.keywords ?? ""}`, needle))
    : actions.slice(0, 8);
  const shownZones = (zones ?? []).filter((z) => !needle || matches(z.name, needle)).slice(0, 8);
  const devices = useMemo(() => {
    const fleet = getRuntime()?.fleet;
    if (!fleet || needle.length === 0) return [];
    const found: string[] = [];
    for (const id of fleet.ids) {
      if (id.toLowerCase().includes(needle)) {
        found.push(id);
        if (found.length >= MAX_DEVICES) break;
      }
    }
    return found.sort();
    // The fleet changes constantly; the query is what drives this list.
  }, [needle]);
  const exactDevice =
    needle.length >= 2 && !devices.some((d) => d.toLowerCase() === needle) ? query.trim() : null;

  function close() {
    useUi.getState().setCommandOpen(false);
    setQuery("");
  }

  function run(action: () => void) {
    close();
    action();
  }

  async function findDevice(id: string) {
    close();
    try {
      const device = await getDevice(id);
      useUi.getState().select({ kind: "device", id: device.id });
      useUi.getState().flyTo(device.lat, device.lon, 16);
    } catch (error) {
      notify({
        tone: isApiError(error, 404) ? "warning" : "error",
        title: isApiError(error, 404) ? `No device “${id}”` : "Search failed",
        body: isApiError(error, 404) ? "It has not reported recently." : describeError(error),
      });
    }
  }

  return (
    <Command.Dialog
      open={open}
      onOpenChange={(next) => (next ? useUi.getState().setCommandOpen(true) : close())}
      label="Search and commands"
      shouldFilter={false}
      loop
      overlayClassName="fixed inset-0 z-50 bg-black/20 backdrop-blur-[2px]"
      contentClassName="glass fixed top-[12vh] left-1/2 z-50 w-[min(94vw,580px)] -translate-x-1/2 animate-rise overflow-hidden rounded-3xl"
    >
      <div className="flex items-center gap-3 border-line border-b px-5">
        <Search className="size-4 shrink-0 text-muted" />
        <Command.Input
          value={query}
          onValueChange={setQuery}
          placeholder="Search zones and devices, or type a command…"
          className="h-14 w-full bg-transparent text-[15px] text-ink outline-none placeholder:text-muted"
        />
        <Kbd>Esc</Kbd>
      </div>
      <Command.List className="scroll-quiet max-h-[min(60vh,440px)] overflow-y-auto p-2">
        <Command.Empty className="px-4 py-10 text-center text-[13px] text-muted">
          Nothing matches “{query}”.
        </Command.Empty>
        {shownZones.length > 0 && (
          <Command.Group heading="Zones" className={GROUP}>
            {shownZones.map((zone) => (
              <Command.Item
                key={zone.id}
                value={`zone:${zone.id}`}
                onSelect={() =>
                  run(() => {
                    useUi.getState().select({ kind: "zone", id: zone.id });
                    mapController.fitZones([zone]);
                  })
                }
                className={ITEM}
              >
                <span className="flex size-6 items-center justify-center">
                  <span className="size-3 rounded-full" style={{ background: zone.color }} />
                </span>
                <span className="flex-1 truncate">{zone.name}</span>
                <span className="text-[12px] text-muted">{formatDistance(zone.radius_m)}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}
        {(devices.length > 0 || exactDevice) && (
          <Command.Group heading="Devices" className={GROUP}>
            {devices.map((id) => (
              <Command.Item
                key={id}
                value={`device:${id}`}
                onSelect={() =>
                  run(() => {
                    const device = getRuntime()?.fleet.viewOf(id);
                    useUi.getState().select({ kind: "device", id });
                    if (device) useUi.getState().flyTo(device.lat, device.lon, 16);
                  })
                }
                className={ITEM}
              >
                <span className="flex size-6 items-center justify-center text-muted">
                  <Navigation2 className="size-4" />
                </span>
                <span className="flex-1 truncate font-mono text-[13px]">{id}</span>
                <span className="text-[12px] text-muted">live</span>
              </Command.Item>
            ))}
            {exactDevice && (
              <Command.Item
                value={`find:${exactDevice}`}
                onSelect={() => void findDevice(exactDevice)}
                className={ITEM}
              >
                <span className="flex size-6 items-center justify-center text-muted">
                  <Search className="size-4" />
                </span>
                <span className="flex-1 truncate">
                  Find device <span className="font-mono">“{exactDevice}”</span> anywhere
                </span>
              </Command.Item>
            )}
          </Command.Group>
        )}
        {shownActions.length > 0 && (
          <Command.Group heading="Actions" className={GROUP}>
            {shownActions.map((action) => (
              <Command.Item
                key={action.id}
                value={`action:${action.id}`}
                onSelect={() => run(action.run)}
                className={ITEM}
              >
                <span className="flex size-6 items-center justify-center text-muted">
                  {action.icon}
                </span>
                <span className="flex-1">{action.label}</span>
                {action.shortcut && <Kbd>{action.shortcut}</Kbd>}
              </Command.Item>
            ))}
          </Command.Group>
        )}
      </Command.List>
    </Command.Dialog>
  );
}

const GROUP =
  "[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pt-2.5 [&_[cmdk-group-heading]]:pb-1.5 [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:text-muted [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.08em]";
const ITEM =
  "flex h-11 cursor-default select-none items-center gap-3 rounded-xl px-3 text-[13.5px] text-ink data-[selected=true]:bg-accent-soft";
