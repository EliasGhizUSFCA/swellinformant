import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FreshnessView, NotAdmin } from "@/components/admin/freshness-dashboard";
import { cycle, duration, percent, staleReason, usageLine, usageTone, utc } from "@/components/admin/freshness-format";
import type { AdminFreshness } from "@/components/admin/freshness-types";
import { FRESHNESS_PATH, useAdminFreshness, VERSION_PATH, versionPollMs } from "@/components/admin/use-admin-freshness";
import * as api from "@/lib/api";

// SYNTHETIC test data: an invented freshness report in the shape of GET /api/admin/freshness.
// The timings imitate the measured Open-Meteo GFS delays; nothing here describes a real run.
const window = (used: number | null, limit: number | null) => ({
  used,
  limit,
  fraction: used != null && limit ? used / limit : null,
  resets_at: "2026-10-29T00:00:00Z",
});
const stats = (values: number[]) =>
  values.length
    ? { samples: values.length, last: values[0], avg: values.reduce((a, b) => a + b, 0) / values.length, p50: values[0], p95: values[values.length - 1], max: Math.max(...values) }
    : { samples: 0, last: null, avg: null, p50: null, p95: null, max: null };

function report(overrides: Partial<AdminFreshness> = {}): AdminFreshness {
  return {
    checked_at: "2026-10-28T18:00:00Z",
    status: "degraded",
    version_id: 42,
    active_provider: {
      primary: "open_meteo",
      providers: ["open_meteo"],
      configured: ["windy"],
      source: "WEATHER_PROVIDER",
      weather_provider: "auto",
      windy_eligible: true,
      fallback: "open_meteo",
      fallback_active: true,
      fallback_from: "windy",
      fallback_until: "2026-10-28T19:00:00Z",
      reasons: ["Windy failed (credentials): open_meteo stands in until 19:00 UTC"],
      health: [],
    },
    atmosphere: {
      component: "atmosphere",
      model: "ncep_gfs013,ncep_gfs025",
      run_key: "ncep_gfs013:2026102806",
      run: "2026-10-28T06:00:00Z",
      estimated: false,
      available_at: "2026-10-28T11:22:00Z",
      last_detected_at: "2026-10-28T11:32:40Z",
      last_acquired_at: "2026-10-28T11:34:00Z",
      changed: false,
      carried_from_run_id: 7,
      newest_run: "2026-10-28T06:00:00Z",
      update_interval_minutes: 360,
      update_interval_recorded: true,
      typical_lag_minutes: 322,
      lag_samples: 4,
      expected_next_at: "2026-10-28T17:22:00Z",
      late_after: "2026-10-28T18:52:00Z",
      late: false,
      domains: [
        { model: "ncep_gfs013", init_time: "2026-10-28T06:00:00Z", available_at: "2026-10-28T10:41:00Z" },
        { model: "ncep_gfs025", init_time: "2026-10-28T06:00:00Z", available_at: "2026-10-28T11:22:00Z" },
      ],
    },
    wave: {
      component: "wave",
      model: "ncep_gfswave025",
      run_key: "ncep_gfswave025:2026102812",
      run: "2026-10-28T12:00:00Z",
      estimated: false,
      available_at: "2026-10-28T17:17:00Z",
      last_detected_at: "2026-10-28T17:27:40Z",
      last_acquired_at: "2026-10-28T17:29:00Z",
      changed: true,
      carried_from_run_id: null,
      newest_run: "2026-10-28T12:00:00Z",
      update_interval_minutes: 360,
      update_interval_recorded: true,
      typical_lag_minutes: 317,
      lag_samples: 5,
      expected_next_at: "2026-10-28T23:17:00Z",
      late_after: "2026-10-29T00:47:00Z",
      late: false,
      domains: [],
    },
    last_detected_model_update: { at: "2026-10-28T17:27:40Z", detail: "open_meteo: wave (version 42)", version_id: 42 },
    last_successful_acquisition: { at: "2026-10-28T17:29:13Z", detail: "open_meteo: wave (version 42)", version_id: 42 },
    last_successful_scoring: { at: "2026-10-28T17:29:23Z", detail: "open_meteo: wave (version 42)", version_id: 42 },
    last_published_version: {
      id: 42,
      published_at: "2026-10-28T17:29:25Z",
      age_minutes: 30.6,
      provider: "open_meteo",
      source_code: "open_meteo_noaa",
      fallback_from: "windy",
      wind_model: "ncep_gfs013,ncep_gfs025",
      wind_model_run: "2026-10-28T06:00:00Z",
      wind_run_estimated: false,
      wave_model: "ncep_gfswave025",
      wave_model_run: "2026-10-28T12:00:00Z",
      wave_run_estimated: false,
      counts: { total: 50, updated: 47, stale: 3, failed: 3 },
    },
    spots_updated: 47,
    stale_spots: [
      { spot_id: 1, slug: "pipeline", name: "Pipeline", last_updated_at: "2026-10-28T11:34:00Z", reason: "refresh_failed", error: "HTTP 500" },
    ],
    failed_requests: [{ provider: "open_meteo_noaa", status: "500", count: 2 }],
    api_usage: {
      checked_at: "2026-10-28T18:00:00Z",
      alert_fraction: 0.8,
      open_meteo: { minute: window(0, 600), hour: window(170, 5000), day: window(8500, 10000), month: window(51000, 300000), available: true },
      windy: { day: window(null, 10000), available: false, counted: false },
      flights: { provider: "duffel", today: 3, per_provider: { duffel: 2, demo: 1 }, failed_today: 1, quota: window(7, 300), user_daily_quota: 20 },
      emails: { provider: "resend", today: 3, account: 2, alerts: 1, failed: 1 },
      requests_24h: [
        { source: "open_meteo_noaa", provider: "open_meteo", runs: 3, failed_runs: 1, requests: 9, weighted_calls: 510, bytes_downloaded: 1020000, http_status_counts: { "200": 6, "500": 2 }, failed_requests: 2 },
      ],
    },
    processing_time: {
      refreshes: 3,
      total_ms: stats([12090, 11940]),
      stages: [{ stage: "predict", runs: 2, avg_ms: 8950, p95_ms: 8950, max_ms: 8950, avg_cpu_s: 7.18 }],
      detected_to_acquired_s: stats([92]),
      acquired_to_scored_s: stats([10]),
      scored_to_published_s: stats([2]),
    },
    latency: {
      upstream_to_publication_s: stats([745, 750]),
      publication_to_frontend_s: stats([21.5, 33]),
      first_served_s: stats([4]),
    },
    refresh_jobs: [
      {
        id: 9,
        trigger: "http",
        status: "failed",
        refresh: true,
        kinds: ["spots"],
        started_at: "2026-10-28T17:45:00Z",
        finished_at: "2026-10-28T17:45:04Z",
        duration_ms: 4250,
        published_at: null,
        stages: [{ name: "spots", status: "failed", duration_ms: 4100, cpu_s: 0.4 }],
        version_ids: [],
        error: "TransientProviderError: Open-Meteo HTTP 500",
      },
    ],
    versions: [
      {
        id: 42,
        status: "published",
        kind: "refresh",
        provider: "open_meteo",
        fallback_from: "windy",
        detected_at: "2026-10-28T17:27:40Z",
        upstream_available_at: "2026-10-28T17:17:00Z",
        staged_at: "2026-10-28T17:29:24Z",
        published_at: "2026-10-28T17:29:25Z",
        wind_model_run: "2026-10-28T06:00:00Z",
        wave_model_run: "2026-10-28T12:00:00Z",
        changed: ["wave"],
        spots_updated: 47,
        spots_stale: 3,
        spots_failed: 3,
        upstream_to_publication_s: 745,
      },
    ],
    scheduler: {
      expected_minutes: 30,
      limit_minutes: 30,
      sync_running: false,
      enabled: true,
      refresh_enabled: true,
      last_check_at: "2026-10-28T17:45:00Z",
      last_check_by: "sync 9 (http)",
      checks: [{ source: "sync 9 (http)", at: "2026-10-28T17:45:00Z" }],
      silent: false,
    },
    problems: [
      { key: "forecast_stale_spots", severity: "degraded", message: "3 of 50 spots in published version 42 serve a stale forecast.", detail: "pipeline: HTTP 500" },
      { key: "quota_near:open_meteo_day", severity: "degraded", message: "Open-Meteo weighted calls this day: 8500 of 10000 used (85%).", detail: null },
    ],
    health_problems: [{ key: "forecasts_unavailable", severity: "failing", message: "No forecast source has a fresh run.", detail: null }],
    notes: ["Counters live in Redis and restart from zero when Redis loses its data."],
    ...overrides,
  };
}

