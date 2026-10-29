import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ageText, ForecastUpdated, updatedClock } from "@/components/forecast-updated";
import { SpotForecastTimeline } from "@/components/forecast/forecast-timeline";
import { SelectionStore, TimelineSelectionProvider } from "@/components/forecast/timeline-selection";
import { useFieldIndex } from "@/hooks/map-fields";
import { useMapConditions, useNotifications, useOpportunities, useSpot, useSpotEvents, useSpotTimeline } from "@/hooks/queries";
import {
  connectStore,
  DEFAULT_POLL_MS,
  fieldIndexPath,
  FIELD_INDEX_KEY,
  ForecastVersionProvider,
  ForecastVersionStore,
  INIT_TIMEOUT_MS,
  isVersionedKey,
  keyVersion,
  MAX_BACKOFF_MS,
  MIN_POLL_MS,
  pollIntervalMs,
  sameExceptVersion,
  SETTLE_MS,
  useForecastVersion,
  utcHourParam,
  versionKey,
} from "@/hooks/use-forecast-version";
import * as api from "@/lib/api";
import type { ForecastVersion } from "@/lib/api";
import type { SpotTimeline } from "@/types/api";

import { jbay } from "./timeline-helpers";

// Every version, spot and timeline below is SYNTHETIC test data (the timeline is the J-Bay
// fixture with made-up changes); only the ids, times and the changed values matter.

const POLL = 20_000;
// The clock of the tests that read through connectStore: in the middle of a UTC hour, so the
// hourly re-read of the public data (review r1-frontend-FE-6) never lands inside a test that
// counts reads (with the wall clock they failed when run in the last minutes of an hour).
const MID_HOUR = Date.UTC(2026, 9, 29, 12, 10);

function version(id: number | null, extra: Partial<ForecastVersion> = {}): ForecastVersion {
  return {
    version_id: id,
    published_at: id == null ? null : new Date(Date.UTC(2026, 9, 27, 10, id)).toISOString(),
    provider: "demo",
    source_code: "demo",
    wind_model: "synthetic-wind",
    wind_model_run: "2026-10-27T06:00:00Z",
    wind_run_estimated: false,
    wave_model: "synthetic-wave",
    wave_model_run: "2026-10-27T06:00:00Z",
    wave_run_estimated: false,
    spots_total: 50,
    spots_updated: 50,
    spots_stale: 0,
    map_run_id: 7,
    map_cycle: "2026-10-27T06:00:00Z",
    attribution: [],
    ...extra,
  };
}

let visibility: DocumentVisibilityState = "visible";
function setVisibility(state: DocumentVisibilityState) {
  visibility = state;
  document.dispatchEvent(new Event("visibilitychange"));
}

/** A fetchVersion that answers `current()` (or throws while `failing`). */
function versionServer(first: ForecastVersion | null) {
  const server = {
    current: first,
    failing: false,
    calls: 0,
    fetch: vi.fn(async () => {
      server.calls += 1;
      if (server.failing || !server.current) throw new api.ApiError(404, "not_found", "Not found");
      return structuredClone(server.current);
    }),
  };
  return server;
}

beforeEach(() => {
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  // Back to jsdom's own getter.
  delete (document as { visibilityState?: unknown }).visibilityState;
});

describe("poll interval setting", () => {
  it("reads NEXT_PUBLIC_FORECAST_VERSION_POLL_MS, with a floor and a default", () => {
    expect(pollIntervalMs(undefined)).toBe(DEFAULT_POLL_MS);
    expect(pollIntervalMs("")).toBe(DEFAULT_POLL_MS);
    expect(pollIntervalMs("soon")).toBe(DEFAULT_POLL_MS);
    expect(pollIntervalMs("2000")).toBe(2000);
    expect(pollIntervalMs("20")).toBe(MIN_POLL_MS);
  });
});

describe("versioned query keys", () => {
  it("end with { v } and compare equal apart from the version", () => {
    const a = ["spot", "pipeline", versionKey(4)];
    const b = ["spot", "pipeline", versionKey(5)];
    expect(isVersionedKey(a)).toBe(true);
    expect(isVersionedKey(["me"])).toBe(false);
    expect(keyVersion(a)).toBe(4);
    expect(keyVersion(["me"])).toBeUndefined();
    expect(sameExceptVersion(a, b)).toBe(true);
    expect(sameExceptVersion(a, ["spot", "uluwatu", versionKey(5)])).toBe(false);
  });

  it("add ?v= only for a published version", () => {
    expect(api.withVersion("/api/spots", 12)).toBe("/api/spots?v=12");
    expect(api.withVersion("/api/events?limit=4", 12)).toBe("/api/events?limit=4&v=12");
    for (const none of [null, undefined, 0, -3, Number.NaN, 1.5]) expect(api.withVersion("/api/spots", none)).toBe("/api/spots");
    expect(api.withParams("/api/map/fields", { v: 12, m: 7 })).toBe("/api/map/fields?v=12&m=7");
    expect(api.withParams("/api/map/fields", { v: null, m: 7 })).toBe("/api/map/fields?m=7");
  });
});

