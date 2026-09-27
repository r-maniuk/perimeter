import { RefreshCw } from "lucide-react";
import { Popover } from "radix-ui";
import { getRuntime } from "@/app/runtime";
import { formatMs } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { useLive } from "@/state/live";
import { useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { present, type Tone } from "./connection";

const dots: Record<Tone, string> = {
  live: "bg-enter",
  busy: "bg-accent",
  warn: "bg-warning",
  down: "bg-critical",
};

export function ConnectionPill({ compact = false }: { compact?: boolean }) {
  const status = useLive((s) => s.status);
  const latency = useLive((s) => s.latencyMs);
  const now = useNow(500);
  const view = present(status, now);

  return (
    <Popover.Root>
      <Popover.Trigger
        className="flex h-8 items-center gap-2 rounded-full px-3 font-medium text-[12.5px] text-ink outline-none transition-colors hover:bg-surface-3/70 focus-visible:ring-2 focus-visible:ring-accent"
        aria-label={`Connection: ${view.label}`}
      >
        <span className="relative flex size-2">
          {(view.tone === "live" || view.tone === "busy") && (
            <span
              className={cx(
                "absolute inset-0 animate-ping rounded-full opacity-60",
                dots[view.tone],
              )}
            />
          )}
          <span className={cx("relative size-2 rounded-full", dots[view.tone])} />
        </span>
        <span aria-live="polite">{view.label}</span>
        {!compact && status.state === "open" && latency !== null && (
          <span className="font-mono text-[11.5px] text-muted tabular-nums">
            {formatMs(latency)}
          </span>
        )}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={10}
          className="glass z-50 w-72 animate-rise rounded-2xl p-4 text-[12.5px]"
        >
          <div className="font-semibold text-[13px] text-ink">{view.label}</div>
          <p className="mt-1 text-ink-2 leading-relaxed">{view.detail}</p>
          <ConnectionFacts />
          {view.canRetry && (
            <button
              type="button"
              onClick={() => {
                if (status.state === "blocked" && status.code === 4009) {
                  useUi.getState().openPanel("sessions");
                }
                getRuntime()?.live.retryNow();
              }}
              className="mt-3 flex h-8 w-full items-center justify-center gap-2 rounded-lg bg-accent font-medium text-[12.5px] text-accent-ink hover:bg-accent-strong"
            >
              <RefreshCw className="size-3.5" />
              Reconnect now
            </button>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function ConnectionFacts() {
  const latency = useLive((s) => s.latencyMs);
  const sessionId = useLive((s) => s.sessionId);
  const replica = useLive((s) => s.replica);
  const lastSeq = useLive((s) => s.lastSeq);
  const offset = useLive((s) => s.clockOffsetMs);
  const rows: [string, string][] = [
    ["Round trip", latency === null ? "–" : formatMs(latency)],
    ["Last event", lastSeq === null ? "none yet" : `#${lastSeq}`],
    [
      "Clock offset",
      Math.abs(offset) < 1 ? "<1 ms" : `${offset > 0 ? "+" : "−"}${formatMs(Math.abs(offset))}`,
    ],
    ["Session", sessionId ?? "–"],
    ["Served by", replica ?? "–"],
  ];
  return (
    <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 rounded-xl bg-surface-2/70 p-3 ring-1 ring-line ring-inset">
      {rows.map(([term, value]) => (
        <div key={term} className="contents">
          <dt className="text-muted">{term}</dt>
          <dd className="truncate text-right font-mono text-ink tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
