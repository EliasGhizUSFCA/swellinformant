import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConditionsSummary } from "@/components/conditions";
import { ForecastBreakdown } from "@/components/forecast-breakdown";
import { AttributionLinks, ForecastProvenance } from "@/components/forecast-provenance";
import { ageText, StaleBanner, StaleBadge } from "@/components/stale-banner";
import { cycleLabel } from "@/lib/map-time";
import type { Conditions, RunMember, SwellEvent, SystemStatus } from "@/types/api";

// SYNTHETIC test data: invented forecast hours, runs and swells that exist only to exercise
// the freshness labels. Nothing here describes a real forecast, model run or surf spot.

const hooks = vi.hoisted(() => ({
  events: { data: undefined as SwellEvent[] | undefined, isLoading: false, isError: false },
  status: { data: undefined as SystemStatus | undefined },
  map: { data: undefined as { range_end: string | null } | undefined },
}));

vi.mock("@/hooks/queries", () => ({
  useEvents: () => hooks.events,
  useSystemStatus: () => hooks.status,
  useMapConditions: () => hooks.map,
}));

// Imported after the mock is registered (vi.mock is hoisted above imports).
const { forecastFreshness, ForecastHorizon, horizonDays, LiveSwells } = await import("@/components/live-swells");

const NOW = Date.parse("2026-10-12T04:00:00Z");
const hoursFromNow = (h: number) => new Date(NOW + h * 3_600_000).toISOString();

function conditions(overrides: Partial<Conditions> = {}): Conditions {
  return {
    valid_time: hoursFromNow(2),
    score: 74,
    label: "very_good",
    breaking_height_min_ft: 4,
    breaking_height_max_ft: 5,
    breaking_height_mid_ft: 4.4,
    breaking_p10_ft: 3,
    breaking_p90_ft: 6.5,
    uncertainty_kind: "heuristic",
    swell_height_m: 1.6,
    swell_period_s: 14,
    swell_direction_deg: 220,
    wind_speed_kmh: 9,
    wind_direction_deg: 40,
    wind_relation: "offshore",
    confidence: 61,
    confidence_label: "moderate",
    is_daylight: true,
    sig_wave_height_m: 1.8,
    source_code: "synthetic",
    source_name: "Synthetic test source (not real data)",
    is_demo: false,
    run_issued_at: hoursFromNow(-16),
    method: "linear_wave_v2",
    algorithm_version: "2.1.0",
    explanation: "Synthetic explanation (test fixture).",
    forecast_confidence_class: "moderate",
    confidence_reasons: ["Synthetic: a 2-day forecast and the models agree."],
    forecast_confidence_reasons: ["Synthetic: a 2-day forecast and the models agree."],
    lead_hours: 18,
    wind_strength: "light",
    wind_favourability: "favourable",
    ...overrides,
  };
}

function event(overrides: Partial<SwellEvent> = {}): SwellEvent {
  return {
    id: "00000000-0000-4000-8000-0000000000f1",
    spot: {
      id: 1,
      slug: "synthetic-point",
      name: "Synthetic Point (test fixture)",
      country: "Testland",
      country_code: "TL",
      region: "Fixture Coast",
      region_group: "Synthetic",
      latitude: 0,
      longitude: 0,
      timezone: "UTC",
      break_type: "point",
    },
    status: "active",
    start_time: hoursFromNow(100),
    end_time: hoursFromNow(140),
    peak_time: hoursFromNow(120),
    peak_score: 80,
    avg_score: 72,
    peak_label: "very_good",
    peak_breaking_height_min_ft: 5,
    peak_breaking_height_max_ft: 7,
    peak_swell_height_m: 2,
    peak_swell_period_s: 15,
    peak_swell_direction_deg: 220,
    peak_wind_speed_kmh: 9,
    peak_wind_direction_deg: 40,
    peak_wind_relation: "offshore",
    qualifying_hours: 12,
    confidence: 60,
    confidence_label: "moderate",
    lead_days: 5,
    version: 1,
    first_detected_at: hoursFromNow(-30),
    last_updated_at: hoursFromNow(-6),
    is_demo: false,
    source_code: "synthetic",
    history: [],
    forecast_confidence_class: "moderate",
    confidence_reasons: [],
    forecast_confidence_reasons: [],
    stale: false,
    attribution_links: [],
    ...overrides,
  };
}