describe("version store", () => {
  beforeEach(() => vi.useFakeTimers());

  it("asks at once, then every poll interval while visible; the same version changes nothing", async () => {
    const server = versionServer(version(1));
    const store = new ForecastVersionStore({ pollMs: POLL, fetchVersion: server.fetch });
    const changes = vi.fn();
    store.onChange = changes;
    const listener = vi.fn();
    store.subscribe(listener);
    const release = store.retain();
    await vi.advanceTimersByTimeAsync(0);
    expect(server.calls).toBe(1);
    expect(store.getState()).toMatchObject({ status: "known", version: { version_id: 1 } });
    const known = store.getState();
    await vi.advanceTimersByTimeAsync(POLL * 3);
    expect(server.calls).toBe(4);
    // Same version: no change, no new state object, no re-render.
    expect(changes).not.toHaveBeenCalled();
    expect(store.getState()).toBe(known);
    expect(listener).toHaveBeenCalledTimes(1);
    release();
    await vi.advanceTimersByTimeAsync(POLL * 5);
    expect(server.calls).toBe(4);
  });

  it("never asks while the tab is hidden, and asks at once when it is visible again", async () => {
    const server = versionServer(version(1));
    const store = new ForecastVersionStore({ pollMs: POLL, fetchVersion: server.fetch });
    store.retain();
    await vi.advanceTimersByTimeAsync(0);
    expect(server.calls).toBe(1);
    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(30 * 60_000);
    expect(server.calls).toBe(1);
    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(server.calls).toBe(2);
    await vi.advanceTimersByTimeAsync(POLL);
    expect(server.calls).toBe(3);
  });

  it("a tab opened in the background reads the version once, then waits until it is shown", async () => {
    visibility = "hidden";
    const server = versionServer(version(1));
    const store = new ForecastVersionStore({ pollMs: POLL, fetchVersion: server.fetch });
    store.retain();
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(server.calls).toBe(1);
    expect(store.getState().status).toBe("known");
  });

  it("backs off after errors (doubling, capped) and returns to the interval after a success", async () => {
    const server = versionServer(version(1));
    server.failing = true;
    const store = new ForecastVersionStore({ pollMs: POLL, fetchVersion: server.fetch });
    store.retain();
    await vi.advanceTimersByTimeAsync(0);
    expect(server.calls).toBe(1);
    expect(store.getState().status).toBe("unknown");
    const at: number[] = [];
    for (const wait of [POLL * 2, POLL * 4, POLL * 8]) {
      await vi.advanceTimersByTimeAsync(wait - 1);
      at.push(server.calls);
      await vi.advanceTimersByTimeAsync(1);
      at.push(server.calls);
    }
    expect(at).toEqual([1, 2, 2, 3, 3, 4]);
    // Capped at MAX_BACKOFF_MS.
    await vi.advanceTimersByTimeAsync(MAX_BACKOFF_MS);
    expect(server.calls).toBe(5);
    await vi.advanceTimersByTimeAsync(MAX_BACKOFF_MS);
    expect(server.calls).toBe(6);
    // Becoming visible during the backoff does not ask early.
    setVisibility("hidden");
    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(server.calls).toBe(6);
    server.failing = false;
    await vi.advanceTimersByTimeAsync(MAX_BACKOFF_MS);
    expect(server.calls).toBe(7);
    expect(store.getState()).toMatchObject({ status: "known", version: { version_id: 1 } });
    await vi.advanceTimersByTimeAsync(POLL);
    expect(server.calls).toBe(8);
  });

  it("settles the first page load on the first answer, a failure or the timeout", async () => {
    const slow = { fetch: vi.fn(() => new Promise<ForecastVersion>(() => undefined)) };
    const store = new ForecastVersionStore({ pollMs: POLL, fetchVersion: slow.fetch });
    store.retain();
    const settled = vi.fn();
    void store.settled().then(settled);
    await vi.advanceTimersByTimeAsync(INIT_TIMEOUT_MS - 1);
    expect(settled).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(settled).toHaveBeenCalled();
    expect(store.getState().status).toBe("unknown");

    const server = versionServer(version(3));
    const quick = new ForecastVersionStore({ pollMs: POLL, fetchVersion: server.fetch });
    quick.retain();
    await quick.settled();
    expect(quick.getState()).toMatchObject({ status: "known", version: { version_id: 3 } });

    const aborted = new ForecastVersionStore({ pollMs: POLL, fetchVersion: slow.fetch });
    aborted.retain();
    const controller = new AbortController();
    const waiting = aborted.settled(controller.signal);
    controller.abort();
    await expect(waiting).rejects.toBeDefined();
  });

  it("reports a new version or map run, and whether the tab stayed visible since the last answer", async () => {
    const server = versionServer(version(1));
    const store = new ForecastVersionStore({ pollMs: POLL, fetchVersion: server.fetch });
    const changes = vi.fn();
    store.onChange = changes;
    store.retain();
    await vi.advanceTimersByTimeAsync(0);
    server.current = version(2);
    await vi.advanceTimersByTimeAsync(POLL);
    expect(changes).toHaveBeenLastCalledWith(expect.objectContaining({ replaced: true, continuous: true, previous: expect.objectContaining({ version_id: 1 }), next: expect.objectContaining({ version_id: 2 }) }));
    // The newest answer is known at once; the page shows it once the provider read its data.
    expect(store.getState()).toMatchObject({ version: { version_id: 2 }, shown: { version_id: 1 }, changes: 0 });
    store.show(store.getState().version!, true);
    expect(store.getState()).toMatchObject({ shown: { version_id: 2 }, changes: 1 });
    server.current = version(2, { map_run_id: 8 });
    await vi.advanceTimersByTimeAsync(POLL);
    expect(changes).toHaveBeenLastCalledWith(expect.objectContaining({ replaced: false }));
    expect(store.getState().changes).toBe(1);
    // Hidden in between: the change is still applied, but not as a continuous view.
    setVisibility("hidden");
    server.current = version(3, { map_run_id: 8 });
    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(changes).toHaveBeenLastCalledWith(expect.objectContaining({ replaced: true, continuous: false }));
    expect(changes).toHaveBeenCalledTimes(3);
  });
});

