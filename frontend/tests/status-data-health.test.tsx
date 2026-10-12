import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ageLine,
  type DataHealth,
  DataHealthSummary,
  formatHours,
  freshnessTone,
  JobTable,
  LayersAndObservations,
  overallTone,
  SourceFreshness,
  type SourceHealth,
} from "@/app/status/data-health";

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
  });
});