describe("freshness formatting", () => {
  it("shows UTC times, cycles and durations in plain units", () => {
    expect(utc("2026-10-28T06:05:00Z")).toBe("28 Oct 06:05 UTC");
    expect(utc(null)).toBe("—");
    expect(cycle("2026-10-28T12:00:00Z")).toBe("12Z 28 Oct");
    expect(duration(0.42)).toBe("420 ms");
    expect(duration(4.2)).toBe("4.2 s");
    expect(duration(745)).toBe("12 min");
    expect(duration(322 * 60)).toBe("5.4 h");
    expect(duration(3 * 86400)).toBe("3.0 days");
    expect(percent(0.051)).toBe("5.1 %");
  });

  it("colours a meter by the alert fraction and says when a counter is not readable", () => {
    expect(usageTone(window(10, 100), 0.8)).toBe("good");
    expect(usageTone(window(80, 100), 0.8)).toBe("warn");
    expect(usageTone(window(100, 100), 0.8)).toBe("danger");
    expect(usageTone(window(null, 100), 0.8)).toBe("muted");
    expect(usageLine(window(8500, 10000))).toBe("8,500 of 10,000 (85 %)");
    expect(usageLine(window(null, 10000))).toBe("unknown of 10,000 (counter unreadable)");
    expect(staleReason("refresh_failed")).toBe("latest refresh failed");
  });
});

