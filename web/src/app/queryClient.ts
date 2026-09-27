import { QueryClient } from "@tanstack/react-query";
import { isApiError } from "@/api/http";

/**
 * Server state cache. Zones and sessions are kept current by live events, so nothing refetches
 * on focus; queries retry transient failures but never a 4xx.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: (failures, error) =>
        failures < 3 && !(isApiError(error) && error.status >= 400 && error.status < 500),
    },
    mutations: { retry: false },
  },
});
