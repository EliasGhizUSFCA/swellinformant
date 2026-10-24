import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ageLine,
  type DataHealth,
  dataHealthRefetchMs,
  DataHealthSummary,
  formatBytes,
  formatHours,
  freshnessTone,
  heartbeatLine,
  JobTable,
  LayersAndObservations,
  overallTone,
  redisLine,
  SourceFreshness,
  type SourceHealth,
  SyncCard,
} from "@/app/status/data-health";
import type { SyncComponent, SyncHealth, SyncRun } from "@/lib/sync";

// SYNTHETIC test data: an invented data-health report that exists only to exercise the
// status-page components. It does not describe a real run, job or buoy.
const now = new Date().toISOString();

function report(overrides: Partial<DataHealth> = {}): DataHealth {
  return {
    status: "degraded",
    checked_at: now,
    reasons: ["synthetic source: newest run synthetic:1 was issued 17.0 h ago (stale after 16.0 h)."],
    problems: [
      {
        key: "forecast_stale:synthetic",
        severity: "degraded",
        message: "synthetic source: newest run synthetic:1 was issued 17.0 h ago (stale after 16.0 h).",
      },
    ],
    forecast_sources: [
      {
        code: "synthetic",
        name: "Synthetic source",
        role: "forecast",
        status: "stale",
        newest_run_key: "synthetic:1",
        newest_issued_at: now,
        age_hours: 17,
        stale_after_hours: 16,
        latest_attempt_status: "failed",
        latest_attempt_started_at: now,
      },
      {
        code: "blend",
        name: "Synthetic blend",
        role: "blend",
        status: "ok",
        newest_run_key: "blend:1",
        newest_issued_at: now,
        age_hours: 2,
        stale_after_hours: null,
        latest_attempt_status: "success",
        latest_attempt_started_at: now,
      },
    ],
    jobs: [
      {
        job: "forecasts.acquire",
        schedule: "every 3 h",
        status: "failed",
        last_status: "failed",
        last_started_at: now,
        last_finished_at: now,
        last_duration_ms: 4200,
        last_success_at: null,
        consecutive_failures: 3,
        overdue_after_minutes: 390,
      },
      {
        job: "maintenance.cleanup",
        schedule: "daily at 03:30 UTC",
        status: "never_run",
        last_status: null,
        last_started_at: null,
        last_finished_at: null,
        last_duration_ms: null,
        last_success_at: null,
        consecutive_failures: 0,
        overdue_after_minutes: 2910,
      },
    ],
    map_fields: {
      mode: "with_noaa",
      status: "ok",
      run_key: "synthetic:1",
      cycle_time: now,
      age_hours: 5,
      stale_after_hours: 16,
      complete_valid_times: 81,
      last_valid_time: now,
    },
    observations: {
      enabled: true,
      providers: [{ provider: "ndbc", status: "missing", active_stations: 0, newest_observed_at: null, age_hours: null, stale_after_hours: 48 }],
    },
    heartbeat: { last_job_finished_at: now, age_minutes: 1, limit_minutes: 30 },
    storage: { database_bytes: 193_035_287, database_max_gb: null },
    ...overrides,
  };
}

describe("data health helpers", () => {
  it("maps the overall status to a tone and label", () => {
    expect(overallTone("ok")).toEqual({ tone: "good", alert: "success", label: "All data checks pass" });
    expect(overallTone("degraded").alert).toBe("warn");
    expect(overallTone("failing")).toMatchObject({ tone: "danger", label: "Failing" });
  });

  it("colours states, never green for an unknown one", () => {
    expect(freshnessTone("ok")).toBe("good");
    expect(freshnessTone("stale")).toBe("warn");
    expect(freshnessTone("overdue")).toBe("warn");
    expect(freshnessTone("stuck")).toBe("danger");
    expect(freshnessTone("missing")).toBe("danger");
    expect(freshnessTone("never_run")).toBe("muted");
    expect(freshnessTone("something-new")).toBe("muted");
  });

  it("formats ages", () => {
    expect(formatHours(0.25)).toBe("15 min");
    expect(formatHours(3.42)).toBe("3.4 h");
    expect(formatHours(50)).toBe("2.1 days");
    expect(formatHours(null)).toBe("—");
    expect(formatBytes(193_035_287)).toBe("184 MB");
    expect(formatBytes(13.2 * 1024 ** 3)).toBe("13.2 GB");
  });

  it("states the stale limit, or the blend rule", () => {
    const [source, blend] = report().forecast_sources as [SourceHealth, SourceHealth];
    expect(ageLine(source)).toBe("Run synthetic:1 · issued 17.0 h ago · stale after 16.0 h");
    expect(ageLine(blend)).toContain("stale when any member is stale");
    expect(ageLine({ ...source, newest_run_key: null, age_hours: null })).toBe("No successful run yet");
  });
});