describe("the freshness dashboard", () => {
  it("renders every section-16 group from the report", () => {
    render(<FreshnessView data={report()} />);
    const problems = screen.getByTestId("freshness-problems");
    expect(within(problems).getByTestId("freshness-status")).toHaveTextContent("Degraded");
    const keys = within(problems).getAllByTestId("freshness-problem").map((el) => el.textContent ?? "");
    expect(keys[0]).toContain("forecasts_unavailable"); // failing first
    expect(problems).toHaveTextContent("Last error: pipeline: HTTP 500");

    const provider = screen.getByTestId("freshness-provider");
    expect(within(provider).getByTestId("freshness-active-provider")).toHaveTextContent("open_meteo");
    expect(provider).toHaveTextContent("open_meteo stands in for windy until 28 Oct 19:00 UTC");
    const wind = screen.getByTestId("freshness-component-atmosphere");
    expect(wind).toHaveTextContent("ncep_gfs013,ncep_gfs025");
    expect(wind).toHaveTextContent("carried forward");
    expect(wind).toHaveTextContent("06Z 28 Oct (from run 7)");
    expect(wind).toHaveTextContent("5.4 h after the cycle (median of 4 runs)");
    expect(screen.getByTestId("freshness-component-wave")).toHaveTextContent("on time");

    expect(screen.getByTestId("freshness-last-detected")).toHaveTextContent("28 Oct 17:27 UTC");
    expect(screen.getByTestId("freshness-published")).toHaveTextContent("#42");
    expect(screen.getByTestId("freshness-spot-counts")).toHaveTextContent("47 of 50 · 3 stale · 3 failed in the last refresh");
    expect(screen.getByTestId("freshness-scheduler")).toHaveTextContent("checking");

    expect(screen.getByTestId("freshness-timeline")).toHaveTextContent("12 min");
    expect(within(screen.getByTestId("freshness-stale-spots")).getByRole("link", { name: "Pipeline" })).toHaveAttribute("href", "/spots/pipeline");
    expect(screen.getByTestId("freshness-failed-requests")).toHaveTextContent("HTTP 500");
    expect(screen.getByTestId("freshness-refresh-jobs")).toHaveTextContent("TransientProviderError: Open-Meteo HTTP 500");

    const usage = screen.getByTestId("freshness-usage");
    expect(within(usage).getByTestId("usage-open-meteo-day")).toHaveTextContent("8,500 of 10,000 (85 %) · near the limit");
    expect(usage).toHaveTextContent("Not counted by this build.");
    expect(within(usage).getByTestId("usage-flights-quota")).toHaveTextContent("7 of 300");
    expect(within(usage).getByTestId("usage-emails")).toHaveTextContent("3 today (1 alerts, 2 account)");
    expect(within(usage).getByTestId("usage-requests-24h")).toHaveTextContent("510");

    expect(screen.getByTestId("latency-upstream")).toHaveTextContent("p50 12 min");
    expect(screen.getByTestId("latency-frontend")).toHaveTextContent("p50 22 s · p95 33 s");
    // Spec 16 asks for the average processing time: every timing row shows it.
    expect(screen.getByTestId("processing-total")).toHaveTextContent("avg 12 s · p50 12 s");
    expect(screen.getByTestId("latency-frontend")).toHaveTextContent("avg 27 s");
    expect(screen.getByTestId("processing-stages")).toHaveTextContent("predict");
    expect(screen.getByTestId("freshness-notes")).toHaveTextContent("Counters live in Redis");
  });

  it("says plainly when nothing is published or measured yet", () => {
    render(
      <FreshnessView
        data={report({
          status: "ok",
          version_id: null,
          last_published_version: null,
          stale_spots: [],
          versions: [],
          refresh_jobs: [],
          failed_requests: [],
          problems: [],
          health_problems: [],
          latency: { upstream_to_publication_s: stats([]), publication_to_frontend_s: stats([]), first_served_s: stats([]) },
        })}
      />,
    );
    expect(screen.getByTestId("freshness-problems")).toHaveTextContent("No problems");
    expect(screen.getByTestId("freshness-published")).toHaveTextContent("none yet");
    expect(screen.getByTestId("freshness-stale-spots")).toHaveTextContent("No version published yet.");
    expect(screen.getByTestId("freshness-timeline")).toHaveTextContent("No forecast version yet.");
    expect(screen.getByTestId("latency-frontend")).toHaveTextContent("no sample yet");
  });

  it("lists the first stale spots and the rest on request", () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      spot_id: i + 1,
      slug: `spot-${i + 1}`,
      name: `Spot ${i + 1}`,
      last_updated_at: "2026-10-28T11:34:00Z",
      reason: "wave_age",
      error: null,
    }));
    render(<FreshnessView data={report({ stale_spots: many })} />);
    expect(screen.getAllByTestId("freshness-stale-spot")).toHaveLength(10);
    fireEvent.click(screen.getByTestId("freshness-stale-toggle"));
    expect(screen.getAllByTestId("freshness-stale-spot")).toHaveLength(12);
    expect(screen.getByTestId("freshness-stale-toggle")).toHaveTextContent("Show fewer");
  });

  it("tells a non-admin why the page is closed", () => {
    render(<NotAdmin />);
    expect(screen.getByTestId("freshness-forbidden")).toHaveTextContent("for admin accounts");
    expect(screen.getByRole("link", { name: "public status page" })).toHaveAttribute("href", "/status");
  });

  it("says which silence limit applies to a runner with a slower check interval", () => {
    const base = report().scheduler;
    render(<FreshnessView data={report({ scheduler: { ...base, limit_minutes: 180, sync_running: true } })} />);
    const row = screen.getByTestId("freshness-scheduler");
    expect(row).toHaveTextContent("expected at least every 180 min (3 × SYNC_CHECK_MINUTES; REFRESH_SCHEDULER_EXPECTED_MINUTES=30)");
    expect(row).toHaveTextContent("a sync is running");
  });
});

