import { formatDistance } from "@/lib/format";
import { usePointer } from "@/state/pointer";

/** The live radius next to the pointer while a zone is being drawn. */
export function DraftLabel() {
  const draft = usePointer((s) => s.draft);
  if (!draft) return null;
  return (
    <div
      className="pointer-events-none fixed z-30 rounded-lg bg-ink px-2 py-1 font-mono font-semibold text-[12px] text-bg tabular-nums shadow-pop"
      style={{ left: draft.x + 16, top: draft.y + 16 }}
    >
      {formatDistance(draft.radiusM)}
    </div>
  );
}
