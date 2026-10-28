import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ForecastBreakdown } from "@/components/forecast-breakdown";
import { drivingComponent, pointToConditions } from "@/lib/forecast";
import type { ForecastPoint, SpotForecast } from "@/types/api";

// SYNTHETIC forecast hour and spot forecast: invented values and reasons that exist only to
// exercise the conversion. Nothing here describes a real forecast or a real surf spot.
function point(overrides: Partial<ForecastPoint> = {}): ForecastPoint {
  return {
    valid_time: "2026-10-14T06:00:00Z",
    is_daylight: true,
    sig_wave_height_m: 2.1,
    primary_swell_height_m: 1.9,
    primary_swell_period_s: 14,
    primary_swell_direction_deg: 215,
    secondary_swell_height_m: null,
    secondary_swell_period_s: null,
    secondary_swell_direction_deg: null,
    wind_wave_height_m: null,
    wind_wave_period_s: null,
    wind_speed_kmh: 10,
    wind_direction_deg: 290,
    wind_gust_kmh: null,
    pressure_msl_hpa: null,
    sea_level_m: null,
    wave_power_kw_m: null,
    breaking_height_min_ft: 4,
    breaking_height_max_ft: 6,
    score: 72,
    label: "very_good",
    confidence: 80,
    confidence_label: "high",
    wind_relation: "offshore",
    explanation: "Synthetic explanation.",
    components: {},
    forecast_confidence_class: "high",
    local_confidence_class: "low",
    confidence_reasons: ["Synthetic: models agree.", "Synthetic: this break's research is thin."],
    forecast_confidence_reasons: ["Synthetic: models agree."],
    lead_hours: 18,
    local_confidence_reason: "Synthetic: the API's own reason for this hour.",
    ...overrides,
  };
}

const FORECAST = {
  spot_slug: "synthetic-spot",
  timezone: "UTC",
  available: true,
  stale: false,
  calibrated: false,
  points: [],
  daily: [],
  models: [],
  ensemble: [],
  alerts_paused: false,
} as SpotForecast;

describe("pointToConditions", () => {
  it("keeps the API's local-confidence reason", () => {
    const c = pointToConditions(point(), FORECAST);
    expect(c.local_confidence_reason).toBe("Synthetic: the API's own reason for this hour.");
    expect(c.local_confidence_class).toBe("low");
    expect(c.confidence_reasons).toEqual(["Synthetic: models agree.", "Synthetic: this break's research is thin."]);
  });

  it("maps a missing reason to null", () => {
    expect(pointToConditions(point({ local_confidence_reason: undefined }), FORECAST).local_confidence_reason).toBeNull();
    expect(pointToConditions(point({ local_confidence_reason: null }), FORECAST).local_confidence_reason).toBeNull();
  });

  it("never shows the deprecated combined reason in the spot page's breakdown (spec 20)", () => {
    render(<ForecastBreakdown c={pointToConditions(point(), FORECAST)} tz="UTC" units="ft" />);
    expect(screen.queryByTestId("local-confidence-reason")).toBeNull();
    expect(screen.getByTestId("forecast-breakdown")).not.toHaveTextContent("Synthetic: the API's own reason for this hour.");
    expect(screen.getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: High");
  });
});

describe("the swell that drives the surf (CH-8)", () => {
  // SYNTHETIC breakdown: a 0.4 m primary swell remnant under a 1.6 m wind sea, as WW3 ranks
  // swells by offshore height only. The backend picks the largest predicted breaking height.
  const components = {
    breaking: {
      partitions: [
        { name: "primary_swell", height_m: 0.4, period_s: 13, direction_deg: 210, breaking_m: 0.35 },
        { name: "wind_sea", height_m: 1.6, period_s: 6, direction_deg: 250, breaking_m: 0.9 },
        { name: "secondary_swell", height_m: 0.2, period_s: 9, direction_deg: 180, breaking_m: 0 },
      ],
    },
  };

  it("uses the partition with the largest breaking height, not the primary swell", () => {
    const c = pointToConditions(point({ primary_swell_height_m: 0.4, primary_swell_period_s: 13, primary_swell_direction_deg: 210, components }), FORECAST);
    expect([c.swell_height_m, c.swell_period_s, c.swell_direction_deg]).toEqual([1.6, 6, 250]);
  });

  it("falls back to the bulk sea state without a breakdown", () => {
    expect(drivingComponent(point({ sig_wave_height_m: 2.1, wave_period_s: 11, wave_direction_deg: 230, components: {} }))).toEqual({ height: 2.1, period: 11, direction: 230 });
    expect(drivingComponent(point({ components: { breaking: { partitions: [{ name: "primary_swell", height_m: 1, breaking_m: 0 }] } } })).height).toBe(2.1);
  });
});
