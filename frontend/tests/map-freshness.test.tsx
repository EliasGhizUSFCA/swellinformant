import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MapFreshness, mapStaleness, ModelRunLabel } from "@/components/map/map-freshness";
import type { FieldIndex } from "@/lib/map-fields";
import type { MapSnapshot } from "@/types/api";

// SYNTHETIC index and snapshot: shapes of the API contract, not real model runs.
const CYCLE = "2026-10-11T18:00:00Z";
const INDEX: FieldIndex = {
  available: true,
  message: null,
  source: { code: "noaa_gfs_direct", name: "NOAA GFS-Wave + GFS (direct from NOAA open data)", attribution: "NOAA", is_demo: false },
  run_key: "noaa_gfs_direct:2026101118",
  cycle_time: CYCLE,
  valid_times: [CYCLE, "2026-10-12T00:00:00Z"],
  variables: [],
  updated_at: "2026-10-11T22:40:00Z",
  age_hours: 6,
  stale_after_hours: 12,
  stale: false,
};
const SNAPSHOT: MapSnapshot = {
  requested_at: null,
  valid_time: "2026-10-12T00:00:00Z",
  range_start: null,
  range_end: null,
  step_hours: 3,
  spots: [],
  stale: false,
  newest_run_issued_at: "2026-10-11T18:00:00Z",
  last_updated_at: "2026-10-11T22:00:00Z",
  attribution_links: [],
};
const NOW = Date.parse("2026-10-12T00:00:00Z");

describe("map freshness", () => {
  it("shows nothing while the model runs are fresh", () => {
    const { container } = render(<MapFreshness index={INDEX} snapshot={SNAPSHOT} now={NOW} />);
    expect(container).toBeEmptyDOMElement();
    expect(mapStaleness(INDEX, SNAPSHOT, NOW)).toEqual({ stale: false, ageHours: null, limitHours: 12 });
  });

  it("names the age of a stale ocean-layer run", () => {
    render(<MapFreshness index={{ ...INDEX, stale: true, age_hours: 54.2 }} snapshot={SNAPSHOT} now={NOW} />);
    expect(screen.getByRole("status")).toHaveTextContent("Forecast out of date — model run 54 h old");
  });

  it("counts a stale spot-prediction run from its issue time, and takes the older of the two", () => {
    const snap = { ...SNAPSHOT, stale: true, newest_run_issued_at: "2026-10-10T12:00:00Z" }; // 36 h before NOW
    expect(mapStaleness(INDEX, snap, NOW).ageHours).toBeCloseTo(36, 6);
    render(<MapFreshness index={{ ...INDEX, stale: true, age_hours: 20 }} snapshot={snap} now={NOW} />);
    expect(screen.getByRole("status")).toHaveTextContent("model run 36 h old");
  });

  it("falls back to the limit when no age is known, and ignores an unavailable index", () => {
    render(<MapFreshness index={{ ...INDEX, stale: true, age_hours: null }} snapshot={null} now={NOW} />);
    expect(screen.getByRole("status")).toHaveTextContent("Forecast out of date — model run over 12 h old");
    const off = { ...INDEX, available: false, stale: true, age_hours: 99 };
    expect(mapStaleness(off, SNAPSHOT, NOW).stale).toBe(false);
  });
});

describe("model run label", () => {
  it("gives the run and the lead time of the selected step", () => {
    render(<ModelRunLabel index={INDEX} at="2026-10-12T00:00:00Z" />);
    expect(screen.getByTestId("map-lead-time")).toHaveTextContent("Model run GFS 18Z Oct 11 · +6 h");
    expect(screen.getByTestId("map-lead")).toHaveTextContent("+6 h");
  });

  it("calls the run's own time the analysis", () => {
    render(<ModelRunLabel index={INDEX} at={CYCLE} />);
    expect(screen.getByTestId("map-lead")).toHaveTextContent("analysis");
  });

  it("is absent without ocean layers or a cycle time", () => {
    const { container, rerender } = render(<ModelRunLabel index={{ ...INDEX, available: false }} at={CYCLE} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<ModelRunLabel index={{ ...INDEX, cycle_time: null }} at={CYCLE} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<ModelRunLabel index={undefined} at={CYCLE} />);
    expect(container).toBeEmptyDOMElement();
  });
});