// --- with React and the query cache -------------------------------------------------------

function setup(first: ForecastVersion | null, { failing = false } = {}) {
  const server = versionServer(first);
  server.failing = failing;
  const store = new ForecastVersionStore({ pollMs: POLL, fetchVersion: server.fetch });
  // The app's defaults (components/providers.tsx).
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ForecastVersionProvider store={store}>{children}</ForecastVersionProvider>
    </QueryClientProvider>
  );
  return { server, store, client, wrapper };
}

const spotDetail = (id: number) => ({ slug: "pipeline", name: "Pipeline", best_upcoming: { score: 40 + id } });

describe("versioned public reads", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true, now: MID_HOUR }));

  it("a first page load reads the data once, with the version in its key and URL", async () => {
    const { wrapper } = setup(version(5));
    const get = vi.spyOn(api, "get").mockImplementation(async () => spotDetail(5));
    const { result } = renderHook(() => useSpot("pipeline"), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(get.mock.calls.map((c) => c[0])).toEqual(["/api/spots/pipeline?v=5"]);
  });

  it("without a version endpoint (or nothing published) the reads stay as they were", async () => {
    for (const [first, failing] of [[version(5), true] as const, [version(null), false] as const]) {
      const { wrapper } = setup(first, { failing });
      const get = vi.spyOn(api, "get").mockImplementation(async () => spotDetail(1));
      const { result, unmount } = renderHook(() => useSpot("pipeline"), { wrapper });
      await waitFor(() => expect(result.current.data).toBeDefined());
      expect(get.mock.calls.map((c) => c[0])).toEqual(["/api/spots/pipeline"]);
      unmount();
      get.mockRestore();
    }
  });

  it("a new version is read at once under its key while the previous data stays on screen", async () => {
    const { server, store, wrapper, client } = setup(version(1));
    let release: (v: unknown) => void = () => undefined;
    const get = vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      const v = Number(new URL(path, "http://x").searchParams.get("v"));
      if (v === 2) await new Promise((r) => (release = r));
      return spotDetail(v);
    });
    const { result } = renderHook(() => useSpot("pipeline"), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual(spotDetail(1)));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(get.mock.calls[1]![0]).toBe("/api/spots/pipeline?v=2");
    // While version 2 loads, the page stays on version 1: its key, its data, its label
    // (never blank, never a loading state).
    expect(result.current.data).toEqual(spotDetail(1));
    expect(result.current.isLoading).toBe(false);
    expect(store.getState()).toMatchObject({ version: { version_id: 2 }, shown: { version_id: 1 } });
    release(undefined);
    await waitFor(() => expect(result.current.data).toEqual(spotDetail(2)));
    expect(store.getState().shown?.version_id).toBe(2);
    // Read once, ahead: the page moved to the cached answer without reading it again.
    expect(get).toHaveBeenCalledTimes(2);
    // Same map run: the field index is left alone.
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: FIELD_INDEX_KEY });
  });

  it("a failed read of the new version keeps the previous version on screen and is tried again at the next answer", async () => {
    const { server, store, wrapper } = setup(version(1));
    let failures = 1;
    const get = vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      const v = Number(new URL(path, "http://x").searchParams.get("v"));
      if (v === 2 && failures > 0) {
        failures -= 1;
        throw new api.ApiError(503, "unavailable", "The service is temporarily unavailable.");
      }
      return spotDetail(v);
    });
    const { result } = renderHook(() => useSpot("pipeline"), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual(spotDetail(1)));
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await act(() => vi.advanceTimersByTimeAsync(0));
    // Still version 1: data, no error, the label's version.
    expect(result.current.data).toEqual(spotDetail(1));
    expect(result.current.isError).toBe(false);
    expect(store.getState().shown?.version_id).toBe(1);
    // The next answer (the same version 2) reads it again, and the page moves to it.
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(result.current.data).toEqual(spotDetail(2)));
    expect(store.getState().shown?.version_id).toBe(2);
    expect(get.mock.calls.map((c) => c[0])).toEqual(["/api/spots/pipeline?v=1", "/api/spots/pipeline?v=2", "/api/spots/pipeline?v=2"]);
  });

  it("a read of the new version that fails for good (4xx) is shown as a first load would", async () => {
    const { server, store, wrapper } = setup(version(1));
    vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      if (path.includes("v=2")) throw new api.ApiError(404, "not_found", "Spot not found");
      return spotDetail(1);
    });
    const { result } = renderHook(() => useSpot("pipeline"), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual(spotDetail(1)));
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(store.getState().shown?.version_id).toBe(2));
    await waitFor(() => expect(result.current.isError).toBe(true));
  });

  it("an older answer (another CDN copy, another API instance) changes nothing", async () => {
    const { server, store, wrapper } = setup(version(1));
    const get = vi.spyOn(api, "get").mockImplementation(async (path: string) => spotDetail(Number(new URL(path, "http://x").searchParams.get("v"))));
    const { result } = renderHook(() => useSpot("pipeline"), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual(spotDetail(1)));
    server.current = version(3);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(result.current.data).toEqual(spotDetail(3)));
    await waitFor(() => expect(store.getState().renderedId).toBe(3));
    const state = store.getState();
    // Version 2 (older, published earlier), and version 3 with an older map run.
    for (const stale of [version(2), version(3, { map_run_id: 6 }), version(null)]) {
      server.current = stale;
      await act(() => vi.advanceTimersByTimeAsync(POLL));
      expect(store.getState()).toBe(state);
    }
    expect(result.current.data).toEqual(spotDetail(3));
    expect(get.mock.calls.map((c) => c[0])).toEqual(["/api/spots/pipeline?v=1", "/api/spots/pipeline?v=3"]);
    // A lower id published later (the database was started again) is a new version.
    server.current = version(1, { published_at: "2026-10-28T09:00:00.000Z" });
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(store.getState().shown?.published_at).toBe("2026-10-28T09:00:00.000Z"));
  });

  it("the previous version is kept for the same read only, never for another spot", async () => {
    const { wrapper } = setup(version(1));
    vi.spyOn(api, "get").mockImplementation(async (path: string) => ({ path }));
    const { result, rerender } = renderHook(({ slug }) => useSpot(slug), { wrapper, initialProps: { slug: "pipeline" } });
    await waitFor(() => expect(result.current.data).toBeDefined());
    rerender({ slug: "uluwatu" });
    expect(result.current.data).toBeUndefined();
    await waitFor(() => expect(result.current.data).toEqual({ path: "/api/spots/uluwatu?v=1" }));
  });

  it("a new map run re-reads the field index under a new URL; the conditions follow the version, not a timer", async () => {
    const { server, wrapper } = setup(version(1));
    const get = vi.spyOn(api, "get").mockImplementation(async (path: string) => (path.startsWith("/api/map/fields") ? { available: false, valid_times: [], variables: [] } : { spots: [] }));
    renderHook(
      () => {
        useFieldIndex();
        useMapConditions(null);
      },
      { wrapper },
    );
    await waitFor(() => expect(get.mock.calls.map((c) => c[0])).toContain("/api/map/conditions?v=1"));
    const urls = () => get.mock.calls.map((c) => c[0]);
    await act(() => vi.advanceTimersByTimeAsync(4 * 60_000));
    expect(urls().filter((u) => u.startsWith("/api/map/conditions"))).toEqual(["/api/map/conditions?v=1"]);
    expect(fieldIndexPath()).toBe("/api/map/fields?v=1&m=7");
    server.current = version(1, { map_run_id: 8 });
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(urls()).toContain("/api/map/fields?v=1&m=8"));
    // A map run alone is no new forecast: the conditions are not read again.
    expect(urls().filter((u) => u.startsWith("/api/map/conditions"))).toEqual(["/api/map/conditions?v=1"]);
  });
});

