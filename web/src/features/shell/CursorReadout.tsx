import { formatCoordinate } from "@/lib/format";
import { usePointer } from "@/state/pointer";

/** Coordinates under the pointer, next to the scale bar. */
export function CursorReadout() {
  const cursor = usePointer((s) => s.cursor);
  if (!cursor) return null;
  return (
    <div className="pointer-events-none fixed bottom-[11px] left-[196px] z-10 rounded-md bg-surface px-2 py-0.5 font-mono text-[10.5px] text-ink-2 tabular-nums backdrop-blur-md transition-[left] duration-300 ease-out-quint [[data-panel]_&]:left-[584px]">
      {formatCoordinate(cursor.lat, cursor.lon)}
    </div>
  );
}
