import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CpuMeter, SyncStatus } from "@/components/status/sync-status";
import {
  componentState,
  cpuBudget,
  formatCpuHours,
  formatDurationMs,
  publicationLine,
  publishedPhrase,
  runDurationMs,
  runStatus,
  stageLabel,
  type SyncComponent,
  type SyncHealth,
  type SyncRun,
  utcClock,
  utcDateTime,
} from "@/lib/sync";

// SYNTHETIC sync reports: shaped like GET /api/health/data .sync (backend app/schemas/sync.py),
// with invented times, run keys and durations. They describe no real sync.
const NOW = Date.parse("2026-10-18T16:00:00Z");
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString();

const STAGES: SyncRun["stages"] = [
  { name: "spots", status: "success", duration_ms: 3751, cpu_s: 0.2 },
  { name: "ensemble", status: "skipped", duration_ms: 0, cpu_s: 0 },
  { name: "map", status: "success", duration_ms: 21_070, cpu_s: 42.1 },
  { name: "blend", status: "skipped", duration_ms: 0, cpu_s: 0 },
  { name: "predict", status: "success", duration_ms: 116, cpu_s: 0.08 },
  { name: "publish", status: "success", duration_ms: 35, cpu_s: 0.02 },
  { name: "detect", status: "failed", duration_ms: 79, cpu_s: 0.05 },
];

const PUBLISHED: SyncRun = {
  id: 12,
  trigger: "daemon",
  status: "partial",
  started_at: iso(88),
  finished_at: iso(87.5),
  published_at: iso(87.6),
  anchor_cycle: "2026-10-18T12:00:00Z",
  stages: STAGES,
};

const COMPONENTS: SyncComponent[] = [
  {
    kind: "spots",
    source: "synthetic_gfs",
    provider: "open_meteo",
    state: "current",
    stored_run_key: "synthetic:2026101812",
    available_run_key: "synthetic:2026101812",
    available_cycle: "2026-10-18T12:00:00Z",
    detail: null,
  },
  {
    kind: "spots",
    source: "synthetic_ecmwf",
    provider: "open_meteo_ecmwf",
    state: "waiting",
    stored_run_key: "synthetic-ec:2026101800",
    available_run_key: "synthetic-ec:2026101812",
    available_cycle: "2026-10-18T12:00:00Z",
    detail: "waiting for synthetic_map 2026-10-18 12Z until 2026-10-18 16:30 UTC",
  },
  {
    kind: "map",
    source: "synthetic_map",
    provider: "map_fields",
    state: "failed",
    stored_run_key: "synthetic-map:2026101800",
    available_run_key: "synthetic-map:2026101812",
    available_cycle: "2026-10-18T12:00:00Z",
    detail: "6 failed attempts for synthetic-map:2026101812; waiting for the next run (SYNC_MAX_ATTEMPTS)",
  },
  { kind: "ensemble", source: "synthetic_gefs", provider: "gefs", state: "disabled", stored_run_key: null, available_run_key: null, available_cycle: null, detail: "GEFS_ENABLED=false" },
];

function sync(overrides: Partial<SyncHealth> = {}): SyncHealth {
  return {
    runner: "daemon",
    check_minutes: 10,
    last_check_at: iso(3),
    heartbeat_stale: false,
    last_run: PUBLISHED,
    last_published: PUBLISHED,
    components: COMPONENTS,
    cpu_hours_month: 2.4,
    cpu_budget_hours: null,
    ...overrides,
  };
}

