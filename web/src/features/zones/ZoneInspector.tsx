import { useQuery } from "@tanstack/react-query";
import { Check, Copy, Crosshair, Trash2, Users } from "lucide-react";
import { AlertDialog } from "radix-ui";
import { type ReactNode, useId, useState } from "react";
import { type ZonePatch, zoneOccupants } from "@/api/endpoints";
import type { Zone } from "@/api/schemas";
import { getRuntime } from "@/app/runtime";
import { PanelFrame } from "@/features/shell/PanelFrame";
import {
  formatAge,
  formatCoordinate,
  formatCount,
  formatDistance,
  formatDuration,
} from "@/lib/format";
import { DESKTOP, useMediaQuery } from "@/lib/useMediaQuery";
import { useNow } from "@/lib/useNow";
import { mapController } from "@/map/controller";
import { useLive } from "@/state/live";
import { usePointer } from "@/state/pointer";
import { useUi } from "@/state/ui";
import { Button } from "@/ui/Button";
import { ConfirmDialog } from "@/ui/ConfirmDialog";
import { cx } from "@/ui/cx";
import { IconButton } from "@/ui/IconButton";
import { SectionLabel } from "@/ui/SectionLabel";
import { Slider } from "@/ui/Slider";
import { Switch } from "@/ui/Switch";
import { deleteZone } from "./actions";
import { CoordinateField, RadiusField, ZoneNameField } from "./fields";
import {
  clampRadius,
  DWELL_PRESETS,
  isDraft,
  RADIUS_MAX_M,
  RADIUS_MIN_M,
  ZONE_SWATCHES,
} from "./model";

/** The slider moves on a log scale: 10 m and 100 km are both a comfortable drag apart. */
const LOG_MIN = Math.log10(RADIUS_MIN_M);
const LOG_MAX = Math.log10(RADIUS_MAX_M);
const toSlider = (radius: number) => ((Math.log10(radius) - LOG_MIN) / (LOG_MAX - LOG_MIN)) * 1000;
const fromSlider = (value: number) => {
  const raw = 10 ** (LOG_MIN + (value / 1000) * (LOG_MAX - LOG_MIN));
  const magnitude = 10 ** Math.max(0, Math.floor(Math.log10(raw)) - 1);
  return clampRadius(Math.round(raw / magnitude) * magnitude);
};

function patch(zone: Zone, change: ZonePatch) {
  getRuntime()?.patcher.patch(zone.id, change);
}