// --------------------------------------------------------------------------- data reads
function wrapper() {
  // The app's defaults (components/providers.tsx).
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("when the dashboard reads the database", () => {
  let versionId = 42;
  let calls: string[] = [];
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    versionId = 42;
    calls = [];
    vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      calls.push(path);
      if (path === VERSION_PATH) return { version_id: versionId } as never;
      if (path === FRESHNESS_PATH) return report({ version_id: versionId }) as never;
      throw new Error(`unexpected ${path}`);
    });
  });
  afterEach(() => {
    focusManager.setFocused(undefined);
    vi.useRealTimers();
    vi.restoreAllMocks();
  });
  const reads = () => calls.filter((p) => p === FRESHNESS_PATH).length;

  it("reads the report on mount, on focus and on a new version, never on a timer", async () => {
    const { result } = renderHook(() => useAdminFreshness(true, 20_000), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.report.data?.version_id).toBe(42));
    expect(reads()).toBe(1);
    // 15 minutes visible with an unchanged version: only the (Redis-only) version poll runs.
    await act(() => vi.advanceTimersByTimeAsync(15 * 60_000));
    expect(reads()).toBe(1);
    expect(calls.filter((p) => p === VERSION_PATH).length).toBeGreaterThanOrEqual(40);
    // Focus after the report went stale: read again.
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(reads()).toBe(2));
    // A new published version: read again, the previous report stays on screen meanwhile.
    versionId = 43;
    await act(() => vi.advanceTimersByTimeAsync(20_000 + 50));
    await waitFor(() => expect(result.current.report.data?.version_id).toBe(43));
    expect(reads()).toBe(3);
    expect(result.current.versionId).toBe(43);
  });

  it("still reads the report when the version endpoint is missing, and stops polling it", async () => {
    vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      calls.push(path);
      if (path === VERSION_PATH) throw new api.ApiError(404, "not_found", "Not Found");
      return report() as never;
    });
    const { result } = renderHook(() => useAdminFreshness(true, 20_000), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.report.data).toBeDefined());
    expect(result.current.versionKnown).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(5 * 60_000));
    expect(calls.filter((p) => p === VERSION_PATH).length).toBe(1);
    expect(reads()).toBe(1);
  });

  it("does not ask anything for a non-admin", async () => {
    renderHook(() => useAdminFreshness(false), { wrapper: wrapper() });
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(calls).toEqual([]);
  });

  it("polls the version at NEXT_PUBLIC_FORECAST_VERSION_POLL_MS", () => {
    expect(versionPollMs(undefined)).toBe(20_000);
    expect(versionPollMs("5000")).toBe(5000);
    expect(versionPollMs("20")).toBe(1000);
    expect(versionPollMs("soon")).toBe(20_000);
  });
});