describe("data health components", () => {
  it("shows the reasons, and says the limits are not accuracy", () => {
    render(<DataHealthSummary data={report()} />);
    const summary = screen.getByTestId("data-health-summary");
    expect(summary).toHaveTextContent("Data health: Degraded");
    expect(within(screen.getByTestId("data-health-reasons")).getAllByRole("listitem")).toHaveLength(1);
    expect(summary).toHaveTextContent("not a measure of forecast accuracy");
  });

  it("marks failing problems and the all-clear", () => {
    const failing = report({
      status: "failing",
      problems: [{ key: "forecasts_unavailable", severity: "failing", message: "No forecast source has a fresh run." }],
    });
    const { unmount } = render(<DataHealthSummary data={failing} />);
    expect(screen.getByTestId("data-health-reasons")).toHaveTextContent("Failing: No forecast source has a fresh run.");
    unmount();
    render(<DataHealthSummary data={report({ status: "ok", reasons: [], problems: [] })} />);
    expect(screen.getByTestId("data-health-summary")).toHaveTextContent("All data checks pass");
  });

  it("lets long run keys wrap so phones do not scroll sideways", () => {
    // Real Open-Meteo run keys are one unbreakable token, e.g. "gfswave016:2026102106|gfswave025:2026102106|gfs013:2026102106|gfs025:2026102106".
    const key = "synthetic016:2026102106|synthetic025:2026102106|synthetic013:2026102106|synthetic025:2026102106";
    const message = `Synthetic source: newest run ${key} was issued 20.6 h ago (stale after 17.2 h).`;
    const data = report({ problems: [{ key: "forecast_stale:synthetic", severity: "degraded", message }] });
    render(<DataHealthSummary data={data} />);
    expect(screen.getByTestId("data-health-reasons")).toHaveClass("wrap-anywhere");
    const [source] = data.forecast_sources as [SourceHealth];
    render(<SourceFreshness sources={[{ ...source, newest_run_key: key }]} />);
    expect(screen.getByText(new RegExp(`Run ${key.replace(/\|/g, "\\|")}`))).toHaveClass("wrap-anywhere");
  });

  it("lists sources with their age, limit, role and a failed attempt", () => {
    render(<SourceFreshness sources={report().forecast_sources} />);
    const list = screen.getByTestId("data-health-sources");
    expect(list).toHaveTextContent("Synthetic source");
    expect(list).toHaveTextContent("stale after 16.0 h");
    expect(list).toHaveTextContent("spot forecast source");
    expect(list).toHaveTextContent("multi-model blend");
    expect(list).toHaveTextContent("Latest attempt");
  });

  it("shows job schedules, states and repeated failures", () => {
    render(<JobTable jobs={report().jobs} />);
    const table = screen.getByTestId("data-health-jobs");
    expect(table).toHaveTextContent("every 3 h");
    expect(table).toHaveTextContent("Failed");
    expect(table).toHaveTextContent("3 failures in a row");
    expect(table).toHaveTextContent("Never Run");
    expect(table).toHaveTextContent("4.2 s");
  });

  it("labels the map layers as offshore model fields, not surf height", () => {
    render(<LayersAndObservations data={report()} />);
    const card = screen.getByTestId("data-health-other");
    expect(card).toHaveTextContent("offshore model fields, not surf height");
    expect(card).toHaveTextContent("NDBC · 0 active stations · none stored");
    expect(card).toHaveTextContent("alarm after 30 min");
    expect(card).toHaveTextContent("Database 184 MB (no size alarm configured)");
    expect(screen.queryByTestId("data-health-redis")).toBeNull(); // older reports have no Redis block
  });

  it("renders the Redis memory check against its cap", () => {
    const redis = {
      available: true,
      used_memory_bytes: 230 * 1024 ** 2,
      maxmemory_bytes: 256 * 1024 ** 2,
      maxmemory_policy: "noeviction",
      cap_bytes: 256 * 1024 ** 2,
      cap_source: "redis" as const,
      used_fraction: 0.8984,
      alert_fraction: 0.8,
    };
    render(<LayersAndObservations data={report({ storage: { database_bytes: 193_035_287, database_max_gb: 5, redis } })} />);
    expect(screen.getByTestId("data-health-other")).toHaveTextContent("Database 184 MB (alarm above 5 GB)");
    expect(screen.getByTestId("data-health-redis")).toHaveTextContent("Redis 230 MB of 256 MB (90 %, alarm above 80 %)");
  });

  it("labels a cap that is only the configured REDIS_MAXMEMORY, and an unreachable Redis", () => {
    const uncapped = { available: true, used_memory_bytes: 1024 ** 2, cap_bytes: 256 * 1024 ** 2, cap_source: "setting" as const, used_fraction: 0.0039, alert_fraction: 0.8 };
    expect(redisLine(uncapped)).toBe("Redis 1 MB of 256 MB (REDIS_MAXMEMORY; Redis itself runs uncapped) (0.4 %, alarm above 80 %)");
    expect(redisLine({ available: false, alert_fraction: 0.8 })).toBe("Redis is not answering");
    expect(redisLine({ available: true, used_memory_bytes: 1024 ** 2, alert_fraction: 0.8 })).toBe("Redis 1 MB (no memory cap known, alarm above 80 %)");
  });
});

