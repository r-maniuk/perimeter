import { AnimatePresence, m } from "motion/react";
import { useLayoutEffect } from "react";
import { DeviceInspector } from "@/features/fleet/DeviceInspector";
import { useZone } from "@/features/zones/useZones";
import { ZoneInspector } from "@/features/zones/ZoneInspector";
import { mapController } from "@/map/controller";
import { useUi } from "@/state/ui";

/** What is selected on the map, in detail (desktop: floating card on the right). */
export function Inspector() {
  const selection = useUi((s) => s.selection);
  const zone = useZone(selection?.kind === "zone" ? selection.id : null);
  const close = () => useUi.getState().select(null);
  const key = selection ? `${selection.kind}:${selection.id}` : "none";
  const visible = selection?.kind === "device" || (selection?.kind === "zone" && zone);

  // Map chrome in the right-hand column (attribution) moves out from under the inspector.
  useLayoutEffect(() => {
    document.documentElement.toggleAttribute("data-inspecting", Boolean(visible));
    mapController.setInsets({ right: visible ? 372 : 64 });
    return () => {
      document.documentElement.removeAttribute("data-inspecting");
      mapController.setInsets({ right: 0 });
    };
  }, [visible]);

  return (
    <AnimatePresence mode="wait">
      {visible && (
        <m.aside
          key={key}
          aria-label={selection?.kind === "zone" ? "Zone details" : "Device details"}
          className="glass fixed top-[76px] right-4 z-20 flex max-h-[calc(100dvh-92px)] w-[356px] flex-col overflow-hidden rounded-[22px]"
          initial={{ opacity: 0, x: 14, scale: 0.985 }}
          animate={{ opacity: 1, x: 0, scale: 1 }}
          exit={{ opacity: 0, x: 10, scale: 0.985, transition: { duration: 0.14 } }}
          transition={{ type: "spring", stiffness: 420, damping: 36 }}
        >
          {selection?.kind === "zone" && zone && <ZoneInspector zone={zone} onClose={close} />}
          {selection?.kind === "device" && <DeviceInspector id={selection.id} onClose={close} />}
        </m.aside>
      )}
    </AnimatePresence>
  );
}