export function ZoneInspector({ zone, onClose }: { zone: Zone; onClose: () => void }) {
  const editing = usePointer((s) => (s.editing?.id === zone.id ? s.editing : null));
  const [sliderRadius, setSliderRadius] = useState<number | null>(null);
  const liveRadius = sliderRadius ?? editing?.radiusM ?? zone.radius_m;
  const liveCenter = editing?.center ?? zone.center;
  const now = useNow(5_000);
  const desktop = useMediaQuery(DESKTOP);

  return (
    <PanelFrame
      title={
        <ZoneNameField
          name={zone.name}
          color={zone.color}
          onCommit={(name) => patch(zone, { name })}
        />
      }
      subtitle={
        isDraft(zone.id)
          ? "Creating…"
          : `Updated ${formatAge(now - Date.parse(zone.updated_at))} · version ${zone.version}`
      }
      onClose={onClose}
      closeLabel="Close zone"
      actions={
        <IconButton
          label="Zoom to zone"
          size="sm"
          side="bottom"
          onClick={() => mapController.fitZones([zone])}
        >
          <Crosshair className="size-4" />
        </IconButton>
      }
      bodyClassName="px-5 pb-5"
    >
      <Occupancy zone={zone} />

      <SectionLabel>Shape</SectionLabel>
      <div className="rounded-2xl bg-surface-2/70 p-3.5 ring-1 ring-line ring-inset">
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium text-[13px] text-ink">Radius</span>
          <RadiusField
            radius={liveRadius}
            onCommit={(radius) => patch(zone, { radius_m: radius })}
          />
        </div>
        <div className="mt-2.5">
          <Slider
            label="Radius"
            min={0}
            max={1000}
            value={toSlider(liveRadius)}
            valueText={formatDistance(liveRadius)}
            onValueChange={(value) => {
              const radius = fromSlider(value);
              setSliderRadius(radius);
              mapController.previewZone({ ...zone, radius_m: radius });
            }}
            onValueCommit={(value) => {
              const radius = fromSlider(value);
              setSliderRadius(null);
              mapController.previewZone(null);
              patch(zone, { radius_m: radius });
            }}
          />
          <div className="mt-1 flex justify-between font-mono text-[10.5px] text-muted">
            <span>10 m</span>
            <span>1 km</span>
            <span>100 km</span>
          </div>
        </div>
        <div className="mt-3 border-line border-t pt-3">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium text-[13px] text-ink">Centre</span>
            <CopyCoordinates lat={liveCenter.lat} lon={liveCenter.lon} />
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <CoordinateField
              axis="lat"
              center={liveCenter}
              onCommit={(center) => patch(zone, { center })}
            />
            <CoordinateField
              axis="lon"
              center={liveCenter}
              onCommit={(center) => patch(zone, { center })}
            />
          </div>
        </div>
        <p className="mt-2 text-[11.5px] text-muted leading-relaxed">
          {desktop
            ? "Drag the centre or the edge handle on the map, or focus one and nudge it with the arrow keys (hold Shift for bigger steps)."
            : "Drag the centre or the edge handle on the map to reshape it."}
        </p>
      </div>

      <SectionLabel>Colour</SectionLabel>
      <Swatches value={zone.color} onChange={(color) => patch(zone, { color })} />

      <SectionLabel>Monitoring</SectionLabel>
      <div className="rounded-2xl bg-surface-2/70 px-3.5 py-1.5 ring-1 ring-line ring-inset">
        <Switch
          label="Active"
          description={zone.is_active ? "Tracking who is inside" : "Paused — presence is cleared"}
          checked={zone.is_active}
          onCheckedChange={(is_active) => patch(zone, { is_active })}
        />
        <div className="h-px bg-line" />
        <Switch
          label="Alert on enter"
          checked={zone.notify_enter}
          onCheckedChange={(notify_enter) => patch(zone, { notify_enter })}
          disabled={!zone.is_active}
        />
        <Switch
          label="Alert on exit"
          checked={zone.notify_exit}
          onCheckedChange={(notify_exit) => patch(zone, { notify_exit })}
          disabled={!zone.is_active}
        />
        <div className="h-px bg-line" />
        <DwellPicker zone={zone} />
      </div>

      <div className="mt-6 flex items-center justify-between">
        <span className="text-[11.5px] text-muted">
          {isDraft(zone.id)
            ? ""
            : `Created ${new Date(zone.created_at).toLocaleDateString("en-GB")}`}
        </span>
        <DeleteZone zone={zone} />
      </div>
    </PanelFrame>
  );
}

function Swatches({ value, onChange }: { value: string; onChange: (color: string) => void }) {
  return (
    <div role="radiogroup" aria-label="Zone colour" className="grid grid-cols-8 gap-1.5">
      {ZONE_SWATCHES.map((swatch) => {
        const checked = swatch.color === value.toLowerCase();
        return (
          // biome-ignore lint/a11y/useSemanticElements: a row of colour chips, not native radio inputs.
          <button
            key={swatch.color}
            type="button"
            role="radio"
            aria-checked={checked}
            aria-label={swatch.name}
            title={swatch.name}
            onClick={() => onChange(swatch.color)}
            className={cx(
              "flex aspect-square items-center justify-center rounded-full transition-transform duration-150 hover:scale-110",
              checked && "ring-2 ring-offset-2 ring-offset-[var(--surface-solid)]",
            )}
            style={{ background: swatch.color, ["--tw-ring-color" as string]: swatch.color }}
          >
            {checked && <Check className="size-3.5 text-white" strokeWidth={3} />}
          </button>
        );
      })}
    </div>
  );
}