function status(opts: { demo?: boolean; stale?: boolean; issuedH?: number } = {}): SystemStatus {
  return {
    mode: {
      demo_mode: !!opts.demo,
      forecast_providers: opts.demo ? ["demo"] : ["synthetic"],
      flight_provider: "mock",
      email_provider: "console",
      email_delivery: "capture",
      dev_inbox_url: null,
      captured_email_location: null,
      sms_provider: "console",
      dev_tools: false,
    },
    detection: { lead_min_days: 1, lead_max_days: 10, event_min_score: 57 },
    forecast: {
      stale_after_hours: 12,
      sources: [
        {
          code: "synthetic",
          name: "Synthetic source (test fixture)",
          is_demo: false,
          stale: !!opts.stale,
          latest_successful: { run_key: "synthetic:1", issued_at: hoursFromNow(-(opts.issuedH ?? 6)), status: "success", spot_count: 50 },
          latest_attempt: null,
        },
      ],
    },
    events: {},
    jobs: [],
  };
}

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
  hooks.events = { data: [event()], isLoading: false, isError: false };
  hooks.status = { data: status() };
  hooks.map = { data: undefined };
});

describe("landing page freshness (LiveSwells)", () => {
  it("says how old the model run is and never calls model output live", () => {
    render(<LiveSwells />);
    const label = screen.getByTestId("forecast-freshness");
    expect(label).toHaveAttribute("data-freshness", "fresh");
    expect(label).toHaveTextContent("Forecast · model run 6 h ago");
    expect(document.body.textContent).not.toMatch(/\blive\b/i);
  });

  it("renders a stale run as out of date (and never live)", () => {
    hooks.status = { data: status({ stale: true, issuedH: 40 }) };
    hooks.events = { data: [event({ stale: true })], isLoading: false, isError: false };
    render(<LiveSwells />);
    const label = screen.getByTestId("forecast-freshness");
    expect(label).toHaveAttribute("data-freshness", "stale");
    expect(label).toHaveTextContent("Forecast out of date · model run 40 h ago");
    expect(screen.getByTestId("stale-badge")).toHaveTextContent("Out of date");
    expect(document.body.textContent).not.toMatch(/\blive\b/i);
  });

  it("an event whose run went stale marks the panel out of date even before every source is stale", () => {
    hooks.events = { data: [event({ stale: true })], isLoading: false, isError: false };
    render(<LiveSwells />);
    expect(screen.getByTestId("forecast-freshness")).toHaveAttribute("data-freshness", "stale");
  });

  it("says demo for synthetic forecasts", () => {
    hooks.status = { data: status({ demo: true }) };
    render(<LiveSwells />);
    expect(screen.getByTestId("forecast-freshness")).toHaveTextContent("demo forecast");
    expect(document.body.textContent).not.toMatch(/\blive\b/i);
  });

  it("classifies freshness from the status and events alone", () => {
    expect(forecastFreshness(undefined, undefined, NOW)).toEqual({ kind: "unknown" });
    expect(forecastFreshness(undefined, [event({ is_demo: true })], NOW)).toEqual({ kind: "demo" });
    expect(forecastFreshness(status({ issuedH: 3 }), [], NOW)).toEqual({ kind: "fresh", runText: "model run 3 h ago" });
  });

  it("derives the horizon stat from the stored predictions, not a fixed number", () => {
    expect(horizonDays({ range_end: hoursFromNow(236) }, NOW)).toBe(10);
    expect(horizonDays({ range_end: hoursFromNow(-1) }, NOW)).toBeNull();
    expect(horizonDays(undefined, NOW)).toBeNull();
    hooks.map = { data: { range_end: hoursFromNow(236) } };
    const { unmount } = render(<ForecastHorizon />);
    expect(screen.getByTestId("forecast-horizon")).toHaveTextContent("10-day");
    unmount();
    hooks.map = { data: undefined };
    render(<ForecastHorizon />);
    expect(screen.getByTestId("forecast-horizon")).toHaveTextContent("—");
    expect(screen.getByTestId("forecast-horizon")).not.toHaveTextContent("16-day");
  });
});

