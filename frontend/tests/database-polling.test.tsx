import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type DataHealth, useDataHealth } from "@/app/status/data-health";
import { useOpportunities } from "@/hooks/queries";
import * as api from "@/lib/api";

// Reads that reach the database (no-store, or a CDN window shorter than Neon's 5-minute
// suspend delay) must not run on a timer while a page merely stays visible: one open tab would
// keep a scale-to-zero database awake. The reports below are SYNTHETIC test data.

function wrapper() {
  // The app's defaults (components/providers.tsx).
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function healthReport(lastRunStatus: "success" | "running"): DataHealth {
  return { sync: { runner: "daemon", last_run: { status: lastRunStatus } } } as unknown as DataHealth;
}

async function refocus() {
  await act(() => vi.advanceTimersByTimeAsync(31_000)); // past the 30 s staleTime
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
}

describe("database reads of an open page", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    focusManager.setFocused(undefined);
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("reads the dashboard's opportunities on mount and on focus, never on a timer", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue([]);
    renderHook(() => useOpportunities(), { wrapper: wrapper() });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await act(() => vi.advanceTimersByTimeAsync(15 * 60_000));
    expect(get).toHaveBeenCalledTimes(1);
    await refocus();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it("re-reads data health under an idle daemon only on focus", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(healthReport("success"));
    renderHook(() => useDataHealth(), { wrapper: wrapper() });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await act(() => vi.advanceTimersByTimeAsync(15 * 60_000));
    expect(get).toHaveBeenCalledTimes(1);
    await refocus();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it("re-reads data health every minute while a sync runs", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(healthReport("running"));
    renderHook(() => useDataHealth(), { wrapper: wrapper() });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await act(() => vi.advanceTimersByTimeAsync(60_000 + 50));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });
});