function DwellPicker({ zone }: { zone: Zone }) {
  const inputId = useId();
  const preset = DWELL_PRESETS.find((p) => p.value === zone.dwell_s);
  const [custom, setCustom] = useState(preset === undefined);
  const [minutes, setMinutes] = useState(zone.dwell_s ? String(zone.dwell_s / 60) : "30");

  function commitCustom() {
    const value = Number(minutes.replace(",", "."));
    if (!Number.isFinite(value) || value <= 0) return;
    const seconds = Math.min(86_400, Math.max(10, Math.round(value * 60)));
    setMinutes(String(Math.round((seconds / 60) * 100) / 100));
    if (seconds !== zone.dwell_s) patch(zone, { dwell_s: seconds });
  }

  return (
    <div className="py-2">
      <div className="flex items-center justify-between">
        <span className="font-medium text-[13px] text-ink">Dwell alert</span>
        <span className="text-[12px] text-muted">
          {zone.dwell_s ? `after ${formatDuration(zone.dwell_s)} inside` : "Off"}
        </span>
      </div>
      <div
        role="radiogroup"
        aria-label="Dwell alert"
        className="mt-2 grid grid-cols-5 gap-1 rounded-xl bg-surface-3/60 p-1"
      >
        {DWELL_PRESETS.map((option) => (
          <Segment
            key={option.label}
            checked={!custom && zone.dwell_s === option.value}
            disabled={!zone.is_active}
            onSelect={() => {
              setCustom(false);
              if (zone.dwell_s !== option.value) patch(zone, { dwell_s: option.value });
            }}
          >
            {option.label}
          </Segment>
        ))}
        <Segment checked={custom} disabled={!zone.is_active} onSelect={() => setCustom(true)}>
          Custom
        </Segment>
      </div>
      {custom && (
        <div className="mt-2.5 flex items-center gap-2">
          <label htmlFor={inputId} className="text-[12px] text-muted">
            Alert after
          </label>
          <input
            id={inputId}
            inputMode="decimal"
            value={minutes}
            onChange={(e) => setMinutes(e.target.value)}
            onBlur={commitCustom}
            onKeyDown={(e) => e.key === "Enter" && commitCustom()}
            className="h-8 w-20 rounded-lg bg-surface-solid px-2.5 text-right font-mono text-[13px] text-ink outline-none ring-1 ring-line-strong focus:ring-2 focus:ring-accent"
          />
          <span className="text-[12px] text-muted">minutes inside</span>
        </div>
      )}
    </div>
  );
}

