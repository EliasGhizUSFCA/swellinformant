import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { layerVariables, useFieldIndex, useMapLayerFields } from "@/hooks/map-fields";
import * as api from "@/lib/api";
import type { FieldIndex } from "@/lib/map-fields";
import { DEFAULT_SETTINGS, type MapSettings } from "@/lib/map-settings";

// The index and fields below are SYNTHETIC test data.
const AT = "2026-10-14T12:00:00Z";
const core = (name: string) => ({ name, label: name, unit: "", display_min: 0, display_max: 1, group: "core" as const, system: null });
const partition = (name: string, system: "primary" | "secondary" | "windsea") => ({ ...core(name), group: "partition" as const, system });
const INDEX: FieldIndex = {
  available: true,
  message: null,
  source: { code: "synthetic", name: "Synthetic", attribution: "Synthetic test data", is_demo: true },
  run_key: "run-1",
  cycle_time: AT,
  valid_times: [AT],
  variables: ["hs", "tp", "wave_dir", "wind_u", "wind_v", "pressure"].map(core),
  updated_at: AT,
  age_hours: 1,
  stale_after_hours: 12,
  stale: false,
};

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("field index freshness", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("re-reads the index when the window regains focus, never on a timer", async () => {
    // Design D11: a new map run reaches an open map through the forecast version poll (its
    // map_run_id re-reads the index under a new URL: tests/forecast-version.test.tsx), so the
    // index has no timer of its own (it used to be re-read every 5 minutes).
    const get = vi.spyOn(api, "get").mockResolvedValue(INDEX);
    const { result } = renderHook(() => useFieldIndex(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data?.run_key).toBe("run-1"));
    expect(get).toHaveBeenCalledTimes(1);
    get.mockResolvedValue({ ...INDEX, run_key: "run-2" });
    await act(() => vi.advanceTimersByTimeAsync(30 * 60_000));
    expect(get).toHaveBeenCalledTimes(1);
    // Focus after the data went stale (1 minute): one more read, with the new run.
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.data?.run_key).toBe("run-2"));
    focusManager.setFocused(undefined);
  });

  it("re-reads the index when a field arrives for another valid time, and never shows it", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(INDEX);
    const fetch = vi.fn(async (url: string) => {
      const variable = new URL(url, "http://x").pathname.split("/").pop()!;
      const meta = { variable, unit: "", valid_time: "2026-10-14T15:00:00Z", run_key: "run-1", nx: 2, ny: 2, lat0: 0, lon0: 0, dlat: 1, dlon: 1, scale: 1, offset: 0, nodata: 255 };
      return new Response(new Uint8Array(4), { status: 200, headers: { "X-Field-Meta": JSON.stringify(meta) } });
    });
    vi.stubGlobal("fetch", fetch);
    try {
      const { result } = renderHook(
        () => {
          const index = useFieldIndex().data ?? null;
          return useMapLayerFields({ settings: DEFAULT_SETTINGS, at: index ? AT : null, index });
        },
        { wrapper: wrapper() },
      );
      await waitFor(() => expect(fetch).toHaveBeenCalled());
      await waitFor(() => expect(get).toHaveBeenCalledTimes(2)); // the index was invalidated
      expect(result.current.fields).toBeNull();
      expect(result.current.error).toMatch(/asked for 2026-10-14T12:00:00Z/);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("which variables the map asks for", () => {
  it("asks for partitions only when the index lists them", () => {
    expect(layerVariables(DEFAULT_SETTINGS, INDEX)).toEqual({ required: ["hs", "tp", "wave_dir"], optional: [] });
    const withParts: FieldIndex = {
      ...INDEX,
      variables: [...INDEX.variables, ...(["swell1_hs", "swell1_tp", "swell1_dir"] as const).map((n) => partition(n, "primary"))],
    };
    // Primary and secondary are chosen; the run has no secondary swell: never requested.
    // (Chosen explicitly, not through the default systems, which the wave look may change.)
    const primaryAndSecondary = { ...DEFAULT_SETTINGS, waveSystems: ["primary", "secondary"] } satisfies MapSettings;
    expect(layerVariables(primaryAndSecondary, withParts).optional).toEqual(["swell1_hs", "swell1_tp", "swell1_dir"]);
    expect(layerVariables({ ...DEFAULT_SETTINGS, layer: "wind" }, withParts)).toEqual({ required: ["wind_u", "wind_v"], optional: [] });
    // The wind overlay on a wave layer adds the wind components.
    expect(layerVariables({ ...DEFAULT_SETTINGS, windOverlay: true }, INDEX).optional).toEqual(["wind_u", "wind_v"]);
  });
});
