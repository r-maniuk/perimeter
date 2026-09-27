/**
 * Text fields that edit a zone property in place.
 *
 * A field only sends what the user actually typed. While it holds no pending edit it follows the
 * zone — changes made on the map, by another session, or by a conflict that showed the latest
 * version — and focusing or leaving it never re-sends a value. Otherwise a field that kept an
 * old value while focused would quietly write it back on blur, undoing a newer change.
 */
import { useEffect, useRef, useState } from "react";
import { formatDistance, parseDistance } from "@/lib/format";
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
