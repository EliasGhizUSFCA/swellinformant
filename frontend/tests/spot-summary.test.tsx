import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AttributionLinks } from "@/components/forecast-provenance";
import { ModelInfo, RatingDetails, TidePreference } from "@/components/forecast/rating-details";
import { ConditionLists, RatingHeadline, WhyThisRating } from "@/components/rating";
import { StaleBanner } from "@/components/stale-banner";
import { WINDY_CREDIT } from "@/lib/provider";
import type { Conditions, ForecastPoint, SpotDetail, SpotForecast } from "@/types/api";

// SYNTHETIC spot, hour and forecast: invented values shaped like the API's, for the
// components only. Nothing here describes a real forecast or a real surf spot.
function conditions(over: Partial<Conditions> = {}): Conditions {
  return {
    valid_time: "2026-10-28T06:00:00Z",
    score: 89,
    label: "excellent",
    breaking_height_min_ft: 4,
    breaking_height_max_ft: 6,
    swell_height_m: 1.8,
    swell_period_s: 15,
    swell_direction_deg: 225,
    wind_speed_kmh: 8,
    wind_direction_deg: 45,
    wind_relation: "offshore",
    confidence: 70,
    confidence_label: "moderate",
    is_daylight: true,
    sig_wave_height_m: 2,
    source_code: "open_meteo_noaa",
    source_name: "Synthetic source",
    is_demo: false,
    run_issued_at: "2026-10-28T00:00:00Z",
    method: "linear_wave_v2",
    algorithm_version: "2.2.0",
    explanation: "Synthetic explanation.",
    forecast_confidence_class: "moderate",
    confidence_reasons: [],
    forecast_confidence_reasons: [],
    ...over,
  } as Conditions;
}

function point(over: Partial<ForecastPoint> = {}): ForecastPoint {
  return {
    valid_time: "2026-10-28T06:00:00Z",
    is_daylight: true,
    sig_wave_height_m: 2,
    wave_period_s: 15,
    wave_direction_deg: 225,
    primary_swell_height_m: 1.8,
    primary_swell_period_s: 15,
    primary_swell_direction_deg: 225,
    secondary_swell_height_m: 0.4,
    secondary_swell_period_s: 9,
    secondary_swell_direction_deg: 180,
    wind_wave_height_m: null,
    wind_wave_period_s: null,
    wind_speed_kmh: 8,
    wind_direction_deg: 45,
    wind_gust_kmh: 14,
    pressure_msl_hpa: 1015,
    sea_level_m: null,
    wave_power_kw_m: null,
    breaking_height_min_ft: 4,
    breaking_height_max_ft: 6,
    score: 89,
    label: "excellent",
    confidence: 70,
    confidence_label: "moderate",
    wind_relation: "offshore",
    explanation: "Synthetic explanation.",
    components: { tide: 0.8 },
    ...over,
  } as ForecastPoint;
}

const spot = {
  slug: "synthetic-point",
  name: "Synthetic Point (test fixture)",
  timezone: "UTC",
  tide_preference: "mid",
  research_class: "moderate",
  research: {
    research_class: "moderate",
    summary: "Synthetic summary.",
    gaps: [],
    variables: [],
    sources: [
      { title: "Synthetic Guide A", url: "https://example.org/a", publisher: "Example Press", source_type: "guidebook", published_year: 2020, accessed_at: null },
      { title: "Synthetic Guide B", url: "javascript:alert(1)", publisher: "Example Press", source_type: "guidebook", published_year: null, accessed_at: null },
    ],
    methodology_version: "synthetic-v1",
    assessed_at: "2026-10-01T00:00:00Z",
    research_updated_at: null,
  },
} as unknown as SpotDetail;

