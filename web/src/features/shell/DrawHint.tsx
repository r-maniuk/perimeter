import { MousePointer2, X } from "lucide-react";
import { AnimatePresence, m } from "motion/react";
import { DESKTOP, useMediaQuery } from "@/lib/useMediaQuery";
import { usePointer } from "@/state/pointer";
import { useUi } from "@/state/ui";
import { Kbd } from "@/ui/Kbd";

/** Instructions while drawing a zone. */
export function DrawHint() {
  const drawing = useUi((s) => s.drawing);
  const dragging = usePointer((s) => s.draft !== null);
  const desktop = useMediaQuery(DESKTOP);

  return (
    <AnimatePresence>
      {drawing && !dragging && (
        <m.div
          initial={{ opacity: 0, y: -8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          className="pointer-events-none fixed inset-x-0 top-[72px] z-30 flex justify-center px-3"
        >
          <div className="pointer-events-auto flex items-center gap-3 rounded-full bg-ink py-1.5 pr-1.5 pl-4 text-[12.5px] text-bg shadow-pop">
            <MousePointer2 className="size-3.5" aria-hidden="true" />
            <span>
              {desktop
                ? "Press at the centre and drag out the radius"
                : "Touch the centre and drag out the radius"}
            </span>
            {desktop && <Kbd tone="inverse">Esc</Kbd>}
            <button
              type="button"
              aria-label="Cancel drawing"
              onClick={() => useUi.getState().setDrawing(false)}
              className="flex size-7 items-center justify-center rounded-full bg-bg/15 hover:bg-bg/25"
            >
              <X className="size-3.5" />
            </button>
          </div>
        </m.div>
      )}
    </AnimatePresence>
  );
}