describe("ConditionsSummary (model output, never 'Now')", () => {
  it("is titled Forecast with the step's zone-named time and how far away it is", () => {
    render(<ConditionsSummary c={conditions()} tz="Africa/Johannesburg" units="ft" title="Now" nowMs={NOW} />);
    const title = screen.getByTestId("conditions-title");
    expect(title).toHaveTextContent("Forecast");
    expect(title).not.toHaveTextContent(/^Now/);
    // 06:00Z is 8:00 AM in Johannesburg (UTC+2): the zone is named, and the step is 2 h away.
    // The zone's own name, as the spot timeline writes it (one naming rule on the page).
    expect(screen.getByTestId("conditions-time")).toHaveTextContent("Mon 8:00 AM SAST · in 2 h");
    expect(screen.queryByText("Now", { exact: true })).toBeNull();
    expect(screen.getByTestId("likely-range")).toHaveTextContent("Likely range 3–6.5 ft (unverified)");
    expect(screen.getByTestId("conditions-summary")).not.toHaveTextContent("80% range");
  });

  it("keeps a descriptive title and says when the step is in the past", () => {
    render(<ConditionsSummary c={conditions({ valid_time: hoursFromNow(-1) })} tz="UTC" units="m" title="Best surf window, next 7 days" nowMs={NOW} />);
    expect(screen.getByTestId("conditions-title")).toHaveTextContent("Best surf window, next 7 days");
    expect(screen.getByTestId("conditions-time")).toHaveTextContent("1 h ago");
  });

  it("the step breakdown names the forecast time in the spot's zone and how far away it is", () => {
    render(<ForecastBreakdown c={conditions()} tz="Africa/Johannesburg" units="ft" nowMs={NOW} />);
    expect(screen.getByTestId("breakdown-time")).toHaveTextContent("Forecast for Mon, Oct 12, 8:00 AM SAST (local time) · in 2 h");
    // The raw model data names its run's age, never "now" or "live".
    expect(screen.getByTestId("raw-model-runs")).toHaveTextContent("Synthetic test source (not real data) · model run 16 h ago");
    expect(document.body.textContent).not.toMatch(/\blive\b/i);
  });

  it("writes wind and swell directions as where they come FROM (WINDVIZ-12)", () => {
    // 40 deg = NE, 220 deg = SW (synthetic fixture values).
    const { unmount } = render(<ConditionsSummary c={conditions()} tz="UTC" units="ft" nowMs={NOW} />);
    expect(screen.getByTestId("conditions-summary")).toHaveTextContent("9 km/h from NE");
    expect(screen.getByTestId("conditions-summary")).toHaveTextContent("14 s SW swell"); // a swell is named by its origin
    unmount();
    render(<ForecastBreakdown c={conditions()} tz="UTC" units="ft" nowMs={NOW} />);
    expect(screen.getByTestId("prediction-inputs")).toHaveTextContent("9 km/h from NE");
    expect(screen.getByTestId("prediction-inputs")).toHaveTextContent("swell from SW");
    expect(screen.getByTestId("prediction-inputs")).not.toHaveTextContent(/km\/h NE/);
  });

  it("names night steps without calling them unsurfable data gaps", () => {
    render(<ConditionsSummary c={conditions({ is_daylight: false })} tz="UTC" units="ft" nowMs={NOW} />);
    expect(screen.getByTestId("conditions-time")).toHaveTextContent("· night");
  });
});

describe("stale banner and badge", () => {
  it("says alerts are paused only when the alert rule withholds them", () => {
    const { rerender } = render(<StaleBanner stale alertsPaused={false} runAgeHours={20} />);
    const banner = screen.getByTestId("stale-banner");
    expect(banner).toHaveTextContent("Forecast out of date");
    expect(banner).toHaveTextContent("The model run is 20 h old, so confidence is capped at Moderate.");
    expect(banner).not.toHaveTextContent(/paused/i);
    rerender(<StaleBanner stale alertsPaused runAgeHours={34} />);
    expect(screen.getByTestId("stale-banner")).toHaveTextContent("New alerts are paused until fresh data arrives.");
    // A blend's issue time is its oldest member's: the age is never called the newest run's.
    rerender(<StaleBanner stale blend runAgeHours={30} />);
    expect(screen.getByTestId("stale-banner")).toHaveTextContent("Its oldest model run is 30 h old, so confidence is capped at Moderate.");
    expect(screen.getByTestId("stale-banner")).not.toHaveTextContent(/newest/i);
  });

  it("renders nothing for fresh data, and an unknown age without a number", () => {
    const { container, rerender } = render(<StaleBanner stale={false} alertsPaused />);
    expect(container).toBeEmptyDOMElement();
    rerender(<StaleBanner stale />);
    expect(screen.getByTestId("stale-banner")).toHaveTextContent("A newer model run is overdue");
    expect(ageText(0.2)).toBe("1 h");
    expect(ageText(50)).toBe("2 days");
  });

  it("tags stale cards only", () => {
    const { container, rerender } = render(<StaleBadge stale={false} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<StaleBadge stale />);
    expect(screen.getByTestId("stale-badge")).toHaveTextContent("Out of date");
  });
});

