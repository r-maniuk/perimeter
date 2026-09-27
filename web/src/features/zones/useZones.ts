import { type QueryClient, queryOptions, useQuery } from "@tanstack/react-query";
import { listZones } from "@/api/endpoints";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { getRuntime } from "@/app/runtime";
import { applyPatch, isDraft, readZones, ZONES_KEY } from "./model";

/**
 * A fresh list from the server, with this tab's unsaved work kept on top: zones still being
 * created and edits still in flight would otherwise blink out when a refetch lands. Events that
 * overtook the list win: a zone changed again or deleted meanwhile is not set back by it.
 *
 * The list shows the zones as they were when the server read them, so it only proves a zone gone
 * if the tab knew of it before asking. One the tab first saw while the list was on its way — its
 * own creation answered, another session's announced — stays: the ledger has seen its version
 * already, and nothing would bring it back.
 */
async function fetchZones(client: QueryClient, signal: AbortSignal): Promise<Zone[]> {
  const asking = getRuntime()?.patcher;
  const asked = asking?.stamp();
  const server = await listZones(signal);
  const patcher = getRuntime()?.patcher;
  const cached = new Map(readZones(client).map((zone) => [zone.id, zone]));
  const listed = new Set(server.map((zone) => zone.id));
  // (A ledger that took over meanwhile belongs to another sign-in: it has nothing to keep.)
  const newer =
    patcher && patcher === asking && asked !== undefined
      ? [...cached.values()].filter(
          (zone) => !isDraft(zone.id) && !listed.has(zone.id) && patcher.seenSince(zone.id, asked),
        )
      : [];
  const merged: Zone[] = [];
  for (const zone of server) {
    if (patcher && !patcher.isCurrent(zone)) {
      // The count is still the list's: it is the freshest one.
      const newer = cached.get(zone.id);
      if (newer) merged.push({ ...newer, occupancy: zone.occupancy ?? newer.occupancy });
      continue;
    }
    patcher?.saw(zone);
    const overlay = patcher?.overlay(zone.id);
    merged.push(overlay ? applyPatch(zone, overlay) : zone);
  }
  const drafts = [...cached.values()].filter((z) => isDraft(z.id));
  return [...drafts, ...newer, ...merged];
}

export const zonesQuery = queryOptions({
  queryKey: ZONES_KEY,
  queryFn: ({ signal }) => fetchZones(queryClient, signal),
  // Live events keep zones current; the periodic refresh corrects occupancy counts, which also
  // change without an alert when a zone does not notify on enter or exit.
  refetchInterval: 20_000,
  refetchIntervalInBackground: false,
});

export function useZones() {
  return useQuery(zonesQuery);
}

export function useZone(id: string | null | undefined): Zone | undefined {
  const { data } = useZones();
  return id ? data?.find((z) => z.id === id) : undefined;
}
