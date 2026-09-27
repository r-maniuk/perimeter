import { useEffect, useRef } from "react";
import { commitZoneEdit, createDrawnZone } from "@/features/zones/actions";
import { usePointer } from "@/state/pointer";
import { useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { mapController } from "./controller";

/**
 * The full-bleed map behind everything. Signed out, it is a softly blurred, slowly drifting
 * backdrop; signed in, it is the workspace. This component only bridges React state to the map
 * controller and the controller's events back to the stores.
 */
export function MapStage({ signedIn }: { signedIn: boolean }) {
  const container = useRef<HTMLDivElement>(null);
  const dark = useUi((s) => s.dark);
  const selection = useUi((s) => s.selection);
  const drawing = useUi((s) => s.drawing);
  const followId = useUi((s) => s.followId);
  const flyTarget = useUi((s) => s.flyTarget);
  const ready = usePointer((s) => s.mapReady);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const events = mapController.events;
    const off = [
      events.on("ready", () => usePointer.setState({ mapReady: true })),
      events.on("basemap", (basemap) => usePointer.setState({ basemap })),
      events.on("select", (next) => useUi.getState().select(next)),
      events.on("hover", (hover) => usePointer.setState({ hover })),
      events.on("draft", (draft) => usePointer.setState({ draft })),
      events.on("pointer", (cursor) => usePointer.setState({ cursor })),
      events.on("drawn", (shape) => void createDrawnZone(shape)),
      events.on("drawCancel", () => useUi.getState().setDrawing(false)),
      events.on("followBreak", () => useUi.getState().follow(null)),
      events.on("zoneEdit", (edit) => {
        if (edit.done) {
          usePointer.setState({ editing: null });
          commitZoneEdit(edit);
        } else {
          usePointer.setState({ editing: edit });
        }
      }),
    ];
    mapController
      .mount(element, useUi.getState().dark ? "dark" : "light")
      .catch((error: unknown) => console.error("map failed to start", error));
    return () => {
      for (const unsubscribe of off) unsubscribe();
      mapController.unmount();
      usePointer.setState({ mapReady: false });
    };
  }, []);

  useEffect(() => mapController.setTheme(dark ? "dark" : "light"), [dark]);
  useEffect(() => {
    if (ready) mapController.setInteractive(signedIn);
  }, [signedIn, ready]);
  useEffect(() => {
    if (ready) mapController.select(selection);
  }, [selection, ready]);
  useEffect(() => {
    if (ready) mapController.setDrawing(drawing);
  }, [drawing, ready]);
  useEffect(() => {
    if (ready) mapController.follow(followId);
  }, [followId, ready]);
  useEffect(() => {
    if (ready && flyTarget) mapController.flyTo(flyTarget.lat, flyTarget.lon, flyTarget.zoom);
  }, [flyTarget, ready]);

  return (
    <div className="fixed inset-0 overflow-hidden bg-bg">
      <div
        ref={container}
        className={cx(
          "absolute inset-0 transition-[filter,transform,opacity] duration-700 ease-out-quint",
          signedIn ? "scale-100 blur-0" : "scale-[1.04] blur-[7px] saturate-[1.15]",
          ready ? "opacity-100" : "opacity-0",
        )}
      />
    </div>
  );
}
