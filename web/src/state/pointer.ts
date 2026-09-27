/** High-frequency map pointer state, kept apart so only the few components that show it re-render. */
import { create } from "zustand";
import type { DraftState } from "@/map/controller";

interface PointerState {
  hover: { id: string; x: number; y: number } | null;
  draft: DraftState | null;
  cursor: { lat: number; lon: number } | null;
  /** Live preview while a zone handle is dragged. */
  editing: { id: string; radiusM?: number; center?: { lat: number; lon: number } } | null;
  mapReady: boolean;
  basemap: "loading" | "ok" | "offline";
}

export const usePointer = create<PointerState>()(() => ({
  hover: null,
  draft: null,
  cursor: null,
  editing: null,
  mapReady: false,
  basemap: "loading",
}));
