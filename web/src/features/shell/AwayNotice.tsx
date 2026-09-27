import { History, X } from "lucide-react";
import { AnimatePresence, m } from "motion/react";
import { useEffect } from "react";
import { formatCount, plural } from "@/lib/format";
import { useLive } from "@/state/live";
import { useUi } from "@/state/ui";

const VISIBLE_MS = 14_000;

/** "N events delivered while you were away", after a reconnect replayed missed events. */
export function AwayNotice() {
  const away = useLive((s) => s.away);

  useEffect(() => {
    if (!away) return;
    const timer = setTimeout(() => useLive.getState().clearAway(), VISIBLE_MS);
    return () => clearTimeout(timer);
  }, [away]);

  return (
    // The glass itself animates: an animated wrapper would become the backdrop the blur samples
    // (an empty layer), leaving the notice see-through over busy traffic.
    <div
      role="status"
      className="pointer-events-none fixed inset-x-0 top-[68px] z-30 flex justify-center px-3"
    >
      <AnimatePresence>
        {away && (
          <m.div
            initial={{ opacity: 0, y: -8, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -6, scale: 0.97 }}
            className="glass pointer-events-auto flex items-center gap-2.5 rounded-full py-1.5 pr-1.5 pl-3.5 text-[12.5px]"
          >
            <History className="size-4 text-accent" aria-hidden="true" />
            <span className="text-ink">
              <span className="font-semibold tabular-nums">{formatCount(away.count)}</span>{" "}
              {plural(away.count, "event")} delivered while you were away
            </span>
            <button
              type="button"
              onClick={() => {
                useUi.getState().openPanel("alerts");
                useLive.getState().clearAway();
              }}
              className="rounded-full bg-accent px-3 py-1 font-medium text-[12px] text-accent-ink hover:bg-accent-strong"
            >
              Review
            </button>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => useLive.getState().clearAway()}
              className="flex size-7 items-center justify-center rounded-full text-muted hover:bg-surface-3 hover:text-ink"
            >
              <X className="size-3.5" />
            </button>
          </m.div>
        )}
      </AnimatePresence>
    </div>
  );
}
