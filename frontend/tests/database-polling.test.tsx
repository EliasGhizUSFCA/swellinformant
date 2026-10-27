import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type DataHealth, useDataHealth } from "@/app/status/data-health";
import { useEvents, useForecast, useMapConditions, useOpportunities, useSpot, useSpotEvents, useSpots, useSpotTimeline } from "@/hooks/queries";
import { DEFAULT_POLL_MS, ForecastVersionProvider } from "@/hooks/use-forecast-version";
import * as api from "@/lib/api";

// Reads that reach the database (no-store, or a CDN window shorter than Neon's 5-minute
// suspend delay) must not run on a timer while a page merely stays visible: one open tab would
// keep a scale-to-zero database awake. The one read an open page makes on a timer is
// GET /api/forecasts/version, which the API answers from Redis or its memo (never the
// database while a version is cached) and only while the tab is visible; every public
// forecast read follows the version it names instead of a timer. The reports and the version
// below are SYNTHETIC test data.

function wrapper({ live = false } = {}) {
  // The app's defaults (components/providers.tsx), with its version provider when `live`.
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{live ? <ForecastVersionProvider>{children}</ForecastVersionProvider> : children}</QueryClientProvider>;
  };
}

const VERSION: api.ForecastVersion = {
  version_id: 3,
  published_at: "2026-10-27T10:00:00Z",
  provider: "demo",
  source_code: "demo",
  wind_model: null,
  wind_model_run: null,
  wind_run_estimated: false,
  wave_model: null,
  wave_model_run: null,
  wave_run_estimated: false,
  spots_total: 50,
  spots_updated: 50,
  spots_stale: 0,
  map_run_id: null,
  map_cycle: null,
  attribution: [],
};

/** Every public forecast read of the map, spot and dashboard pages, plus the opportunities. */
function usePublicReads() {
  useSpots();
  useSpot("pipeline");
  useForecast("pipeline");
  useSpotTimeline("pipeline");
  useSpotEvents("pipeline");
  useEvents(6, 57);
  useMapConditions(null);
  useMapConditions("2026-10-28T12:00:00Z");
  useOpportunities();
}

function countReads(get: ReturnType<typeof vi.spyOn>) {
  const counts = new Map<string, number>();
  for (const [path] of get.mock.calls as Array<[string]>) counts.set(path, (counts.get(path) ?? 0) + 1);
  return counts;
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

describe("the forecast version poll, the one read on a timer", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("asks for the version every poll interval; every other read of an open page happens once", async () => {
    const get = vi.spyOn(api, "get").mockImplementation(async (path: string) => (path === api.FORECAST_VERSION_PATH ? VERSION : path.startsWith("/api/map/conditions") ? { spots: [] } : []));
    renderHook(() => usePublicReads(), { wrapper: wrapper({ live: true }) });
    await waitFor(() => expect(countReads(get).get("/api/opportunities?include_past=false")).toBe(1));
    await act(() => vi.advanceTimersByTimeAsync(15 * 60_000));
    const counts = countReads(get);
    const polls = counts.get(api.FORECAST_VERSION_PATH) ?? 0;
    counts.delete(api.FORECAST_VERSION_PATH);
    // 15 minutes at 20 s, plus the first answer (the fake clock may add one tick).
    expect(polls).toBeGreaterThanOrEqual((15 * 60_000) / DEFAULT_POLL_MS);
    expect(polls).toBeLessThanOrEqual((15 * 60_000) / DEFAULT_POLL_MS + 2);
    expect(Object.fromEntries(counts)).toEqual({
      "/api/spots?v=3": 1,
      "/api/spots/pipeline?v=3": 1,
      "/api/spots/pipeline/forecast?days=10&v=3": 1,
      "/api/spots/pipeline/timeline?days=10&v=3": 1,
      "/api/spots/pipeline/events?v=3": 1,
      "/api/events?limit=6&min_score=57&v=3": 1,
      "/api/map/conditions?v=3": 1,
      "/api/map/conditions?at=2026-10-28T12%3A00%3A00Z&v=3": 1,
      "/api/opportunities?include_past=false": 1,
    });
  });

  it("asks nothing at all while the tab is hidden", async () => {
    let visibility: DocumentVisibilityState = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
    try {
      const get = vi.spyOn(api, "get").mockImplementation(async (path: string) => (path === api.FORECAST_VERSION_PATH ? VERSION : []));
      renderHook(() => usePublicReads(), { wrapper: wrapper({ live: true }) });
      await waitFor(() => expect(countReads(get).get("/api/opportunities?include_past=false")).toBe(1));
      visibility = "hidden";
      document.dispatchEvent(new Event("visibilitychange"));
      const before = get.mock.calls.length;
      await act(() => vi.advanceTimersByTimeAsync(30 * 60_000));
      expect(get.mock.calls.length).toBe(before);
    } finally {
      delete (document as { visibilityState?: unknown }).visibilityState;
    }
  });
});

