/** View state: which panel is open, what is selected, drawing mode, theme. */
import { create } from "zustand";
import type { AlertKind } from "@/api/schemas";

export type Panel = "zones" | "alerts" | "fleet" | "sessions" | "ops";
export type Selection = { kind: "zone"; id: string } | { kind: "device"; id: string } | null;
export type ThemePreference = "light" | "dark" | "system";
export type SheetSnap = "peek" | "half" | "full";

const THEME_KEY = "perimeter.theme";

function savedTheme(): ThemePreference {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

function systemDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

interface UiState {
  panel: Panel | null;
  selection: Selection;
  drawing: boolean;
  followId: string | null;
  hoveredZone: string | null;
  theme: ThemePreference;
  dark: boolean;
  commandOpen: boolean;
  shortcutsOpen: boolean;
  sheet: SheetSnap;
  alertKind: AlertKind | null;
  alertZone: string | null;
  /** Bumped to ask the map to fly somewhere (target in `flyTarget`). */
  flyTarget: { lat: number; lon: number; zoom?: number; seq: number } | null;

  openPanel(panel: Panel | null): void;
  togglePanel(panel: Panel): void;
  select(selection: Selection): void;
  setDrawing(drawing: boolean): void;
  follow(id: string | null): void;
  hoverZone(id: string | null): void;
  setTheme(theme: ThemePreference): void;
  syncSystemTheme(): void;
  setCommandOpen(open: boolean): void;
  setShortcutsOpen(open: boolean): void;
  setSheet(snap: SheetSnap): void;
  setAlertKind(kind: AlertKind | null): void;
  setAlertZone(zoneId: string | null): void;
  flyTo(lat: number, lon: number, zoom?: number): void;
}

export const useUi = create<UiState>()((set, get) => ({
  panel: null,
  selection: null,
  drawing: false,
  followId: null,
  hoveredZone: null,
  theme: savedTheme(),
  dark: savedTheme() === "dark" || (savedTheme() === "system" && systemDark()),
  commandOpen: false,
  shortcutsOpen: false,
  sheet: "peek",
  alertKind: null,
  alertZone: null,
  flyTarget: null,

  openPanel: (panel) =>
    set((s) => ({ panel, sheet: panel && s.sheet === "peek" ? "half" : s.sheet })),
  togglePanel: (panel) =>
    set((s) => {
      const open = s.panel === panel ? null : panel;
      return { panel: open, sheet: open && s.sheet === "peek" ? "half" : s.sheet };
    }),
  select: (selection) =>
    set((s) => ({
      selection,
      followId: selection?.kind === "device" && s.followId === selection.id ? s.followId : null,
      sheet: selection && s.sheet === "peek" ? "half" : s.sheet,
    })),
  setDrawing: (drawing) => set({ drawing, ...(drawing ? { followId: null } : {}) }),
  follow: (followId) => set({ followId }),
  hoverZone: (hoveredZone) => set({ hoveredZone }),
  setTheme: (theme) => {
    try {
      if (theme === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, theme);
    } catch {
      // Not persisted; still applied for this visit.
    }
    set({ theme, dark: theme === "dark" || (theme === "system" && systemDark()) });
  },
  syncSystemTheme: () => {
    if (get().theme === "system") set({ dark: systemDark() });
  },
  setCommandOpen: (commandOpen) => set({ commandOpen }),
  setShortcutsOpen: (shortcutsOpen) => set({ shortcutsOpen }),
  setSheet: (sheet) => set({ sheet }),
  setAlertKind: (alertKind) => set({ alertKind }),
  setAlertZone: (alertZone) => set({ alertZone }),
  flyTo: (lat, lon, zoom) =>
    set((s) => ({
      flyTarget: {
        lat,
        lon,
        ...(zoom === undefined ? {} : { zoom }),
        seq: (s.flyTarget?.seq ?? 0) + 1,
      },
    })),
}));
