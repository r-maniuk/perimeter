import { Activity, CircleAlert, CircleCheck, Cpu, Server } from "lucide-react";
import type { ReactNode } from "react";
import type { OpsFrame } from "@/api/schemas";
import { PanelFrame } from "@/features/shell/PanelFrame";
import type { PanelProps } from "@/features/shell/panels";
import { contrast, hexToRgba } from "@/lib/color";
import { formatCount, formatMs, formatRate } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { useLive } from "@/state/live";
import { useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { Empty } from "@/ui/Empty";
import { Hint } from "@/ui/Hint";
import { SectionLabel } from "@/ui/SectionLabel";
import { Sparkline } from "@/ui/Sparkline";
import { admission, instances, ownership } from "./model";

/** Engine owner colours: validated for colour-vision separation and contrast, per theme. */
const OWNER_COLORS = {
  light: ["#6d5dfc", "#0b91a8", "#c77700"],
  dark: ["#8b7fff", "#1aa3b8", "#c98500"],
} as const;

function labelInk(fill: string): string {
  const color = hexToRgba(fill);
  return contrast(color, hexToRgba("#ffffff")) >= contrast(color, hexToRgba("#16151d"))
    ? "#ffffff"
    : "#16151d";
}

export function OpsPanel({ onClose, titleId }: PanelProps) {
  const ops = useLive((s) => s.ops);
  const history = useLive((s) => s.history);
  const now = useNow(1_000);

  return (
    <PanelFrame
      title="Pipeline"
      titleId={titleId}
      subtitle={
        ops
          ? `Live from ${ops.frame.services.length} processes · ${Math.max(0, Math.round((now - ops.at) / 1000))} s ago`
          : "Waiting for the first heartbeat…"
      }
      onClose={onClose}
      bodyClassName="px-4 pb-5"
    >
      {!ops ? (
        <Empty icon={<Activity className="size-5" />} title="Listening for metrics">
          Every api and engine process reports once a second. Numbers appear with the first beat.
        </Empty>
      ) : (
        <OpsBody frame={ops.frame} history={history} />
      )}
    </PanelFrame>
  );
}

function OpsBody({ frame, history }: { frame: OpsFrame; history: Record<string, number[]> }) {
  const dark = useUi((s) => s.dark);
  const last = (key: string) => history[key]?.at(-1) ?? 0;
  const state = admission(frame);
  const owners = ownership(frame);
  const palette = OWNER_COLORS[dark ? "dark" : "light"];
  const colorOf = (instance: string | null) => {
    if (!instance) return null;
    const slot = owners.engines.find((e) => e.instance === instance)?.slot ?? 99;
    return palette[slot] ?? (dark ? "#6b6a78" : "#9a98a8");
  };
  const procs = instances(frame, Date.now() / 1000);

  return (
    <>
      <SectionLabel>Throughput</SectionLabel>
      <div className="grid grid-cols-2 gap-2">
        <StatTile label="Ingest" value={formatRate(last("ingest"))} series={history.ingest} />
        <StatTile label="Processed" value={formatRate(last("reports"))} series={history.reports} />
        <StatTile label="Alerts" value={formatRate(last("alerts"))} series={history.alerts} />
        <StatTile label="Live out" value={formatRate(last("wsOut"))} series={history.wsOut} />
      </div>

      <SectionLabel>Backpressure</SectionLabel>
      <div className="rounded-2xl bg-surface-2/70 p-3.5 ring-1 ring-line ring-inset">
        <div className="flex items-center justify-between">
          <span className="text-[12.5px] text-ink-2">Admission</span>
          <span
            className={cx(
              "flex items-center gap-1.5 font-semibold text-[12.5px]",
              state === "shedding"
                ? "text-critical"
                : state === "open"
                  ? "text-good"
                  : "text-muted",
            )}
          >
            {state === "shedding" ? (
              <CircleAlert className="size-4" />
            ) : (
              <CircleCheck className="size-4" />
            )}
            {state === "shedding" ? "Shedding load" : state === "open" ? "Open" : "Unknown"}
          </span>
        </div>
        <Row label="Stream backlog" value={`${formatCount(Math.round(last("lag")))} msgs`} />
        <Row label="Rejected reports" value={formatRate(last("rejected"))} />
        <Row label="Outbox backlog" value={`${formatCount(Math.round(last("backlog")))} rows`} />
        <Row label="Live drops" value={formatRate(last("drops"))} />
      </div>

      <SectionLabel>Latency · p99 unless noted</SectionLabel>
      <div className="space-y-2.5 rounded-2xl bg-surface-2/70 p-3.5 ring-1 ring-line ring-inset">
        <Meter label="Batch apply · p50" value={last("batchP50")} max={50} warn={25} />
        <Meter label="Batch apply" value={last("batchP99")} max={100} warn={50} />
        <Meter label="Ingest → commit" value={last("commitLag")} max={1_000} warn={500} />
        <Meter label="Publish ack" value={last("publishP99")} max={50} warn={25} />
      </div>

      <SectionLabel
        aside={
          owners.conflicts.length > 0 ? (
            <span className="text-[11px] text-serious">handover in progress</span>
          ) : undefined
        }
      >
        Partition ownership
      </SectionLabel>
      <div className="rounded-2xl bg-surface-2/70 p-3.5 ring-1 ring-line ring-inset">
        <ul className="grid grid-cols-8 gap-1.5" aria-label="Partitions and their owning engine">
          {owners.owners.map((owner, p) => {
            const fill = colorOf(owner);
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: the index is the partition number itself.
              <li key={p}>
                <Hint label={owner ? `Partition ${p} · ${owner}` : `Partition ${p} · unassigned`}>
                  <span
                    className={cx(
                      "flex aspect-square items-center justify-center rounded-lg font-mono font-semibold text-[11px] transition-colors duration-500",
                      !fill &&
                        "bg-[repeating-linear-gradient(135deg,var(--line-strong)_0_2px,transparent_2px_6px)] text-muted ring-1 ring-line-strong ring-inset",
                      owners.conflicts.includes(p) && "ring-2 ring-serious",
                    )}
                    style={fill ? { background: fill, color: labelInk(fill) } : undefined}
                  >
                    {p}
                  </span>
                </Hint>
              </li>
            );
          })}
        </ul>
        <ul className="mt-3 space-y-1">
          {owners.engines.map((engine) => (
            <li key={engine.instance} className="flex items-center gap-2 text-[12px]">
              <span
                className="size-2.5 rounded-sm"
                style={{ background: colorOf(engine.instance) ?? undefined }}
              />
              <span className="flex-1 truncate font-mono text-ink">{engine.instance}</span>
              <span className="text-muted tabular-nums">
                {engine.count} {engine.count === 1 ? "partition" : "partitions"}
              </span>
            </li>
          ))}
          {owners.owners.some((o) => o === null) && (
            <li className="flex items-center gap-2 text-[12px]">
              <span className="size-2.5 rounded-sm bg-[repeating-linear-gradient(135deg,var(--muted)_0_1.5px,transparent_1.5px_4px)] ring-1 ring-line-strong" />
              <span className="flex-1 text-muted">Unassigned</span>
              <span className="text-muted tabular-nums">
                {owners.owners.filter((o) => o === null).length}
              </span>
            </li>
          )}
        </ul>
      </div>

      <SectionLabel>Processes</SectionLabel>
      <ul className="space-y-1.5">
        {procs.map((p) => (
          <li
            key={`${p.service}:${p.instance}`}
            className="rounded-2xl bg-surface-2/70 px-3.5 py-2.5 ring-1 ring-line ring-inset"
          >
            <div className="flex items-center gap-2.5">
              <span className="flex size-7 items-center justify-center rounded-lg bg-surface-solid text-ink-2 ring-1 ring-line ring-inset">
                {p.service === "engine" ? (
                  <Cpu className="size-3.5" />
                ) : (
                  <Server className="size-3.5" />
                )}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-mono text-[12px] text-ink">{p.instance}</span>
                <span className="block text-[11px] text-muted">
                  {describeProcess(p.service, p.raw)}
                </span>
              </span>
              <span
                className={cx(
                  "flex items-center gap-1 text-[11px] tabular-nums",
                  p.ageS > 3 ? "text-serious" : "text-muted",
                )}
                title="Time since the last heartbeat"
              >
                <span
                  className={cx("size-1.5 rounded-full", p.ageS > 3 ? "bg-serious" : "bg-good")}
                />
                {p.ageS < 1 ? "now" : `${Math.round(p.ageS)} s`}
              </span>
            </div>
            <div className="mt-2">
              <Meter label="Event loop lag" value={p.loopLagMs} max={50} warn={20} compact />
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

function describeProcess(service: string, raw: Record<string, unknown>): string {
  if (service === "engine") {
    const parts = Array.isArray(raw.partitions) ? raw.partitions.length : 0;
    return `${parts} partitions · ${formatRate(Number(raw.batches_rate ?? 0))} batches`;
  }
  const sessions = Number(raw.sessions ?? 0);
  return `${formatCount(sessions)} ${sessions === 1 ? "session" : "sessions"} · db pool ${formatCount(Number(raw.db_pool_checked_out ?? 0))}`;
}

function StatTile({
  label,
  value,
  series,
}: {
  label: string;
  value: string;
  series: number[] | undefined;
}) {
  return (
    <div className="rounded-2xl bg-surface-2/70 px-3.5 pt-3 pb-2.5 ring-1 ring-line ring-inset">
      <div className="text-[11.5px] text-muted">{label}</div>
      <div className="mt-0.5 font-semibold text-[20px] text-ink leading-tight tracking-[-0.02em]">
        {value}
      </div>
      <div className="mt-2 pr-1">
        <Sparkline values={series ?? []} label={`${label}, last minute`} />
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="mt-2 flex items-center justify-between border-line border-t pt-2 text-[12.5px]">
      <span className="text-ink-2">{label}</span>
      <span className="font-medium text-ink tabular-nums">{value}</span>
    </div>
  );
}

/** A value against a scale: accent while healthy, the "serious" status colour past `warn`. */
function Meter({
  label,
  value,
  max,
  warn,
  compact = false,
}: {
  label: string;
  value: number;
  max: number;
  warn: number;
  compact?: boolean;
}) {
  const ratio = Math.min(1, Math.max(0, value / max));
  const slow = value >= warn;
  return (
    <div>
      <div
        className={cx(
          "flex items-center justify-between",
          compact ? "text-[11px]" : "text-[12.5px]",
        )}
      >
        <span className="text-ink-2">{label}</span>
        <span className={cx("font-medium tabular-nums", slow ? "text-serious" : "text-ink")}>
          {formatMs(value)}
          {slow && <span className="ml-1 font-normal text-[11px]">slow</span>}
        </span>
      </div>
      <div
        className={cx(
          "mt-1.5 overflow-hidden rounded-full bg-accent/12",
          compact ? "h-1" : "h-1.5",
        )}
      >
        <div
          className={cx(
            "h-full rounded-full transition-[width] duration-700 ease-out-quint",
            slow ? "bg-serious" : "bg-accent",
          )}
          style={{ width: `${Math.max(2, ratio * 100)}%` }}
        />
      </div>
    </div>
  );
}