function forecast(code: string, over: Partial<SpotForecast> = {}): SpotForecast {
  return {
    spot_slug: "synthetic-point",
    timezone: "UTC",
    available: true,
    stale: false,
    calibrated: false,
    points: [],
    daily: [],
    models: [],
    ensemble: [],
    alerts_paused: false,
    source: { code, name: `Synthetic ${code}`, is_demo: false, attribution: "", wave_model: "gfsWave", atmosphere_model: "gfs", attribution_links: [] },
    run: { run_key: "r", issued_at: "2026-10-28T00:00:00Z", completed_at: "2026-10-28T04:00:00Z", wave_model_run_at: "2026-10-28T00:00:00Z", atmosphere_model_run_at: "2026-10-27T18:00:00Z", status: "success", members: [] },
    ...over,
  } as SpotForecast;
}

describe("the rating as one sentence (spec 25)", () => {
  it('reads "Excellent Surf — 8.9/10 — <reason>" inline, the reason its own element', () => {
    render(<RatingHeadline label="excellent" score={89} reason="Well-aligned swell and light offshore wind." size="lg" inline />);
    const h = screen.getByTestId("rating-headline");
    expect(h).toHaveTextContent("Excellent Surf — 8.9/10 — Well-aligned swell and light offshore wind.");
    expect(screen.getByTestId("rating-title")).toHaveTextContent("Excellent Surf — 8.9/10");
    expect(screen.getByTestId("rating-reason")).toHaveTextContent("Well-aligned swell and light offshore wind.");
  });

  it("has no trailing dash without a reason", () => {
    render(<RatingHeadline label="poor" score={12} inline />);
    expect(screen.getByTestId("rating-headline").textContent?.trim()).toBe("Poor Surf — 1.2/10");
  });
});

