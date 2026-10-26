import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useNotifications } from "@/hooks/queries";
import * as api from "@/lib/api";

// The pages below are SYNTHETIC test data: only their length matters.

describe("useNotifications", () => {
  afterEach(() => vi.restoreAllMocks());

  it("keeps the dashboard's 5 alerts and the Alerts page's 25 apart in the cache", async () => {
    const get = vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      const limit = Number(new URL(path, "http://test").searchParams.get("limit"));
      return { items: Array.from({ length: limit }, (_, i) => ({ id: String(i) })), total: 40 };
    });
    // One client for both hooks, with the app's 30 s staleTime (components/providers.tsx).
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;

    const dashboard = renderHook(() => useNotifications(0, 5), { wrapper });
    await waitFor(() => expect(dashboard.result.current.data?.items).toHaveLength(5));
    const alertsPage = renderHook(() => useNotifications(0, 25), { wrapper });
    await waitFor(() => expect(alertsPage.result.current.data?.items).toHaveLength(25));

    expect(get.mock.calls.map((c) => c[0])).toEqual(["/api/notifications?limit=5&offset=0", "/api/notifications?limit=25&offset=0"]);
    expect(dashboard.result.current.data?.items).toHaveLength(5);
  });
});