describe("forecast provenance", () => {
  const members: RunMember[] = [
    { code: "synthetic_gfs", name: "Synthetic GFS family (test fixture)", family: "ncep_gfs", run_key: "gfs:1", init_time: "2026-10-11T18:00:00Z", wave_model: null, atmosphere_model: null },
    { code: "synthetic_ecmwf", name: "Synthetic ECMWF family (test fixture)", family: "ecmwf_ifs", run_key: "ec:1", init_time: "2026-10-11T12:00:00Z", wave_model: null, atmosphere_model: null },
  ];

  it("lists every blend member with its own init time and when the data was fetched", () => {
    render(
      <ForecastProvenance
        source={{ name: "Synthetic blend (test fixture)", is_demo: false }}
        run={{ issued_at: "2026-10-11T12:00:00Z", completed_at: "2026-10-12T01:00:00Z" }}
        members={members}
      />,
    );
    const items = within(screen.getByTestId("provenance-members")).getAllByTestId("provenance-member");
    expect(items.map((i) => i.textContent?.replace(/^ · /, ""))).toEqual(["Synthetic GFS family (test fixture) 18Z Oct 11", "Synthetic ECMWF family (test fixture) 12Z Oct 11"]);
    expect(screen.getByTestId("provenance-members")).toHaveTextContent(/^Model runs: /);
    expect(screen.getByTestId("forecast-provenance")).toHaveTextContent("Fetched 3 hours ago");
  });

  it("shows the sources' credit and licence links next to the data, as links only for http(s)", () => {
    const links = [
      { label: "Weather data by Open-Meteo.com", href: "https://open-meteo.com/" },
      { label: "Open-Meteo licence (CC BY 4.0)", href: "https://open-meteo.com/en/licence" },
      { label: "Copyright ECMWF: IFS and ecWAM open data (modified)", href: "https://www.ecmwf.int/en/forecasts/datasets/open-data" },
      { label: "Not a link", href: "javascript:alert(1)" },
    ];
    render(<ForecastProvenance source={{ name: "Synthetic blend (test fixture)", is_demo: false }} run={{ issued_at: "2026-10-11T12:00:00Z" }} members={members} links={links} />);
    const box = screen.getByTestId("attribution-links");
    expect(box).toHaveTextContent("Data: Weather data by Open-Meteo.com · Open-Meteo licence (CC BY 4.0) · Copyright ECMWF");
    expect(within(box).getByRole("link", { name: "Open-Meteo licence (CC BY 4.0)" })).toHaveAttribute("href", "https://open-meteo.com/en/licence");
    expect(within(box).getAllByRole("link")).toHaveLength(3);
    expect(box).toHaveTextContent("· Not a link"); // shown as text: javascript: is never a link
    expect(within(box).queryByRole("link", { name: "Not a link" })).toBeNull();
    for (const a of within(box).getAllByRole("link")) expect(a).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("shows no attribution line without links (the demo source)", () => {
    const { container } = render(<AttributionLinks links={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names a single run's cycle in UTC", () => {
    expect(cycleLabel("2026-10-11T18:00:00Z")).toBe("18Z Oct 11");
    expect(cycleLabel("2026-12-31T00:00:00Z")).toBe("00Z Dec 31");
    render(<ForecastProvenance source={{ name: "Synthetic source", is_demo: false }} run={{ issued_at: "2026-10-11T06:00:00Z" }} />);
    expect(screen.getByTestId("provenance-members")).toHaveTextContent("Model run: 06Z Oct 11");
  });
});
