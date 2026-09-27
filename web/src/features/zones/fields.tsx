/**
 * Text fields that edit a zone property in place.
 *
 * A field only sends what the user actually typed. While it holds no pending edit it follows the
 * zone — changes made on the map, by another session, or by a conflict that showed the latest
 * version — and focusing or leaving it never re-sends a value. Otherwise a field that kept an
 * old value while focused would quietly write it back on blur, undoing a newer change.
 */
import { useEffect, useRef, useState } from "react";
import {
  type Axis,
  formatDistance,
  parseCoordinatePair,
  parseDegrees,
  parseDistance,
} from "@/lib/format";
import { cx } from "@/ui/cx";
import { clampRadius } from "./model";

const NAME_MAX_LENGTH = 80;

export function ZoneNameField({
  name,
  color,
  onCommit,
}: {
  name: string;
  color: string;
  onCommit: (name: string) => void;
}) {
  const [value, setValue] = useState(name);
  const edited = useRef(false);
  const cancelled = useRef(false);

  useEffect(() => {
    if (!edited.current) setValue(name);
  }, [name]);

  function commit() {
    if (!edited.current) return;
    edited.current = false;
    const next = value.trim();
    if (next.length === 0 || next.length > NAME_MAX_LENGTH) {
      setValue(name);
      return;
    }
    setValue(next);
    if (next !== name) onCommit(next);
  }

  return (
    <span className="flex items-center gap-2">
      <span className="size-3 shrink-0 rounded-full" style={{ background: color }} />
      <input
        aria-label="Zone name"
        value={value}
        maxLength={NAME_MAX_LENGTH}
        onChange={(e) => {
          edited.current = true;
          setValue(e.target.value);
        }}
        onBlur={() => {
          if (cancelled.current) {
            cancelled.current = false;
            return;
          }
          commit();
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") {
            // The blur below runs before this render: tell it not to save.
            cancelled.current = true;
            edited.current = false;
            setValue(name);
            e.currentTarget.blur();
            e.stopPropagation();
          }
        }}
        className="-mx-1.5 w-full min-w-0 rounded-md bg-transparent px-1.5 py-0.5 font-semibold text-[15px] text-ink outline-none transition-shadow hover:ring-1 hover:ring-line-strong focus:ring-2 focus:ring-accent"
      />
    </span>
  );
}

export function RadiusField({
  radius,
  onCommit,
}: {
  radius: number;
  onCommit: (radius: number) => void;
}) {
  const [text, setText] = useState(formatDistance(radius));
  const [invalid, setInvalid] = useState(false);
  const edited = useRef(false);
  const cancelled = useRef(false);

  useEffect(() => {
    if (!edited.current) setText(formatDistance(radius));
  }, [radius]);

  function revert() {
    edited.current = false;
    setInvalid(false);
    setText(formatDistance(radius));
  }

  function commit() {
    if (!edited.current) return;
    const parsed = parseDistance(text);
    if (parsed === null) {
      setInvalid(true);
      return;
    }
    edited.current = false;
    setInvalid(false);
    const next = clampRadius(Math.round(parsed));
    setText(formatDistance(next));
    if (next !== Math.round(radius)) onCommit(next);
  }

  return (
    <input
      aria-label="Radius"
      aria-invalid={invalid || undefined}
      inputMode="decimal"
      value={text}
      onChange={(e) => {
        edited.current = true;
        setText(e.target.value);
        setInvalid(false);
      }}
      onFocus={(e) => e.currentTarget.select()}
      onBlur={() => {
        if (cancelled.current) {
          cancelled.current = false;
          return;
        }
        commit();
        // Leaving a field that cannot be read puts the zone's radius back.
        if (edited.current) revert();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          cancelled.current = true;
          revert();
          e.currentTarget.blur();
        }
      }}
      className="h-8 w-28 rounded-lg bg-surface-solid px-2.5 text-right font-mono text-[13px] text-ink tabular-nums outline-none ring-1 ring-line-strong focus:ring-2 focus:ring-accent aria-[invalid]:ring-critical"
    />
  );
}

/** Six decimals of a degree: about ten centimetres, finer than any device reports. */
const DEGREE_DECIMALS = 6;
const SAME_DEGREES = 0.5 * 10 ** -DEGREE_DECIMALS;

const AXIS_LABEL: Record<Axis, { short: string; long: string }> = {
  lat: { short: "Lat", long: "Centre latitude" },
  lon: { short: "Lon", long: "Centre longitude" },
};

/**
 * One coordinate of a zone's centre, in decimal degrees ("52.3731", "52,3731", "4.89° W"). A
 * pair typed or pasted into either field ("52.3731, 4.8926", as maps copy it) moves both at once.
 */
export function CoordinateField({
  axis,
  center,
  onCommit,
}: {
  axis: Axis;
  center: { lat: number; lon: number };
  onCommit: (center: { lat: number; lon: number }) => void;
}) {
  const value = center[axis];
  const [text, setText] = useState(value.toFixed(DEGREE_DECIMALS));
  const [invalid, setInvalid] = useState(false);
  const edited = useRef(false);
  const cancelled = useRef(false);

  useEffect(() => {
    if (!edited.current) setText(value.toFixed(DEGREE_DECIMALS));
  }, [value]);

  function revert() {
    edited.current = false;
    setInvalid(false);
    setText(value.toFixed(DEGREE_DECIMALS));
  }

  function commit() {
    if (!edited.current) return;
    // "52,38" is a latitude with a decimal comma, not a pair: a single value is read first.
    const single = parseDegrees(text, axis);
    const pair = single === null ? parseCoordinatePair(text) : null;
    if (!pair && single === null) {
      setInvalid(true);
      return;
    }
    edited.current = false;
    setInvalid(false);
    const next = pair ?? { ...center, [axis]: single };
    setText(next[axis].toFixed(DEGREE_DECIMALS));
    const moved =
      Math.abs(next.lat - center.lat) >= SAME_DEGREES ||
      Math.abs(next.lon - center.lon) >= SAME_DEGREES;
    if (moved) onCommit(next);
  }

  return (
    <label
      className={cx(
        "flex h-8 min-w-0 items-center gap-2 rounded-lg bg-surface-solid pr-2.5 pl-2 ring-1 ring-line-strong focus-within:ring-2 focus-within:ring-accent",
        invalid && "ring-critical focus-within:ring-critical",
      )}
    >
      <span className="font-medium text-[11px] text-muted" aria-hidden="true">
        {AXIS_LABEL[axis].short}
      </span>
      <input
        aria-label={AXIS_LABEL[axis].long}
        aria-invalid={invalid || undefined}
        inputMode="decimal"
        autoComplete="off"
        spellCheck={false}
        value={text}
        onChange={(e) => {
          edited.current = true;
          setText(e.target.value);
          setInvalid(false);
        }}
        onFocus={(e) => e.currentTarget.select()}
        onBlur={() => {
          if (cancelled.current) {
            cancelled.current = false;
            return;
          }
          commit();
          // Leaving a field that cannot be read puts the zone's coordinate back.
          if (edited.current) revert();
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") {
            cancelled.current = true;
            revert();
            e.currentTarget.blur();
          }
        }}
        className="w-full min-w-0 bg-transparent text-right font-mono text-[12.5px] text-ink tabular-nums outline-none"
      />
    </label>
  );
}