describe("staleness by age (review r1-frontend-FE-6)", () => {
  // 10:59:30 UTC: the hour changes half a minute after the page loaded.
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true, now: Date.UTC(2026, 9, 29, 10, 59, 30) }));

  it("reads the public data on screen again when the UTC hour changes, under a URL naming the hour", async () => {
    const { wrapper } = setup(version(1));
    const get = vi.spyOn(api, "get").mockImplementation(async () => spotDetail(1));
    renderHook(() => useSpot("pipeline"), { wrapper });
    const reads = () => get.mock.calls.map((c) => c[0]).filter((u) => u.startsWith("/api/spots/pipeline"));
    await waitFor(() => expect(reads()).toEqual(["/api/spots/pipeline?v=1"]));
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    expect(reads()).toEqual(["/api/spots/pipeline?v=1"]); // same hour: nothing read again
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    await waitFor(() => expect(reads()).toEqual(["/api/spots/pipeline?v=1", "/api/spots/pipeline?v=1&h=2026102911"]));
    // Once per hour.
    await act(() => vi.advanceTimersByTimeAsync(POLL * 5));
    expect(reads()).toHaveLength(2);
    expect(utcHourParam(Date.UTC(2026, 0, 2, 3, 59))).toBe(2026010203);
  });

  it("keeps the data on screen when the hour's re-read fails, and tries again at the next answer (review r2 FE-3)", async () => {
    const { wrapper } = setup(version(1));
    let failing = true;
    const get = vi.spyOn(api, "get").mockImplementation(async (url: string) => {
      // Every open tab re-reads at the top of the hour: a 503 for the hour's URLs.
      if (url.includes("&h=") && failing) throw new api.ApiError(503, "unavailable", "Service unavailable");
      return spotDetail(1);
    });
    const { result } = renderHook(() => useSpot("pipeline"), { wrapper });
    const reads = () => get.mock.calls.map((c) => c[0]).filter((u) => u.startsWith("/api/spots/pipeline"));
    await waitFor(() => expect(result.current.data).toBeDefined());
    await act(() => vi.advanceTimersByTimeAsync(40_000)); // the answer of 11:00:10: a new hour
    await waitFor(() => expect(reads()).toEqual(["/api/spots/pipeline?v=1", "/api/spots/pipeline?v=1&h=2026102911"]));
    // The page keeps its data, with no "could not load" state (a first load's error).
    expect(result.current.data).toEqual(spotDetail(1));
    expect(result.current.isLoadingError).toBe(false);
    // The next answer of the poll tries again (not only at the next hour) ...
    failing = false;
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(reads()).toHaveLength(3));
    expect(reads()[2]).toBe("/api/spots/pipeline?v=1&h=2026102911");
    expect(result.current.isError).toBe(false);
    // ... and once it succeeded the hour is done.
    await act(() => vi.advanceTimersByTimeAsync(POLL * 4));
    expect(reads()).toHaveLength(3);
  });

  it("reads each query once, under the new version, when a new version arrives with the new hour (review r2 FE-4)", async () => {
    const { server, wrapper } = setup(version(2));
    const get = vi.spyOn(api, "get").mockImplementation(async (url: string) => (url.includes("v=3") ? spotDetail(3) : spotDetail(2)));
    const { result } = renderHook(() => ({ spot: useSpot("pipeline"), events: useSpotEvents("pipeline") }), { wrapper });
    const reads = () => get.mock.calls.map((c) => c[0]).filter((u) => u.startsWith("/api/spots/pipeline"));
    await waitFor(() => expect(result.current.spot.data).toEqual(spotDetail(2)));
    expect(reads()).toEqual(["/api/spots/pipeline?v=2", "/api/spots/pipeline/events?v=2"]);
    // The first answer of the new hour (11:00:10) also names version 3.
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    server.current = version(3);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(result.current.spot.data).toEqual(spotDetail(3)));
    // No read of the old version for the new hour: one read per query, under version 3.
    expect(reads().slice(2).sort()).toEqual(["/api/spots/pipeline/events?v=3&h=2026102911", "/api/spots/pipeline?v=3&h=2026102911"]);
    await act(() => vi.advanceTimersByTimeAsync(POLL * 3));
    expect(reads()).toHaveLength(4);
  });
});