function Segment({
  checked,
  disabled,
  onSelect,
  children,
}: {
  checked: boolean;
  disabled?: boolean;
  onSelect: () => void;
  children: ReactNode;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: segmented control built from buttons.
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      disabled={disabled}
      onClick={onSelect}
      className={cx(
        "h-7 rounded-lg font-medium text-[11.5px] transition-colors disabled:opacity-40",
        checked
          ? "bg-surface-solid text-ink shadow-[0_1px_2px_rgb(0_0_0/0.12)]"
          : "text-muted hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

function Occupancy({ zone }: { zone: Zone }) {
  const reporting = useLive((s) => s.reporting[zone.id] ?? 0);
  const draft = isDraft(zone.id);
  const { data: inside, isPending } = useQuery({
    queryKey: ["occupants", zone.id],
    queryFn: ({ signal }) => zoneOccupants(zone.id, signal),
    enabled: !draft,
    refetchInterval: 10_000,
    staleTime: 2_000,
  });
  const now = useNow(5_000);
  const occupants = inside?.items;

  return (
    <div className="mt-1">
      <div className="flex items-stretch gap-2">
        <Stat label="Inside now" value={formatCount(zone.occupancy ?? inside?.occupancy ?? 0)} />
        <Stat
          label="Reporting"
          value={formatCount(reporting)}
          hint="last 6 s"
          live={reporting > 0 && zone.is_active}
        />
      </div>
      <SectionLabel
        aside={
          inside && inside.occupancy > 0 ? (
            <span className="text-[11px] text-muted tabular-nums">
              {inside.items.length < inside.occupancy
                ? `${inside.items.length} of ${formatCount(inside.occupancy)}`
                : formatCount(inside.occupancy)}
            </span>
          ) : undefined
        }
      >
        Occupants
      </SectionLabel>
      {draft || isPending ? (
        <div className="h-12 animate-pulse rounded-xl bg-surface-2" />
      ) : occupants && occupants.length > 0 ? (
        <ul className="scroll-quiet max-h-44 overflow-y-auto rounded-xl ring-1 ring-line ring-inset">
          {occupants.slice(0, 100).map((o) => (
            <li key={o.device_id}>
              <button
                type="button"
                onClick={() => {
                  useUi.getState().select({ kind: "device", id: o.device_id });
                  if (o.position) useUi.getState().flyTo(o.position.lat, o.position.lon, 16);
                }}
                className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-surface-3/60"
              >
                <span className="truncate font-mono text-[12.5px] text-ink">{o.device_id}</span>
                <span className="shrink-0 text-[11.5px] text-muted">
                  {o.entered_at ? enteredLabel(now - Date.parse(o.entered_at)) : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <div className="flex items-center gap-2.5 rounded-xl bg-surface-2/70 px-3 py-3 text-[12.5px] text-muted ring-1 ring-line ring-inset">
          <Users className="size-4" />
          {zone.is_active ? "Nobody inside right now." : "Paused zones track nobody."}
        </div>
      )}
    </div>
  );
}

function enteredLabel(ms: number): string {
  const age = formatAge(ms);
  return age === "now" ? "just entered" : `entered ${age}`;
}

function Stat({
  label,
  value,
  hint,
  live,
}: {
  label: string;
  value: string;
  hint?: string;
  live?: boolean;
}) {
  return (
    <div className="flex-1 rounded-2xl bg-surface-2/70 px-3.5 py-3 ring-1 ring-line ring-inset">
      <div className="flex items-center gap-1.5 text-[11.5px] text-muted">
        {live !== undefined && (
          <span
            className={cx(
              "size-1.5 rounded-full",
              live ? "animate-breathe bg-enter" : "bg-line-strong",
            )}
          />
        )}
        {label}
        {hint && <span className="text-muted/70">· {hint}</span>}
      </div>
      <div className="mt-1 font-semibold text-[22px] text-ink leading-none tracking-[-0.02em]">
        {value}
      </div>
    </div>
  );
}

function CopyCoordinates({ lat, lon }: { lat: number; lon: number }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(`${lat.toFixed(6)}, ${lon.toFixed(6)}`).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1_400);
        });
      }}
      className="flex items-center gap-1.5 rounded-md px-1.5 py-0.5 text-[11.5px] text-muted hover:bg-surface-3 hover:text-ink"
      aria-label={`Copy centre coordinates, ${formatCoordinate(lat, lon)}`}
    >
      {copied ? "Copied" : "Copy"}
      {copied ? <Check className="size-3.5 text-enter" /> : <Copy className="size-3.5" />}
    </button>
  );
}

function DeleteZone({ zone }: { zone: Zone }) {
  return (
    <AlertDialog.Root>
      <AlertDialog.Trigger asChild>
        <Button
          size="sm"
          variant="ghost"
          icon={<Trash2 className="size-3.5" />}
          className="text-critical hover:bg-critical/10 hover:text-critical"
        >
          Delete zone
        </Button>
      </AlertDialog.Trigger>
      <ConfirmDialog
        title={`Delete “${zone.name}”?`}
        body="Monitoring stops immediately. Alerts it already raised stay in the history; devices inside will not get an exit alert."
        confirm="Delete zone"
        cancel="Keep it"
        onConfirm={() => void deleteZone(zone)}
      />
    </AlertDialog.Root>
  );
}
