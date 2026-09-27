/** User actions on zones: create from a drawing, edit via handles, delete. */
import { createZone, deleteZone as deleteZoneRequest } from "@/api/endpoints";
import { describeError, isApiError } from "@/api/http";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { getRuntime } from "@/app/runtime";
import { notify } from "@/features/shell/notices";
import { useUi } from "@/state/ui";
import {
  DRAFT_PREFIX,
  nextSwatch,
  nextZoneName,
  readZones,
  removeZone,
  upsertZone,
  writeZones,
  ZONES_KEY,
} from "./model";
import { zonesQuery } from "./useZones";

let draftCounter = 0;

/** A zone was drawn: show it at once, save it, and hand edits made meanwhile to the saved zone. */
export async function createDrawnZone(shape: { lat: number; lon: number; radiusM: number }) {
  // The list is normally loaded long before anyone draws; if not, load it first so the new zone
  // joins a complete list.
  if (queryClient.getQueryData(ZONES_KEY) === undefined) {
    await queryClient.fetchQuery(zonesQuery).catch(() => undefined);
  }
  const zones = readZones(queryClient);
  draftCounter += 1;
  const draftId = `${DRAFT_PREFIX}${draftCounter}`;
  const now = new Date().toISOString();
  const draft: Zone = {
    id: draftId,
    name: nextZoneName(zones),
    color: nextSwatch(zones),
    center: { lat: shape.lat, lon: shape.lon },
    radius_m: Math.round(shape.radiusM),
    is_active: true,
    notify_enter: true,
    notify_exit: true,
    dwell_s: null,
    version: 0,
    created_at: now,
    updated_at: now,
    occupancy: 0,
  };
  upsertZone(queryClient, draft);
  const ui = useUi.getState();
  ui.setDrawing(false);
  ui.select({ kind: "zone", id: draftId });
  try {
    const saved = await createZone({
      name: draft.name,
      center: draft.center,
      radius_m: draft.radius_m,
      color: draft.color,
      is_active: true,
      notify_enter: true,
      notify_exit: true,
      dwell_s: null,
    });
    const edited = readZones(queryClient).find((z) => z.id === draftId);
    writeZones(queryClient, (list) =>
      list.filter((z) => z.id !== draftId && z.id !== saved.id).concat(saved),
    );
    const selection = useUi.getState().selection;
    if (selection?.kind === "zone" && selection.id === draftId) {
      useUi.getState().select({ kind: "zone", id: saved.id });
    }
    const runtime = getRuntime();
    if (edited && runtime) {
      runtime.patcher.rekey(draftId, saved.id);
    }
  } catch (error) {
    removeZone(queryClient, draftId);
    getRuntime()?.patcher.discard(draftId);
    const selection = useUi.getState().selection;
    if (selection?.kind === "zone" && selection.id === draftId) useUi.getState().select(null);
    notify({ tone: "error", title: "Couldn't create the zone", body: describeError(error) });
  }
}

/** Handle drag on the map finished: persist the new centre or radius. */
export function commitZoneEdit(edit: {
  id: string;
  center?: { lat: number; lon: number };
  radiusM?: number;
}) {
  const runtime = getRuntime();
  if (!runtime) return;
  if (edit.center) runtime.patcher.patch(edit.id, { center: edit.center });
  if (edit.radiusM !== undefined)
    runtime.patcher.patch(edit.id, { radius_m: Math.round(edit.radiusM) });
}

export async function deleteZone(zone: Zone): Promise<void> {
  const runtime = getRuntime();
  await runtime?.patcher.settled(zone.id);
  const latest = readZones(queryClient).find((z) => z.id === zone.id) ?? zone;
  removeZone(queryClient, zone.id);
  const selection = useUi.getState().selection;
  if (selection?.kind === "zone" && selection.id === zone.id) useUi.getState().select(null);
  if (latest.id.startsWith(DRAFT_PREFIX)) return;
  try {
    await deleteZoneRequest(latest.id, latest.version);
  } catch (error) {
    if (isApiError(error, 404)) return;
    upsertZone(queryClient, latest);
    notify({
      tone: isApiError(error, 412) ? "warning" : "error",
      title: isApiError(error, 412)
        ? "Zone changed in another session"
        : "Couldn't delete the zone",
      body: isApiError(error, 412)
        ? "Review the latest version, then delete again."
        : describeError(error),
    });
  }
}
