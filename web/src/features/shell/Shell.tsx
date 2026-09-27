import { useEffect } from "react";
import { getRuntime } from "@/app/runtime";
import { CommandPalette } from "@/features/command/CommandPalette";
import { ShortcutsDialog } from "@/features/command/ShortcutsDialog";
import { DeviceTooltip } from "@/features/fleet/DeviceTooltip";
import { DraftLabel } from "@/features/zones/DraftLabel";
import { useZones } from "@/features/zones/useZones";
import { DESKTOP, useMediaQuery } from "@/lib/useMediaQuery";
import { useAlerts } from "@/state/alerts";
import { type Panel, useUi } from "@/state/ui";
import { AwayNotice } from "./AwayNotice";
import { BottomSheet } from "./BottomSheet";
import { CursorReadout } from "./CursorReadout";
import { DrawHint } from "./DrawHint";
import { Inspector } from "./Inspector";
import { MapControls } from "./MapControls";
import { useNotices } from "./notices";
import { Rail } from "./Rail";
import { SidePanel } from "./SidePanel";
import { Toaster } from "./Toaster";
import { TopBar } from "./TopBar";

const PANEL_KEYS: Record<string, Panel> = {
  z: "zones",
  a: "alerts",
  f: "fleet",
  s: "sessions",
  o: "ops",
};

function typingInto(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/** The signed-in workspace: floating chrome on desktop, a bottom sheet on phones. */
export function Shell() {
  const desktop = useMediaQuery(DESKTOP);
  const panel = useUi((s) => s.panel);
  // Zones are needed by the map, the inspector and the palette, not only by their panel.
  useZones();

  useEffect(() => {
    getRuntime()?.setOps(panel === "ops");
    if (panel === "alerts") useAlerts.getState().markSeen();
  }, [panel]);

  useEffect(() => {
    const timer = setInterval(() => {
      const now = Date.now();
      useAlerts.getState().expireToasts(now);
      useNotices.getState().expire(now);
    }, 400);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const ui = useUi.getState();
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        ui.setCommandOpen(!ui.commandOpen);
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey || typingInto(event.target)) return;
      // Holding a key must not flip a toggle on every auto-repeat.
      if (event.repeat) return;
      if (ui.commandOpen || ui.shortcutsOpen) return;
      if (document.querySelector("[role=dialog], [role=alertdialog]")) return;
      const key = event.key;
      if (key === "Escape") {
        if (ui.drawing) ui.setDrawing(false);
        else if (ui.selection) ui.select(null);
        else if (ui.panel) ui.openPanel(null);
        return;
      }
      if (key === "?") {
        ui.setShortcutsOpen(true);
        return;
      }
      if (event.shiftKey) return;
      const panelKey = PANEL_KEYS[key.toLowerCase()];
      if (panelKey) {
        ui.togglePanel(panelKey);
        return;
      }
      if (key === "d" || key === "n") ui.setDrawing(!ui.drawing);
      else if (key === "t") ui.setTheme(ui.dark ? "light" : "dark");
      else if (key === "l" && ui.selection?.kind === "device") {
        ui.follow(ui.followId === ui.selection.id ? null : ui.selection.id);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      {desktop ? (
        <>
          <Rail />
          <SidePanel />
          <Inspector />
          <MapControls />
          <CursorReadout />
        </>
      ) : (
        <BottomSheet />
      )}
      <TopBar compact={!desktop} />
      <DrawHint />
      <AwayNotice />
      <Toaster placement={desktop ? "bottom" : "top"} />
      <DeviceTooltip />
      <DraftLabel />
      <CommandPalette />
      <ShortcutsDialog />
    </>
  );
}
