import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ForecastConfidence } from "@/components/confidence";
import { ForecastProvenance } from "@/components/forecast-provenance";
import { componentFetchedText, componentRuns, componentRunsText, hoursAfterWaveRun } from "@/lib/model-runs";
import type { RunMember } from "@/types/api";

// SYNTHETIC run members (review r1-frontend-FE-1): a wave-only refresh published the 07Z wave
// run beside the carried 01Z wind run. Nothing here describes a real forecast.

const base = { code: "demo", name: "Synthetic source", family: "demo", wave_model: "Synthetic waves", atmosphere_model: "Synthetic wind" };
const wave: RunMember = { ...base, run_key: "w:2026102907", init_time: "2026-10-29T07:00:00Z", component: "wave", retrieved_at: "2026-10-29T11:40:00Z" };
const wind: RunMember = { ...base, run_key: "a:2026102901", init_time: "2026-10-29T01:00:00Z", component: "atmosphere", retrieved_at: "2026-10-29T05:40:00Z" };

describe("component model runs", () => {
  it("names both runs of a composed forecast, never only the older one", () => {
    const runs = componentRuns([wave, wind]);
    expect(runs).not.toBeNull();
    expect(componentRunsText(runs!)).toBe("Wave run 07Z Oct 29 · Wind run 01Z Oct 29");
    expect(componentFetchedText(runs!, new Date("2026-10-29T12:00:00Z"))).toBe("waves 20 minutes ago · wind 6 hours ago");
    expect(hoursAfterWaveRun(runs!, Date.parse("2026-10-29T17:00:00Z"))).toBe(10);
  });

  it("is null for a forecast whose components share one run", () => {
    expect(componentRuns([{ ...base, run_key: "x", init_time: "2026-10-29T06:00:00Z" }])).toBeNull();
    expect(componentRuns([wave])).toBeNull();
    expect(componentRuns(null)).toBeNull();
  });

  it("gives one fetch time when both components were fetched together", () => {
    const together = componentRuns([wave, { ...wind, retrieved_at: "2026-10-29T11:40:20Z" }]);
    expect(componentFetchedText(together!, new Date("2026-10-29T12:00:00Z"))).toBe("20 minutes ago");
  });

  it("shows each component's run and fetch time in the provenance", () => {
    render(
      <ForecastProvenance
        source={{ name: "Synthetic source", is_demo: false }}
        run={{ issued_at: "2026-10-29T01:00:00Z", completed_at: "2026-10-29T11:40:00Z" }}
        members={[wave, wind]}
        generatedAt="2026-10-29T12:00:00Z"
      />,
    );
    const runs = screen.getByTestId("provenance-members");
    expect(runs).toHaveTextContent("Wave run 07Z Oct 29 · Wind run 01Z Oct 29");
    expect(within(runs).getAllByTestId("provenance-member")).toHaveLength(2);
    expect(screen.getByTestId("provenance-fetched")).toHaveTextContent("Fetched waves");
    expect(screen.getByTestId("provenance-fetched")).toHaveTextContent("wind");
  });

  it("names both model-run ages in the confidence details", () => {
    render(
      <ForecastConfidence
        value="moderate"
        runIssuedAt="2026-10-29T01:00:00Z"
        waveRunIssuedAt="2026-10-29T07:00:00Z"
        windRunIssuedAt="2026-10-29T01:00:00Z"
        nowMs={Date.parse("2026-10-29T08:00:00Z")}
      />,
    );
    expect(screen.getByTestId("forecast-confidence-timing")).toHaveTextContent("Wave model run 1 h ago · wind model run 7 h ago.");
  });

  it("keeps one age when the runs are the same", () => {
    render(
      <ForecastConfidence
        value="moderate"
        runIssuedAt="2026-10-29T01:00:00Z"
        waveRunIssuedAt="2026-10-29T01:00:00Z"
        windRunIssuedAt="2026-10-29T01:00:00Z"
        nowMs={Date.parse("2026-10-29T08:00:00Z")}
      />,
    );
    expect(screen.getByTestId("forecast-confidence-timing")).toHaveTextContent("Model run 7 h ago.");
  });
});
