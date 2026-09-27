import { CirclePlus, X } from "lucide-react";
import { animate, m, useMotionValue } from "motion/react";
import {
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
} from "react";
import { DeviceInspector } from "@/features/fleet/DeviceInspector";
import { useZone } from "@/features/zones/useZones";
import { ZoneInspector } from "@/features/zones/ZoneInspector";
import { mapController } from "@/map/controller";
import { useAlerts } from "@/state/alerts";
import { type SheetSnap, useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { PANEL_VIEWS } from "./panels";
import { PANELS } from "./Rail";

const PEEK = 92;

/** Room kept above a full-height sheet for the status bar. */
const TOP_CLEARANCE = 88;

function snapHeight(snap: SheetSnap, viewport: number): number {
  if (snap === "peek") return PEEK;
  if (snap === "half") return Math.round(viewport * 0.52);
  return viewport - TOP_CLEARANCE;
}

/** Phone layout: panels and inspectors live in a draggable sheet with three resting heights. */
export function BottomSheet() {
  const panel = useUi((s) => s.panel);
  const selection = useUi((s) => s.selection);
  const snap = useUi((s) => s.sheet);
  const drawing = useUi((s) => s.drawing);
  const unseen = useAlerts((s) => s.unseen);
  const zone = useZone(selection?.kind === "zone" ? selection.id : null);
  const titleId = useId();
  const height = useMotionValue(PEEK);
  const drag = useRef<{ y: number; h: number; moved: boolean; t: number } | null>(null);

  useLayoutEffect(() => {
    // Camera moves frame their target above the sheet (and below the status bar).
    const target = snapHeight(snap, window.innerHeight);
    mapController.setInsets({ top: 64, bottom: Math.min(target, window.innerHeight * 0.6) });
  }, [snap]);

  useEffect(() => {
    const target = snapHeight(snap, window.innerHeight);
    const controls = animate(height, target, { type: "spring", stiffness: 380, damping: 38 });
    return () => controls.stop();
  }, [snap, height]);

  useEffect(() => () => mapController.setInsets({ top: 0, bottom: 0 }), []);

  useEffect(() => {
    const onResize = () => height.set(snapHeight(useUi.getState().sheet, window.innerHeight));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [height]);

  function onPointerDown(e: ReactPointerEvent) {
    drag.current = { y: e.clientY, h: height.get(), moved: false, t: performance.now() };
  }

  function onPointerMove(e: ReactPointerEvent) {
    const d = drag.current;
    if (!d) return;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.abs(dy) < 6) return;
    if (!d.moved) {
      // Capture only once this is a drag: a tap must still reach the tab under the finger.
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    }
    d.moved = true;
    const max = window.innerHeight - TOP_CLEARANCE;
    height.set(Math.min(max, Math.max(PEEK - 20, d.h - dy)));
  }

  function onPointerUp(e: ReactPointerEvent) {
    const d = drag.current;
    drag.current = null;
    if (!d?.moved) return;
    const dy = e.clientY - d.y;
    const velocity = dy / Math.max(1, performance.now() - d.t);
    const current = height.get();
    const viewport = window.innerHeight;
    const snaps: SheetSnap[] = ["peek", "half", "full"];
    let next: SheetSnap;
    if (Math.abs(velocity) > 0.6) {
      const index = snaps.indexOf(snap);
      next = snaps[Math.min(2, Math.max(0, index + (velocity < 0 ? 1 : -1)))] ?? snap;
    } else {
      next = snaps.reduce((best, s) =>
        Math.abs(snapHeight(s, viewport) - current) < Math.abs(snapHeight(best, viewport) - current)
          ? s
          : best,
      );
    }
    if (next === snap) {
      animate(height, snapHeight(next, viewport), { type: "spring", stiffness: 380, damping: 38 });
    }
    useUi.getState().setSheet(next);
  }

  const View = panel ? PANEL_VIEWS[panel] : null;
  const showSelection = selection?.kind === "device" || (selection?.kind === "zone" && zone);
  const closeContent = () => {
    const ui = useUi.getState();
    if (selection) ui.select(null);
    else ui.openPanel(null);
    ui.setSheet("peek");
  };

  return (
    <>
      <m.button
        type="button"
        aria-label={drawing ? "Cancel drawing" : "Draw a zone"}
        aria-pressed={drawing}
        onClick={() => {
          const ui = useUi.getState();
          ui.setDrawing(!drawing);
          if (!drawing) ui.setSheet("peek");
        }}
        className={cx(
          "fixed right-4 z-20 flex size-14 items-center justify-center rounded-full shadow-pop transition-[background-color,opacity] duration-200",
          drawing ? "bg-ink text-bg" : "bg-accent text-accent-ink",
          // A full-height sheet leaves no map to draw on.
          snap === "full" && "pointer-events-none opacity-0",
        )}
        tabIndex={snap === "full" ? -1 : 0}
        // Clear of the map attribution line that sits on top of the sheet.
        style={{ bottom: height, marginBottom: 44 }}
      >
        {drawing ? <X className="size-6" /> : <CirclePlus className="size-6" />}
      </m.button>

      <m.section
        aria-labelledby={titleId}
        className="glass fixed inset-x-0 bottom-0 z-20 flex flex-col overflow-hidden rounded-t-[26px] pb-[env(safe-area-inset-bottom)]"
        style={{ height }}
      >
        <div
          className="shrink-0 touch-none select-none"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <div className="flex justify-center pt-2 pb-1.5">
            <button
              type="button"
              aria-label={snap === "peek" ? "Expand" : "Collapse"}
              onClick={() => useUi.getState().setSheet(snap === "peek" ? "half" : "peek")}
              className="h-5 w-16 rounded-full"
            >
              <span className="mx-auto block h-[5px] w-10 rounded-full bg-line-strong" />
            </button>
          </div>
          <nav aria-label="Workspace" className="grid grid-cols-5 px-2 pb-2">
            {PANELS.map((item) => {
              const active = panel === item.id && !showSelection;
              return (
                <button
                  key={item.id}
                  type="button"
                  aria-pressed={active}
                  aria-label={
                    item.id === "alerts" && unseen > 0
                      ? `${item.label}, ${unseen} unseen`
                      : item.label
                  }
                  onClick={() => {
                    const ui = useUi.getState();
                    ui.select(null);
                    if (panel === item.id && snap !== "peek") {
                      ui.openPanel(null);
                      ui.setSheet("peek");
                    } else {
                      ui.openPanel(item.id);
                      if (snap === "peek") ui.setSheet("half");
                    }
                  }}
                  className={cx(
                    "relative flex flex-col items-center gap-1 rounded-xl py-1.5 text-[10.5px] transition-colors",
                    active ? "text-accent" : "text-ink-2",
                  )}
                >
                  {item.icon}
                  {item.label}
                  {item.id === "alerts" && unseen > 0 && panel !== "alerts" && (
                    <span
                      aria-hidden="true"
                      className="absolute top-0.5 right-[calc(50%-18px)] flex h-4 min-w-4 items-center justify-center rounded-full bg-critical px-1 font-semibold text-[9.5px] text-white ring-2 ring-surface-solid"
                    >
                      {unseen > 99 ? "99+" : unseen}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>
        </div>
        <div id={titleId} className="sr-only">
          {showSelection
            ? "Details"
            : panel
              ? PANELS.find((p) => p.id === panel)?.label
              : "Workspace"}
        </div>
        <div className="min-h-0 flex-1 border-line border-t">
          {/* Keyed by what is selected, like the desktop inspector: a half-typed field, an open
              dwell picker or a trail window belongs to one zone or device, not the next. */}
          {showSelection ? (
            selection?.kind === "zone" && zone ? (
              <ZoneInspector key={`zone:${zone.id}`} zone={zone} onClose={closeContent} />
            ) : selection?.kind === "device" ? (
              <DeviceInspector
                key={`device:${selection.id}`}
                id={selection.id}
                onClose={closeContent}
              />
            ) : null
          ) : View ? (
            <View onClose={closeContent} />
          ) : null}
        </div>
      </m.section>
    </>
  );
}