describe("sync helpers", () => {
  it("writes UTC clock times, with the date when it is not the cycle's day", () => {
    expect(utcClock("2026-10-18T14:32:10Z")).toBe("14:32 UTC");
    expect(utcDateTime("2026-10-18T04:05:00Z")).toBe("Oct 18, 04:05 UTC");
    expect(publishedPhrase("2026-10-18T14:32:00Z", "2026-10-18T12:00:00Z")).toBe("published 14:32 UTC");
    expect(publishedPhrase("2026-10-19T02:10:00Z", "2026-10-18T18:00:00Z")).toBe("published Oct 19, 02:10 UTC");
    expect(publishedPhrase("2026-10-19T02:10:00Z")).toBe("published Oct 19, 02:10 UTC");
  });

  it("names the published model run, or only the publication without an anchor cycle", () => {
    expect(publicationLine("2026-10-18T12:00:00Z", "2026-10-18T14:32:00Z")).toBe("Updated with the 12Z Oct 18 model run · published 14:32 UTC");
    expect(publicationLine(null, "2026-10-18T14:32:00Z")).toBe("Published Oct 18, 14:32 UTC");
  });

  it("formats durations from milliseconds to hours", () => {
    expect(formatDurationMs(35)).toBe("35 ms");
    expect(formatDurationMs(3751)).toBe("3.8 s");
    expect(formatDurationMs(9_960)).toBe("10 s");
    expect(formatDurationMs(21_070)).toBe("21 s");
    expect(formatDurationMs(245_000)).toBe("4 min 5 s");
    expect(formatDurationMs(4_320_000)).toBe("1 h 12 min");
    expect(formatDurationMs(null)).toBe("—");
    expect(formatDurationMs(-1)).toBe("—");
  });

  it("measures a finished sync, a running one up to now, and nothing else", () => {
    expect(runDurationMs(PUBLISHED, NOW)).toBe(30_000);
    expect(runDurationMs({ status: "running", started_at: iso(2), finished_at: null }, NOW)).toBe(120_000);
    expect(runDurationMs({ status: "abandoned", started_at: iso(2), finished_at: null }, NOW)).toBeNull();
  });

  it("colours states, never green for an unknown or failed one", () => {
    expect(runStatus("success")).toEqual({ label: "Success", tone: "good" });
    expect(runStatus("abandoned").tone).toBe("danger");
    expect(runStatus("something_new")).toEqual({ label: "Something New", tone: "muted" });
    expect(componentState("waiting")).toEqual({ label: "Waiting", tone: "info" });
    expect(componentState("failed").tone).toBe("danger");
    expect(componentState("unknown").tone).toBe("warn");
    expect(componentState("disabled")).toEqual({ label: "Off", tone: "muted" });
    expect(stageLabel("predict")).toBe("Surf predictions");
    expect(stageLabel("later_stage")).toBe("Later Stage");
  });

  it("has a CPU budget only when one is set, and warns above 80 %", () => {
    expect(cpuBudget({ cpu_hours_month: 2.4, cpu_budget_hours: null })).toBeNull();
    expect(cpuBudget({ cpu_hours_month: 2.4, cpu_budget_hours: 0 })).toBeNull();
    expect(cpuBudget({ cpu_hours_month: null, cpu_budget_hours: 10 })).toBeNull();
    expect(cpuBudget({ cpu_hours_month: 2.4, cpu_budget_hours: 10 })).toEqual({ used: 2.4, budget: 10, fraction: 0.24, tone: "good" });
    expect(cpuBudget({ cpu_hours_month: 8, cpu_budget_hours: 10 })?.tone).toBe("good"); // alert is above 80 %
    expect(cpuBudget({ cpu_hours_month: 8.5, cpu_budget_hours: 10 })?.tone).toBe("warn");
    expect(cpuBudget({ cpu_hours_month: 12, cpu_budget_hours: 10 })).toMatchObject({ fraction: 1.2, tone: "danger" });
    expect(formatCpuHours(0.042)).toBe("0.04 h");
    expect(formatCpuHours(2.44)).toBe("2.4 h");
    expect(formatCpuHours(12.5)).toBe("12.5 h");
    expect(formatCpuHours(10)).toBe("10 h");
    expect(formatCpuHours(0)).toBe("0 h");
  });
});

