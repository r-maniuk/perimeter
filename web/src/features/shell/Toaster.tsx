import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import { AnimatePresence, m } from "motion/react";
import type { AlertKind } from "@/api/schemas";
import { KIND_ICON, KIND_LABEL, KIND_TEXT } from "@/features/alerts/kinds";
import { headline, type Toast } from "@/features/alerts/toasts";
import { useZones } from "@/features/zones/useZones";
import { formatClock } from "@/lib/format";
import { useAlerts } from "@/state/alerts";
import { useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { type Notice, useNotices } from "./notices";

const RESUME_AFTER_HOVER_MS = 2_500;
const FAR_FUTURE_MS = 3_600_000;

/** Alert toasts and notices, stacked (bottom-centre on desktop, under the status bar on phones). */
export function Toaster({ placement }: { placement: "top" | "bottom" }) {
  const toasts = useAlerts((s) => s.toasts);
  const notices = useNotices((s) => s.notices);
  // On phones a full-height sheet owns the screen; alerts wait in the badge until it closes.
  const covered = useUi((s) => placement === "top" && s.sheet === "full");
  const items: ({ type: "toast"; toast: Toast } | { type: "notice"; notice: Notice })[] = [
    ...notices.map((notice) => ({ type: "notice" as const, notice })),
    ...toasts.map((toast) => ({ type: "toast" as const, toast })),
  ];
  // Phones have room for one card above the map; desktops stack up to three.
  const shown = covered ? [] : items.slice(placement === "top" ? -1 : -3);
  const ordered = placement === "bottom" ? shown : [...shown].reverse();

  return (
    <section
      aria-label="Notifications"
      aria-live="polite"
      className={cx(
        "pointer-events-none fixed inset-x-0 z-40 flex flex-col items-center gap-2 px-3",
        placement === "bottom"
          ? "bottom-12"
          : "top-[max(68px,calc(env(safe-area-inset-top)+60px))]",
      )}
    >
      <AnimatePresence initial={false}>
        {ordered.map((item) =>
          item.type === "toast" ? (
            <AlertToast key={item.toast.id} toast={item.toast} placement={placement} />
          ) : (
            <NoticeCard key={item.notice.id} notice={item.notice} placement={placement} />
          ),
        )}
      </AnimatePresence>
    </section>
  );
}

function motionProps(placement: "top" | "bottom") {
  const offset = placement === "bottom" ? 16 : -16;
  return {
    layout: true,
    initial: { opacity: 0, y: offset, scale: 0.96 },
    animate: { opacity: 1, y: 0, scale: 1 },
    exit: { opacity: 0, scale: 0.96, transition: { duration: 0.18 } },
    transition: { type: "spring" as const, stiffness: 480, damping: 34 },
  };
}

function AlertToast({ toast, placement }: { toast: Toast; placement: "top" | "bottom" }) {
  const { data: zones } = useZones();
  const first = toast.alerts[0];
  const total = toast.count;
  const color = zones?.find((z) => z.id === first?.zoneId)?.color ?? "var(--accent)";
  const dominant =
    (Object.entries(toast.kinds) as [AlertKind, number][]).sort((a, b) => b[1] - a[1])[0]?.[0] ??
    "enter";

  function open() {
    const ui = useUi.getState();
    if (total === 1 && first) {
      ui.select({ kind: "device", id: first.deviceId });
      ui.flyTo(first.lat, first.lon, 16);
    } else {
      ui.openPanel("alerts");
    }
    useAlerts.getState().dismissToast(toast.id);
  }

  return (
    <m.div
      {...motionProps(placement)}
      className="glass pointer-events-auto relative w-[min(100%,380px)] overflow-hidden rounded-2xl"
      onPointerEnter={() =>
        useAlerts.getState().setToastExpiry(toast.id, Date.now() + FAR_FUTURE_MS)
      }
      onPointerLeave={() =>
        useAlerts.getState().setToastExpiry(toast.id, Date.now() + RESUME_AFTER_HOVER_MS)
      }
    >
      <span
        className="absolute inset-y-0 left-0 w-1"
        style={{ background: color }}
        aria-hidden="true"
      />
      <div className="flex items-start gap-3 py-3 pr-2.5 pl-4">
        <span
          className={cx(
            "mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-xl bg-surface-2 ring-1 ring-line ring-inset",
            KIND_TEXT[dominant],
          )}
        >
          {KIND_ICON[dominant]}
        </span>
        <button type="button" onClick={open} className="min-w-0 flex-1 text-left">
          <span className="block truncate font-medium text-[13px] text-ink">{headline(toast)}</span>
          <span className="mt-0.5 flex items-center gap-2 text-[11.5px] text-muted">
            {total > 1 ? (
              (Object.entries(toast.kinds) as [AlertKind, number][])
                .filter(([, n]) => n > 0)
                .map(([kind, n]) => (
                  <span key={kind} className={cx("font-medium tabular-nums", KIND_TEXT[kind])}>
                    {n} {KIND_LABEL[kind].toLowerCase()}
                  </span>
                ))
            ) : (
              <span>{first ? formatClock(first.occurredAt) : ""}</span>
            )}
            <span className="ml-auto text-muted/80">{total > 1 ? "View all" : "Show on map"}</span>
          </span>
        </button>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={() => useAlerts.getState().dismissToast(toast.id)}
          className="flex size-6 items-center justify-center rounded-lg text-muted hover:bg-surface-3 hover:text-ink"
        >
          <X className="size-3.5" />
        </button>
      </div>
    </m.div>
  );
}

const NOTICE_ICON = {
  info: <Info className="size-4 text-accent" />,
  success: <CircleCheck className="size-4 text-good" />,
  warning: <TriangleAlert className="size-4 text-serious" />,
  error: <CircleAlert className="size-4 text-critical" />,
} as const;

function NoticeCard({ notice, placement }: { notice: Notice; placement: "top" | "bottom" }) {
  return (
    <m.div
      {...motionProps(placement)}
      role={notice.tone === "error" ? "alert" : "status"}
      className="glass pointer-events-auto w-[min(100%,380px)] rounded-2xl"
    >
      <div className="flex items-start gap-3 py-3 pr-2.5 pl-4">
        <span className="mt-0.5">{NOTICE_ICON[notice.tone]}</span>
        <div className="min-w-0 flex-1">
          <div className="font-medium text-[13px] text-ink">{notice.title}</div>
          {notice.body && <div className="mt-0.5 text-[12px] text-ink-2">{notice.body}</div>}
          {notice.action && (
            <button
              type="button"
              onClick={() => {
                notice.action?.run();
                useNotices.getState().dismiss(notice.id);
              }}
              className="mt-2 rounded-lg bg-accent-soft px-2.5 py-1 font-medium text-[12px] text-accent hover:bg-accent/20"
            >
              {notice.action.label}
            </button>
          )}
        </div>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={() => useNotices.getState().dismiss(notice.id)}
          className="flex size-6 items-center justify-center rounded-lg text-muted hover:bg-surface-3 hover:text-ink"
        >
          <X className="size-3.5" />
        </button>
      </div>
    </m.div>
  );
}