describe("after a new version is on screen", () => {
  // A fixed clock a few minutes after the versions' publication times (10:01, 10:02 UTC), with
  // no quarter hour in between: in every time zone they fall on the clock's local day, so the
  // label writes "hh:mm" (the day is added only for another day).
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true, now: Date.UTC(2026, 9, 27, 10, 5) }));

  function Page() {
    const spot = useSpot("pipeline");
    return (
      <div>
        <ForecastUpdated />
        <span data-testid="score">{(spot.data as { best_upcoming?: { score: number } } | undefined)?.best_upcoming?.score}</span>
      </div>
    );
  }

  it("reports it once with the seen beacon, announces it and updates the label, never on the first load", async () => {
    const { server, store, client } = setup(version(1));
    const beacon = vi.fn((url: string, body?: BodyInit | null) => Boolean(url && body));
    Object.defineProperty(navigator, "sendBeacon", { configurable: true, value: beacon });
    vi.spyOn(api, "get").mockImplementation(async (path: string) => spotDetail(Number(new URL(path, "http://x").searchParams.get("v"))));
    render(
      <QueryClientProvider client={client}>
        <ForecastVersionProvider store={store}>
          <Page />
        </ForecastVersionProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("41"));
    const label = screen.getByTestId("forecast-updated");
    expect(label).toHaveAttribute("data-version-id", "1");
    expect(label).toHaveTextContent(/^Forecast updated \d{2}:\d{2}/);
    expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("");
    await act(() => vi.advanceTimersByTimeAsync(POLL * 2));
    expect(beacon).not.toHaveBeenCalled();

    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("42"));
    await waitFor(() => expect(beacon).toHaveBeenCalledTimes(1));
    expect(beacon).toHaveBeenCalledWith("/api/forecasts/seen", JSON.stringify({ version_id: 2 }));
    expect(screen.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", "2");
    expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("New forecast loaded");
    expect(screen.getByTestId("forecast-updated")).toHaveAttribute("data-highlight");
    await act(() => vi.advanceTimersByTimeAsync(POLL * 3));
    expect(beacon).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("forecast-updated")).not.toHaveAttribute("data-highlight");
    delete (navigator as { sendBeacon?: unknown }).sendBeacon;
  });

  // Review r1-frontend-FE-5/FE-9: a spot page names the spot's own update, in the spot's zone.
  function SpotPage({ updatedAt }: { updatedAt: (v: number) => string }) {
    const spot = useSpot("pipeline");
    const v = (spot.data as { best_upcoming?: { score: number } } | undefined)?.best_upcoming?.score;
    return (
      <div>
        <ForecastUpdated updatedAt={v == null ? null : updatedAt(v - 40)} timeZone="Pacific/Honolulu" />
        <span data-testid="score">{v}</span>
      </div>
    );
  }

  it("on a spot page shows the spot's update time, and announces only a version that updated it", async () => {
    const { server, store, client } = setup(version(1));
    vi.spyOn(api, "get").mockImplementation(async (path: string) => spotDetail(Number(new URL(path, "http://x").searchParams.get("v"))));
    // Versions 1 and 2 leave this spot as it was at 09:40 UTC (version 2 retried other spots);
    // version 3 updates it at 10:03 UTC.
    const updatedAt = (v: number) => (v >= 3 ? "2026-10-27T10:03:00Z" : "2026-10-27T09:40:00Z");
    render(
      <QueryClientProvider client={client}>
        <ForecastVersionProvider store={store}>
          <SpotPage updatedAt={updatedAt} />
        </ForecastVersionProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("41"));
    const label = () => screen.getByTestId("forecast-updated");
    // 09:40 UTC is 23:40 the day before in Honolulu (HST): the date is named, then the zone.
    expect(label()).toHaveTextContent("Forecast updated Oct 26, 23:40 HST");
    expect(label()).toHaveAttribute("data-updated-at", "2026-10-27T09:40:00Z");
    expect(screen.getByTestId("forecast-updated-age")).toHaveTextContent("25 min ago");

    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("42"));
    await waitFor(() => expect(label()).toHaveAttribute("data-version-id", "2"));
    await act(() => vi.advanceTimersByTimeAsync(0));
    // A version that did not touch this spot: neither announced nor highlighted.
    expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("");
    expect(label()).not.toHaveAttribute("data-highlight");
    expect(label()).toHaveTextContent("23:40 HST");
    // Review r2-frontend-R2-FE-5: the title names the version's own publication (10:02 UTC)
    // and the spot's older update, both in the clock's zone; never the update as "published".
    expect(label().getAttribute("title")).toBe(
      "Forecast version 2, published Tue, Oct 27, 00:02 HST; this spot's forecast was last updated Mon, Oct 26, 23:40 HST. " +
        "Open pages pick up new versions by themselves.",
    );

    server.current = version(3);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("43"));
    await waitFor(() => expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("New forecast loaded"));
    expect(label()).toHaveAttribute("data-highlight");
    expect(label()).toHaveTextContent("Forecast updated 00:03 HST");
  });

  it("draws no label beside an out-of-date selection (the map passes stale)", async () => {
    const { store, client } = setup(version(1));
    vi.spyOn(api, "get").mockImplementation(async () => spotDetail(1));
    render(
      <QueryClientProvider client={client}>
        <ForecastVersionProvider store={store}>
          <ForecastUpdated tone="map" stale />
          <Page />
        </ForecastVersionProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("41"));
    // Only Page's own label (not stale) is drawn; the map's stale one keeps its live region.
    expect(screen.getAllByTestId("forecast-updated")).toHaveLength(1);
    expect(screen.getAllByTestId("forecast-updated-announcement")).toHaveLength(2);
  });

  it("does not report a version learned after the tab was hidden (its latency would include the absence)", async () => {
    const { server, store, client } = setup(version(1));
    const report = vi.fn();
    vi.spyOn(api, "get").mockImplementation(async () => spotDetail(1));
    render(
      <QueryClientProvider client={client}>
        <ForecastVersionProvider store={store}>
          <Page />
        </ForecastVersionProvider>
      </QueryClientProvider>,
    );
    // The provider's own connection reports with sendBeacon; this one records instead.
    const disconnect = connectStore(store, client, report);
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("41"));
    setVisibility("hidden");
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(10 * 60_000));
    setVisibility("visible");
    await waitFor(() => expect(screen.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", "2"));
    await waitFor(() => expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("New forecast loaded"));
    expect(report).not.toHaveBeenCalled();
    disconnect();
  });

  it("the label stays on the version shown while the next one loads, and while its read fails", async () => {
    const { server, store, client } = setup(version(1));
    let mode: "hold" | "fail" | "ok" = "hold";
    let release: (v: unknown) => void = () => undefined;
    vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      const v = Number(new URL(path, "http://x").searchParams.get("v"));
      if (v === 2 && mode === "hold") await new Promise((r) => (release = r));
      if (v === 2 && mode === "fail") throw new TypeError("Failed to fetch");
      return spotDetail(v);
    });
    render(
      <QueryClientProvider client={client}>
        <ForecastVersionProvider store={store}>
          <Page />
        </ForecastVersionProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("41"));
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    // Loading version 2: the page, its label and the announcement stay on version 1.
    expect(screen.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", "1");
    expect(screen.getByTestId("score")).toHaveTextContent("41");
    mode = "fail";
    release(undefined);
    await act(() => vi.advanceTimersByTimeAsync(0));
    // Version 2's read failed (no network): still version 1, no error shown.
    expect(screen.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", "1");
    expect(screen.getByTestId("score")).toHaveTextContent("41");
    expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("");
    mode = "ok";
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("42"));
    expect(screen.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", "2");
    await waitFor(() => expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("New forecast loaded"));
  });

  it("does not report a version whose data was painted after the tab was hidden", async () => {
    const { server, store, client } = setup(version(1));
    const report = vi.fn();
    let release: (v: unknown) => void = () => undefined;
    vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      const v = Number(new URL(path, "http://x").searchParams.get("v"));
      if (v === 2) await new Promise((r) => (release = r));
      return spotDetail(v);
    });
    render(
      <QueryClientProvider client={client}>
        <ForecastVersionProvider store={store}>
          <Page />
        </ForecastVersionProvider>
      </QueryClientProvider>,
    );
    const disconnect = connectStore(store, client, report);
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("41"));
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    // Version 2 was learned while visible; its data arrives after the tab was hidden.
    setVisibility("hidden");
    release(undefined);
    setVisibility("visible");
    await waitFor(() => expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("New forecast loaded"));
    expect(report).not.toHaveBeenCalled();
    disconnect();
  });

  it("reports a version id at most once per tab, also when a database started again reuses it", async () => {
    const { server, store, client } = setup(version(1));
    const report = vi.fn();
    vi.spyOn(api, "get").mockImplementation(async (path: string) => spotDetail(Number(new URL(path, "http://x").searchParams.get("v"))));
    render(
      <QueryClientProvider client={client}>
        <ForecastVersionProvider store={store}>
          <Page />
        </ForecastVersionProvider>
      </QueryClientProvider>,
    );
    const disconnect = connectStore(store, client, report);
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("41"));
    // 2, then a database started from scratch publishes 1 and 2 again (later than before):
    // each is a new version on screen, but the tab reports the id 2 only once.
    for (const [next, minute] of [
      [2, 2],
      [1, 3],
      [2, 4],
    ] as const) {
      server.current = version(next, { published_at: new Date(Date.UTC(2026, 9, 27, 10, minute)).toISOString() });
      await act(() => vi.advanceTimersByTimeAsync(POLL));
      await waitFor(() => expect(store.getState().renderedId).toBe(next));
      await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent(String(40 + next)));
    }
    expect(store.getState().changes).toBe(3);
    expect(report.mock.calls.map((c) => c[0])).toEqual([2, 1]);
    disconnect();
  });

  it("a stale spot's label leaves the claim to its stale notice; the announcement region stays", async () => {
    const { wrapper } = setup(version(1));
    const { result } = renderHook(() => useForecastVersion(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("known"));
    const { rerender } = render(<ForecastUpdated stale />, { wrapper });
    expect(screen.queryByTestId("forecast-updated")).toBeNull();
    expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("");
    rerender(<ForecastUpdated />);
    expect(screen.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", "1");
  });

  it("a version endpoint that answers after the first reads moves the page to it without announcing it", async () => {
    let answer: (v: ForecastVersion) => void = () => undefined;
    const store = new ForecastVersionStore({ pollMs: POLL, initTimeoutMs: 1_000, fetchVersion: () => new Promise<ForecastVersion>((r) => (answer = r)) });
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: false } } });
    const report = vi.fn();
    const get = vi.spyOn(api, "get").mockImplementation(async (path: string) => spotDetail(Number(new URL(path, "http://x").searchParams.get("v") ?? 0)));
    render(
      <QueryClientProvider client={client}>
        <ForecastVersionProvider store={store}>
          <Page />
        </ForecastVersionProvider>
      </QueryClientProvider>,
    );
    const disconnect = connectStore(store, client, report);
    // The version did not answer in time: the data is read without one.
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("40"));
    expect(get.mock.calls.map((c) => c[0])).toEqual(["/api/spots/pipeline"]);
    answer(version(4));
    await waitFor(() => expect(screen.getByTestId("score")).toHaveTextContent("44"));
    expect(screen.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", "4");
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(screen.getByTestId("forecast-updated-announcement")).toHaveTextContent("");
    expect(store.getState().changes).toBe(0);
    expect(report).not.toHaveBeenCalled();
    expect(get.mock.calls.map((c) => c[0])).toEqual(["/api/spots/pipeline", "/api/spots/pipeline?v=4"]);
    disconnect();
  });

  it("reads the signed-in opportunities again once, after the refresh's matching", async () => {
    const { server, wrapper } = setup(version(1));
    const get = vi.spyOn(api, "get").mockImplementation(async (path: string) =>
      path.startsWith("/api/opportunities") ? [] : path.startsWith("/api/notifications") ? { items: [], total: 0 } : spotDetail(1),
    );
    renderHook(
      () => {
        useSpot("pipeline");
        useOpportunities();
        // The dashboard's "Alerts sent" / "Recent alerts" (review r1-frontend-FE-7).
        useNotifications(0, 5);
      },
      { wrapper },
    );
    const reads = () => get.mock.calls.filter((c) => c[0].startsWith("/api/opportunities")).length;
    const alerts = () => get.mock.calls.filter((c) => c[0].startsWith("/api/notifications")).length;
    await waitFor(() => expect(reads()).toBe(1));
    await waitFor(() => expect(alerts()).toBe(1));
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    expect(reads()).toBe(1);
    expect(alerts()).toBe(1);
    await act(() => vi.advanceTimersByTimeAsync(SETTLE_MS));
    await waitFor(() => expect(reads()).toBe(2));
    await waitFor(() => expect(alerts()).toBe(2));
    await act(() => vi.advanceTimersByTimeAsync(SETTLE_MS * 4));
    expect(reads()).toBe(2);
    expect(alerts()).toBe(2);
  });
});