describe("sync section", () => {
  it("renders nothing without a sync block (older backends)", () => {
    const { container, rerender } = render(<SyncStatus sync={undefined} now={NOW} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<SyncStatus sync={null} now={NOW} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the published run, the runner's last check, and the stages with durations", () => {
    render(<SyncStatus sync={sync()} now={NOW} sourceNames={new Map([["synthetic_gfs", "Synthetic GFS source"]])} />);
    expect(screen.getByTestId("sync-published")).toHaveTextContent("Updated with the 12Z Oct 18 model run · published 14:32 UTC");
    expect(screen.getByTestId("sync-last-check")).toHaveTextContent("Sync daemon · checks every 10 min · last check 3 minutes ago");
    expect(screen.queryByText("Not checking")).toBeNull();
    const last = screen.getByTestId("sync-last-run");
    expect(last).toHaveTextContent("Last sync");
    expect(last).toHaveTextContent("Partial");
    expect(last).toHaveTextContent("#12 · sync daemon · started 1 hour ago · took 30 s");
    const stages = within(screen.getByTestId("sync-stages")).getAllByRole("listitem");
    expect(stages.map((s) => s.textContent)).toEqual([
      "Spot forecasts3.8 s",
      "Ensembleskipped",
      "Map layers21 s",
      "Blendskipped",
      "Surf predictions116 ms",
      "Publish35 ms",
      "Swell detectionfailed",
    ]);
    expect(stages[6]).toHaveAttribute("data-status", "failed");
    // No budget configured: no meter.
    expect(screen.queryByTestId("sync-cpu")).toBeNull();
    expect(screen.queryByRole("meter")).toBeNull();
  });

  it("lists every component's state with its cycle, and the backend's detail (waiting until, failures)", () => {
    render(<SyncStatus sync={sync()} now={NOW} sourceNames={new Map([["synthetic_gfs", "Synthetic GFS source"]])} />);
    const list = screen.getByTestId("sync-components");
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    const gfs = screen.getByTestId("sync-component-spots-synthetic_gfs");
    expect(gfs).toHaveAttribute("data-state", "current");
    expect(gfs).toHaveTextContent("Spot forecasts");
    expect(gfs).toHaveTextContent("Synthetic GFS source · 12Z Oct 18 run");
    expect(gfs).toHaveTextContent("Current");
    const ec = screen.getByTestId("sync-component-spots-synthetic_ecmwf");
    expect(ec).toHaveTextContent("synthetic_ecmwf · 12Z Oct 18 run"); // no name known: the code
    expect(ec).toHaveTextContent("Waiting");
    expect(ec).toHaveTextContent("until 2026-10-18 16:30 UTC");
    const map = screen.getByTestId("sync-component-map-synthetic_map");
    expect(map).toHaveTextContent("World map layers");
    expect(map).toHaveTextContent("Failed");
    expect(map).toHaveTextContent("6 failed attempts");
    expect(within(map).getByText("synthetic_map · 12Z Oct 18 run")).toHaveAttribute("title", "stored synthetic-map:2026101800 · available synthetic-map:2026101812");
    const gefs = screen.getByTestId("sync-component-ensemble-synthetic_gefs");
    expect(gefs).toHaveTextContent("Off");
    expect(gefs).toHaveTextContent("GEFS_ENABLED=false");
  });

  it("shows a newer unpublished sync apart from the published one, and a running sync's time so far", () => {
    const running: SyncRun = { id: 13, trigger: "daemon", status: "running", started_at: iso(2), finished_at: null, published_at: null, anchor_cycle: null, stages: [STAGES[0]!] };
    render(<SyncStatus sync={sync({ last_run: running })} now={NOW} />);
    expect(screen.getByTestId("sync-published")).toHaveTextContent("Updated with the 12Z Oct 18 model run");
    const last = screen.getByTestId("sync-last-run");
    expect(last).toHaveTextContent("Newest sync");
    expect(last).toHaveTextContent("Running");
    expect(last).toHaveTextContent("#13 · sync daemon · started 2 minutes ago · running for 2 min 0 s");
    expect(within(last).getAllByRole("listitem")).toHaveLength(1);
  });

  it("says when nothing ran or was published, when the runner stopped checking, and when no components are reported", () => {
    render(<SyncStatus sync={sync({ last_run: null, last_published: null, last_check_at: null, heartbeat_stale: true, components: [] })} now={NOW} />);
    expect(screen.getByTestId("sync-published")).toHaveTextContent("Nothing published yet.");
    expect(screen.getByTestId("sync-last-run")).toHaveTextContent("No sync has run yet.");
    expect(screen.getByTestId("sync-last-check")).toHaveTextContent("no check recorded yet");
    expect(screen.getByTestId("sync-last-check")).toHaveTextContent("Not checking");
    expect(screen.getByTestId("sync-components")).toHaveTextContent("No check reported yet.");
  });

  it("explains that the Celery runner reports no component states", () => {
    render(<SyncStatus sync={sync({ runner: "celery", components: [] })} now={NOW} />);
    expect(screen.getByTestId("sync-last-check")).toHaveTextContent("Celery (sync.tick) · checks every 10 min");
    expect(screen.getByTestId("sync-components")).toHaveTextContent("The Celery runner reports no component states");
  });

  it("shows a published sync without an anchor cycle as a plain publication", () => {
    const forced: SyncRun = { ...PUBLISHED, id: 14, anchor_cycle: null, published_at: "2026-10-18T15:02:00Z" };
    render(<SyncStatus sync={sync({ last_run: forced, last_published: forced })} now={NOW} />);
    expect(screen.getByTestId("sync-published")).toHaveTextContent("Published Oct 18, 15:02 UTC");
  });
});

describe("CPU meter", () => {
  it("appears only with a budget, as a meter against it", () => {
    const { container } = render(<CpuMeter sync={sync({ cpu_budget_hours: null })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the share of the budget and the alert threshold", () => {
    render(<SyncStatus sync={sync({ cpu_hours_month: 2.4, cpu_budget_hours: 10 })} now={NOW} />);
    const cpu = screen.getByTestId("sync-cpu");
    expect(cpu).toHaveTextContent("CPU this month (UTC)");
    expect(cpu).toHaveTextContent("at least 2.4 h of 10 h (24 %)");
    expect(cpu).toHaveTextContent("alert above 80 %");
    const meter = screen.getByRole("meter", { name: "CPU used this month against the budget" });
    expect(meter).toHaveAttribute("aria-valuenow", "2.4");
    expect(meter).toHaveAttribute("aria-valuemax", "10");
    expect(meter).toHaveAttribute("data-tone", "good");
  });

  it("turns amber above the alert threshold and red at the budget, the bar capped at full", () => {
    const { rerender } = render(<CpuMeter sync={sync({ cpu_hours_month: 8.6, cpu_budget_hours: 10 })} />);
    expect(screen.getByRole("meter")).toHaveAttribute("data-tone", "warn");
    rerender(<CpuMeter sync={sync({ cpu_hours_month: 12.5, cpu_budget_hours: 10 })} />);
    const meter = screen.getByRole("meter");
    expect(meter).toHaveAttribute("data-tone", "danger");
    expect(meter).toHaveAttribute("aria-valuenow", "10");
    expect(meter).toHaveAttribute("aria-valuetext", "12.5 h of 10 h, 125 %");
    expect((meter.firstElementChild as HTMLElement).style.width).toBe("100%");
    expect(screen.getByTestId("sync-cpu")).toHaveTextContent("at least 12.5 h of 10 h (125 %)");
  });
});
