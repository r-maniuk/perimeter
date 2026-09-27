import { type QueryClient, queryOptions, useQuery } from "@tanstack/react-query";
import { listZones } from "@/api/endpoints";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { getRuntime } from "@/app/runtime";
import { applyPatch, isDraft, readZones, ZONES_KEY } from "./model";

/**
 * A fresh list from the server, with this tab's unsaved work kept on top: zones still being
 * created and edits still in flight would otherwise blink out when a refetch lands.
 */
async function fetchZones(client: QueryClient, signal: AbortSignal): Promise<Zone[]> {
  const server = await listZones(signal);
  const patcher = getRuntime()?.patcher;
  const merged = server.map((zone) => {
    const overlay = patcher?.overlay(zone.id);
    return overlay ? applyPatch(zone, overlay) : zone;
  });
  const drafts = readZones(client).filter((z) => isDraft(z.id));
  return [...drafts, ...merged];
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
