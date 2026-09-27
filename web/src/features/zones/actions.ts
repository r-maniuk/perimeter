/** User actions on zones: create from a drawing, edit via handles, delete. */
import { createZone, deleteZone as deleteZoneRequest, getZone } from "@/api/endpoints";
import { describeError, isApiError, retryDelayMs } from "@/api/http";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { getRuntime } from "@/app/runtime";
import { notify } from "@/features/shell/notices";
import { useUi } from "@/state/ui";
import {
  applyPatch,
  DRAFT_PREFIX,
  isDraft,
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
/** Creations on their way, by draft id: each settles with the saved zone, or null if it failed. */
const creations = new Map<string, Promise<Zone | null>>();
/** Drafts deleted before the server had them: the zone their creation makes is deleted in turn. */
const abandoned = new Set<string>();

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
  const creation = save(draft);
  creations.set(draftId, creation);
  await creation;
  creations.delete(draftId);
}

/**
 * Send a zone write until the server takes it. A 429 (too many changes) only puts it off: it goes
 * out again when the server says it may — unless meanwhile it stopped being `wanted`, or the
 * session that made it ended (signed out, or another account signed in). Resolves with the
 * answer, or with `null` for a write given up; any other failure rejects.
 */
async function whenAllowed<T>(
  send: () => Promise<T>,
  wanted: () => boolean = () => true,
): Promise<{ answer: T } | null> {
  const runtime = getRuntime();
  for (;;) {
    try {
      return { answer: await send() };
    } catch (error) {
      const wait = retryDelayMs(error);
      if (wait === null) throw error;
      await new Promise((resolve) => setTimeout(resolve, wait));
      if (!wanted() || getRuntime() !== runtime) return null;
    }
  }
}

/** Create the drawn zone; it takes the draft's place, unless the draft was deleted meanwhile. */
async function save(draft: Zone): Promise<Zone | null> {
  let created: { answer: Zone } | null;
  try {
    created = await whenAllowed(
      () =>
        createZone({
          name: draft.name,
          center: draft.center,
          radius_m: draft.radius_m,
          color: draft.color,
          is_active: true,
          notify_enter: true,
          notify_exit: true,
          dwell_s: null,
        }),
      // A draft deleted while its creation waits needs no zone.
      () => !abandoned.has(draft.id),
    );
  } catch (error) {
    removeZone(queryClient, draft.id);
    getRuntime()?.patcher.discard(draft.id);
    deselect(draft.id);
    // A draft deleted meanwhile has what its user wanted: no zone.
    if (!abandoned.has(draft.id)) {
      notify({ tone: "error", title: "Couldn't create the zone", body: describeError(error) });
    }
    return null;
  }
  if (!created) return null;
  const saved = created.answer;
  const runtime = getRuntime();
  runtime?.patcher.saw(saved);
  if (abandoned.has(draft.id)) return saved;
  const edited = readZones(queryClient).find((z) => z.id === draft.id);
  // Edits made to the draft meanwhile stay in view while they are sent against the saved zone.
  const overlay = runtime?.patcher.overlay(draft.id);
  const shown = overlay ? applyPatch(saved, overlay) : saved;
  writeZones(queryClient, (list) =>
    list.filter((z) => z.id !== draft.id && z.id !== saved.id).concat(shown),
  );
  const selection = useUi.getState().selection;
  if (selection?.kind === "zone" && selection.id === draft.id) {
    useUi.getState().select({ kind: "zone", id: saved.id });
  }
  if (edited && runtime) runtime.patcher.rekey(draft.id, saved);
  return saved;
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

/** Delete a zone: it leaves the map at once, and comes back only if the server refuses. */
export async function deleteZone(zone: Zone): Promise<void> {
  if (isDraft(zone.id)) {
    await deleteDraft(zone.id);
    return;
  }
  await getRuntime()?.patcher.settled(zone.id);
  const latest = readZones(queryClient).find((z) => z.id === zone.id) ?? zone;
  removeZone(queryClient, zone.id);
  deselect(zone.id);
  await deleteSaved(latest);
}

/**
 * Delete a zone the server has not confirmed yet. It leaves the map now; once its creation has
 * returned, the zone it made is deleted in turn — it would otherwise appear moments later.
 */
async function deleteDraft(draftId: string): Promise<void> {
  removeZone(queryClient, draftId);
  getRuntime()?.patcher.discard(draftId);
  deselect(draftId);
  const creation = creations.get(draftId);
  if (!creation) return;
  abandoned.add(draftId);
  const saved = await creation;
  abandoned.delete(draftId);
  if (!saved) return;
  // Its creation may have been announced, and shown, in the meantime.
  removeZone(queryClient, saved.id);
  await deleteSaved(saved);
}

async function deleteSaved(zone: Zone): Promise<void> {
  const patcher = getRuntime()?.patcher;
  patcher?.deleting(zone.id);
  try {
    if (await whenAllowed(() => deleteZoneRequest(zone.id, zone.version))) patcher?.bury(zone.id);
  } catch (error) {
    if (isApiError(error, 404)) {
      patcher?.bury(zone.id);
      return;
    }
    await reinstate(zone, error);
  }
}

/**
 * A deletion did not go through: show the zone again. After a conflict that is the version
 * another session saved meanwhile, which is what the user gets to review before deleting again.
 */
async function reinstate(zone: Zone, error: unknown): Promise<void> {
  const conflict = isApiError(error, 412);
  let shown = zone;
  if (conflict) {
    try {
      shown = await getZone(zone.id);
    } catch (readError) {
      if (isApiError(readError, 404)) {
        // Deleted by someone else in the meantime: what this user wanted too.
        getRuntime()?.patcher.bury(zone.id);
        return;
      }
    }
  }
  const patcher = getRuntime()?.patcher;
  if (!patcher?.undelete(shown)) return;
  upsertZone(queryClient, shown);
  if (conflict && useUi.getState().selection === null) {
    useUi.getState().select({ kind: "zone", id: shown.id });
  }
  notify({
    tone: conflict ? "warning" : "error",
    title: conflict ? "Zone changed in another session" : "Couldn't delete the zone",
    body: conflict ? "Review the latest version, then delete again." : describeError(error),
  });
}

function deselect(zoneId: string): void {
  const selection = useUi.getState().selection;
  if (selection?.kind === "zone" && selection.id === zoneId) useUi.getState().select(null);
}