describe("Why this rating? (spec 25)", () => {
  it("lists what helps and what holds the rating back, in the viewer's units", () => {
    render(<ConditionLists favourable={["good size for this break (4–6 ft)"]} unfavourable={["onshore wind 25 km/h"]} units="m" />);
    expect(screen.getByTestId("rating-favourable")).toHaveTextContent("good size for this break (1.2–1.8 m)");
    expect(screen.getByTestId("rating-unfavourable")).toHaveTextContent("onshore wind 25 km/h");
  });

  it("shows nothing for hours scored before the lists existed", () => {
    const { container } = render(<ConditionLists favourable={null} unfavourable={null} units="ft" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("opens the breakdown, every variable, the tide, the models and the research", () => {
    const c = conditions();
    render(
      <WhyThisRating score={c.score} explanation={c.explanation} components={{ swell_direction: 0.9 }} favourable={["well-aligned swell"]} unfavourable={[]} units="ft">
        <RatingDetails spot={spot} c={c} point={point()} step={null} fc={forecast("open_meteo_noaa")} tide={null} units="ft" windUnit="km/h" />
      </WhyThisRating>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Why this rating?" }));
    const why = screen.getByTestId("why-rating");
    expect(within(why).getByTestId("rating-fits")).toBeVisible();
    expect(within(why).getByTestId("rating-favourable")).toHaveTextContent("well-aligned swell");
    const vars = within(why).getByTestId("rating-variables");
    expect(vars).toHaveTextContent("Primary swell5.9 ft @ 15 s from SW");
    expect(vars).toHaveTextContent("Secondary swell1.3 ft @ 9 s from S");
    expect(vars).toHaveTextContent("Gusts14 km/h");
    expect(vars).not.toHaveTextContent("Wind waves"); // not sent: left out, never filled in
    expect(within(why).getByTestId("rating-tide")).toHaveTextContent("Preferred tide here: Mid");
    expect(within(why).getByTestId("rating-tide-fit")).toHaveTextContent("Tide is part of this rating: favorable at this hour (fit 0.80).");
    const models = within(why).getByTestId("rating-models");
    expect(models).toHaveTextContent("Wind: gfsrun 18Z Oct 27");
    expect(models).toHaveTextContent("Waves: gfsWaverun 00Z Oct 28");
    expect(models).not.toHaveTextContent("estimated");
    const research = within(why).getByTestId("rating-research");
    // Two confidences, two badges, never one combined class.
    expect(within(research).getByTestId("rating-forecast-confidence")).toHaveTextContent("Forecast confidence: Moderate");
    expect(within(research).getByTestId("rating-research-confidence")).toHaveTextContent("Spot research confidence: Moderate");
    // Only http(s) sources become links.
    expect(within(research).getByRole("link", { name: /Synthetic Guide A/ })).toHaveAttribute("href", "https://example.org/a");
    expect(within(research).queryByRole("link", { name: /Synthetic Guide B/ })).toBeNull();
    expect(research).toHaveTextContent("research methodology synthetic-v1");
  });

  it("says when the tide is not part of the rating", () => {
    render(<TidePreference spot={{ ...spot, tide_preference: null } as SpotDetail} tide={null} components={{}} />);
    expect(screen.getByTestId("rating-tide")).toHaveTextContent("Preferred tide here: Not established");
    expect(screen.getByTestId("rating-tide-fit")).toHaveTextContent("Tide is not part of this rating");
  });

  it("marks Windy run times estimated and credits the Windy database", () => {
    render(<ModelInfo fc={forecast("windy_gfswave")} c={conditions({ source_code: "windy_gfswave" })} />);
    const models = screen.getByTestId("rating-models");
    expect(models).toHaveTextContent("run 18Z Oct 27 (estimated)");
    expect(screen.getByTestId("rating-models-estimated")).toBeVisible();
    expect(screen.getByRole("link", { name: WINDY_CREDIT })).toHaveAttribute("href", "https://www.windy.com/");
  });

  it("takes the version's own run flags when the page has them", () => {
    render(<ModelInfo fc={forecast("open_meteo_noaa")} c={conditions()} version={{ wind_run_estimated: true, wave_run_estimated: false }} />);
    const models = screen.getByTestId("rating-models");
    expect(models).toHaveTextContent("Wind: gfsrun 18Z Oct 27 (estimated)");
    expect(models).toHaveTextContent("Waves: gfsWaverun 00Z Oct 28");
    expect(models).not.toHaveTextContent("00Z Oct 28 (estimated)");
  });
});

describe("staleness (spec 14)", () => {
  it('says "Forecast last updated 7 hours ago." and why, and never shows stale data as current', () => {
    render(<StaleBanner stale runAgeHours={13} lastUpdated="7 hours ago" reason="The latest refresh failed for this spot; the previous forecast is shown." />);
    const b = screen.getByTestId("stale-banner");
    expect(within(b).getByTestId("stale-last-updated")).toHaveTextContent("Forecast last updated 7 hours ago.");
    expect(b).toHaveTextContent("The latest refresh failed for this spot; the previous forecast is shown.");
    expect(b).toHaveTextContent("confidence is capped at Moderate");
  });

  it("keeps the run-age wording without a freshness block, and shows nothing when fresh", () => {
    const { unmount } = render(<StaleBanner stale runAgeHours={20} />);
    expect(screen.getByTestId("stale-banner")).toHaveTextContent("The model run is 20 h old, so confidence is capped at Moderate.");
    expect(screen.queryByTestId("stale-last-updated")).toBeNull();
    unmount();
    const { container } = render(<StaleBanner stale={false} lastUpdated="7 hours ago" />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("provider attribution", () => {
  it("keeps Open-Meteo's credit and CC BY 4.0 link as sent", () => {
    render(
      <AttributionLinks
        links={[
          { label: "Weather data by Open-Meteo.com", href: "https://open-meteo.com/" },
          { label: "CC BY 4.0", href: "https://creativecommons.org/licenses/by/4.0/" },
        ]}
        sourceCode="open_meteo_noaa"
      />,
    );
    expect(screen.getByRole("link", { name: "Weather data by Open-Meteo.com" })).toHaveAttribute("href", "https://open-meteo.com/");
    expect(screen.getByRole("link", { name: "CC BY 4.0" })).toBeVisible();
    expect(screen.queryByText(WINDY_CREDIT)).toBeNull();
  });

  it('credits Windy data with "Contains data from the Windy database" and a link', () => {
    render(<AttributionLinks links={[{ label: "Windy.com", href: "https://www.windy.com/" }]} sourceCode="windy_gfswave" />);
    expect(screen.getByRole("link", { name: WINDY_CREDIT })).toHaveAttribute("href", "https://www.windy.com/");
    expect(screen.queryByText("Windy.com")).toBeNull();
  });
});