describe("the spot timeline across versions", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: MID_HOUR });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 0, right: 1000, top: 0, bottom: 600, width: 1000, height: 600, x: 0, y: 0, toJSON: () => ({}) });
  });

  /** Version 2 of the fixture: every breaking height 1.5 ft higher (SYNTHETIC change), and,
   * with `dropFirst`, a run that starts that many steps later. */
  function nextVersion(tl: SpotTimeline, dropFirst = 0): SpotTimeline {
    const out = structuredClone(tl);
    for (const s of out.steps) {
      s.breaking.central_ft = Math.round((s.breaking.central_ft + 1.5) * 100) / 100;
      s.breaking.typical_ft = Math.round((s.breaking.typical_ft + 1.5) * 10) / 10;
    }
    out.steps = out.steps.slice(dropFirst);
    return out;
  }

  function Section({ store }: { store: SelectionStore }) {
    const q = useSpotTimeline("jeffreys-bay");
    return (
      <TimelineSelectionProvider initialT={null} store={store}>
        {q.isLoading && <p>loading</p>}
        {q.data && <SpotForecastTimeline timeline={q.data} units="ft" />}
      </TimelineSelectionProvider>
    );
  }

  const selectedBar = () => document.querySelector('[data-testid="wave-bars"] g[data-selected]');

  it("keeps the selected time and shows the new version's values for it", async () => {
    const v1 = jbay();
    const v2 = nextVersion(v1);
    const { server, wrapper } = setup(version(1));
    vi.spyOn(api, "get").mockImplementation(async (path: string) => (path.includes("v=2") ? v2 : v1));
    const store = new SelectionStore({ selectedT: Date.parse(v1.steps[6]!.t) });
    render(<Section store={store} />, { wrapper });
    await waitFor(() => expect(selectedBar()).toHaveAttribute("data-central", String(v1.steps[6]!.breaking.central_ft)));
    const summary = screen.getByTestId("summary-time").textContent;
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(selectedBar()).toHaveAttribute("data-central", String(v2.steps[6]!.breaking.central_ft)));
    expect(selectedBar()).toHaveAttribute("data-step", "6");
    expect(screen.getByTestId("summary-time").textContent).toBe(summary);
    expect(screen.getByTestId("summary-breaking")).toHaveAttribute("data-central", String(v2.steps[6]!.breaking.central_ft));
    expect(store.getState().selectedT).toBe(Date.parse(v1.steps[6]!.t));
    expect(screen.queryByText("loading")).toBeNull();
  });

  it("snaps to the nearest step when the selected one is gone from the new version", async () => {
    const v1 = jbay();
    const v2 = nextVersion(v1, 2); // the new run starts two steps later
    const { server, wrapper } = setup(version(1));
    vi.spyOn(api, "get").mockImplementation(async (path: string) => (path.includes("v=2") ? v2 : v1));
    const store = new SelectionStore({ selectedT: Date.parse(v1.steps[0]!.t) });
    render(<Section store={store} />, { wrapper });
    await waitFor(() => expect(selectedBar()).toHaveAttribute("data-step", "0"));
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL));
    await waitFor(() => expect(selectedBar()).toHaveAttribute("data-central", String(v2.steps[0]!.breaking.central_ft)));
    expect(selectedBar()).toHaveAttribute("data-step", "0");
    expect(screen.getByTestId("summary-breaking")).toHaveAttribute("data-central", String(v2.steps[0]!.breaking.central_ft));
  });
});