describe("sync section on the status page", () => {
  // SYNTHETIC sync block (backend app/schemas/sync.py shape); invented run keys and times.
  const sync: SyncHealth = {
    runner: "daemon",
    check_minutes: 10,
    last_check_at: now,
    heartbeat_stale: false,
    last_run: null,
    last_published: null,
    components: [
      {
        kind: "spots",
        source: "synthetic",
        provider: "open_meteo",
        state: "new",
        stored_run_key: "synthetic:1",
        available_run_key: "synthetic:2",
        available_cycle: "2026-10-18T12:00:00Z",
        detail: null,
      },
    ],
    cpu_hours_month: 0.5,
    cpu_budget_hours: null,
    cpu_alert_fraction: 0.8,
  };

  it("is absent when the backend sends no sync block (older backends)", () => {
    const { container, rerender } = render(<SyncCard data={report()} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<SyncCard data={report({ sync: null })} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("sync-status")).toBeNull();
  });

  it("never polls under an idle sync daemon; every minute while a sync runs or without the daemon", () => {
    expect(dataHealthRefetchMs(undefined)).toBe(60_000);
    expect(dataHealthRefetchMs(report())).toBe(60_000); // older backend: no sync block
    expect(dataHealthRefetchMs(report({ sync: { ...sync, runner: "celery" } }))).toBe(60_000);
    expect(dataHealthRefetchMs(report({ sync }))).toBe(false);
    const run: SyncRun = { id: 3, trigger: "daemon", status: "running", started_at: now, finished_at: null, published_at: null, anchor_cycle: null, stages: [] };
    expect(dataHealthRefetchMs(report({ sync: { ...sync, last_run: run } }))).toBe(60_000);
    expect(dataHealthRefetchMs(report({ sync: { ...sync, last_run: { ...run, status: "success", finished_at: now } } }))).toBe(false);
  });

  it("renders the sync block with the report's source names", () => {
    render(<SyncCard data={report({ sync })} />);
    expect(screen.getByTestId("sync-status")).toHaveTextContent("Forecast sync");
    const row = screen.getByTestId("sync-component-spots-synthetic");
    expect(row).toHaveTextContent("Synthetic source · newest run 12Z Oct 18");
    expect(row).toHaveTextContent("New run");
    expect(screen.getByTestId("sync-published")).toHaveTextContent("Nothing published yet.");
  });

  it("shows the component states as the backend reports them", () => {
    // The daemon settles its components after a sync (backend app/services/sync/detect.py
    // settle_plan), so the page no longer second-guesses them: a "new" component stays new
    // even when the report lists that run as the source's newest stored run.
    const map: SyncComponent = {
      kind: "map",
      source: "synthetic_fields",
      source_name: "Synthetic fields",
      provider: "map_fields",
      state: "current",
      stored_run_key: "synthetic-map:1",
      available_run_key: "synthetic-map:1",
      available_cycle: "2026-10-18T12:00:00Z",
      detail: "stored by sync 5",
    };
    const done: SyncRun = { id: 5, trigger: "daemon", status: "success", started_at: now, finished_at: now, published_at: now, anchor_cycle: null, stages: [] };
    const base = report({ sync: { ...sync, components: [...sync.components!, map], last_run: done, last_published: done } });
    render(<SyncCard data={{ ...base, forecast_sources: base.forecast_sources.map((s) => (s.code === "synthetic" ? { ...s, newest_run_key: "synthetic:2" } : s)) }} />);
    expect(screen.getByTestId("sync-component-spots-synthetic")).toHaveAttribute("data-state", "new");
    const row = screen.getByTestId("sync-component-map-synthetic_fields");
    expect(row).toHaveAttribute("data-state", "current");
    expect(row).toHaveTextContent("Synthetic fields · newest run 12Z Oct 18");
    expect(row).toHaveTextContent("stored by sync 5");
  });

  it("promises no job-silence alarm under the sync daemon, which is judged by its heartbeat", () => {
    const finished = new Date(Date.now() - 180 * 60_000).toISOString();
    const celery = { heartbeat: { last_job_finished_at: finished, age_minutes: 180, limit_minutes: 30 }, sync: { ...sync, runner: "celery" as const } };
    expect(heartbeatLine(celery)).toBe("Last background job finished 3 hours ago (alarm after 30 min of silence)");
    expect(heartbeatLine({ ...celery, sync: null })).toContain("alarm after 30 min of silence"); // older backend
    const daemon = { ...celery, sync };
    expect(heartbeatLine(daemon)).toBe("Last background job finished 3 hours ago (the sync daemon's own heartbeat is checked instead: Forecast sync)");
    expect(heartbeatLine({ ...daemon, heartbeat: { ...daemon.heartbeat, last_job_finished_at: null } })).not.toContain("alarm");
    render(<LayersAndObservations data={report({ sync, heartbeat: daemon.heartbeat })} />);
    expect(screen.getByTestId("data-health-heartbeat")).not.toHaveTextContent("alarm after");
  });
});
