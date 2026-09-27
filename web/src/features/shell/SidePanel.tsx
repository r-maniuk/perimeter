import { AnimatePresence, m } from "motion/react";
import { useId, useLayoutEffect } from "react";
import { mapController } from "@/map/controller";
import { useUi } from "@/state/ui";
import { PANEL_VIEWS } from "./panels";

/** The panel that slides out next to the rail (desktop). */
export function SidePanel() {
  const panel = useUi((s) => s.panel);
  const titleId = useId();
  const View = panel ? PANEL_VIEWS[panel] : null;

  // Map chrome in the left-hand column (scale, cursor position) moves out from under the panel.
  useLayoutEffect(() => {
    document.documentElement.toggleAttribute("data-panel", panel !== null);
    mapController.setInsets({ left: panel ? 460 : 72, top: 60 });
    return () => {
      document.documentElement.removeAttribute("data-panel");
      mapController.setInsets({ left: 0, top: 0 });
    };
  }, [panel]);

  return (
    <AnimatePresence mode="wait">
      {panel && View && (
        <m.section
          key={panel}
          aria-labelledby={titleId}
          className="glass fixed top-4 bottom-4 left-[88px] z-20 flex w-[372px] flex-col overflow-hidden rounded-[22px]"
          initial={{ opacity: 0, x: -14, scale: 0.985 }}
          animate={{ opacity: 1, x: 0, scale: 1 }}
          exit={{ opacity: 0, x: -10, scale: 0.985, transition: { duration: 0.14 } }}
          transition={{ type: "spring", stiffness: 420, damping: 36 }}
        >
          <View titleId={titleId} onClose={() => useUi.getState().openPanel(null)} />
        </m.section>
      )}
    </AnimatePresence>
  );
}