describe("forecast updated label", () => {
  it("writes the publication time in the viewer's zone and its age", () => {
    const now = Date.parse("2026-10-27T14:40:00Z");
    expect(updatedClock("2026-10-27T14:32:00Z", now, "UTC")).toBe("14:32");
    expect(updatedClock("2026-10-27T14:32:00Z", now, "Asia/Tokyo")).toBe("23:32");
    expect(updatedClock("2026-10-26T09:05:00Z", now, "UTC")).toBe("Oct 26, 09:05");
    expect(ageText(now - 20_000, now)).toBe("just now");
    expect(ageText(now - 8 * 60_000, now)).toBe("8 min ago");
    expect(ageText(now - 7 * 3_600_000, now)).toBe("7 h ago");
    expect(ageText(now - 3 * 86_400_000, now)).toBe("3 days ago");
    expect(ageText(now + 60_000, now)).toBe("just now");
  });

  it("shows nothing before a version is known or while nothing is published", async () => {
    const client = new QueryClient();
    const { container } = render(
      <QueryClientProvider client={client}>
        <ForecastUpdated />
      </QueryClientProvider>,
    );
    expect(container).toBeEmptyDOMElement();
    vi.useFakeTimers({ shouldAdvanceTime: true, now: MID_HOUR });
    const { wrapper } = setup(version(null));
    const { result } = renderHook(() => useForecastVersion(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("known"));
    render(<ForecastUpdated />, { wrapper });
    expect(screen.queryByTestId("forecast-updated")).toBeNull();
  });
});

// A plain query outside the versioned set is never touched by a new version.
describe("other reads", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true, now: MID_HOUR }));

  it("keep their keys and are not read again", async () => {
    const { server, wrapper } = setup(version(1));
    const fn = vi.fn(async () => "regions");
    renderHook(() => useQuery({ queryKey: ["regions"], queryFn: fn }), { wrapper });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));
    server.current = version(2);
    await act(() => vi.advanceTimersByTimeAsync(POLL + SETTLE_MS + 1000));
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
