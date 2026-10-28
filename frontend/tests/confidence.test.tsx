import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import * as confidenceComponents from "@/components/confidence";
import { ConfidenceBadge, ForecastConfidence, ResearchConfidence } from "@/components/confidence";
import { EventCard } from "@/components/event-card";
import {
  asConfidenceClass,
  confidenceLabel,
  FORECAST_CONFIDENCE_MEANING,
  forecastOnlyReasons,
  forecastReasonsOf,
  legacyIndexText,
  modelRunText,
  NOT_VERIFIED_TEXT,
  RESEARCH_REASON_TEXTS,
  safeExternalUrl,
  validInText,
  WIND_DISPUTE_TEXT,
} from "@/lib/confidence";
import type { Research, SwellEvent } from "@/types/api";

// SYNTHETIC test data: invented reasons, sources and URLs that exist only to exercise the
// components. Nothing here describes a real forecast or a real surf spot.

describe("confidence classes", () => {
  it("maps known classes and treats anything else as not assessed (never High)", () => {
    expect(asConfidenceClass("high")).toBe("high");
    expect(asConfidenceClass(" Moderate ")).toBe("moderate");
    expect(asConfidenceClass("LOW")).toBe("low");
    for (const v of [null, undefined, "", "certain", "very_high", "1"]) {
      expect(asConfidenceClass(v)).toBeNull();
      expect(confidenceLabel(v)).toBe("Not assessed yet");
    }
  });

  it("formats the legacy index only as an index, never a percentage", () => {
    // The value of the "Offshore spread index" row: no class word, no %.
    expect(legacyIndexText(72)).toBe("72 / 100 (not a probability)");
    expect(legacyIndexText(71.6)).toBe("72 / 100 (not a probability)");
    expect(legacyIndexText(null)).toBeNull();
    expect(legacyIndexText(72)).not.toContain("%");
  });

  // validInText / modelRunText replace leadTimeText, whose "about 18 hours ahead" counted from
  // the run's issue time (a blend's oldest member) and contradicted the hour shown.
  it("says how far away a valid time is, from now", () => {
    const now = Date.parse("2026-10-12T04:00:00Z");
    expect(validInText("2026-10-12T06:00:00Z", now)).toBe("in 2 h");
    expect(validInText("2026-10-12T03:00:00Z", now)).toBe("1 h ago");
    expect(validInText("2026-10-12T04:40:00Z", now)).toBe("in 40 min");
    expect(validInText("2026-10-12T04:00:20Z", now)).toBe("within a minute");
    expect(validInText("2026-10-15T04:00:00Z", now)).toBe("in 3 days");
    expect(validInText("2026-10-13T16:00:00Z", now)).toBe("in 36 h");
    expect(validInText(null, now)).toBeNull();
    expect(validInText("not a time", now)).toBeNull();
  });

  it("says how old the model run is", () => {
    const now = Date.parse("2026-10-12T04:00:00Z");
    expect(modelRunText("2026-10-11T12:00:00Z", now)).toBe("model run 16 h ago");
    expect(modelRunText("2026-10-09T12:00:00Z", now)).toBe("model run 3 days ago");
    expect(modelRunText("2026-10-12T04:05:00Z", now)).toBe("model run under a minute ago");
    expect(modelRunText(undefined, now)).toBeNull();
  });

  it("keeps only forecast reasons out of a mixed list", () => {
    const mixed = ["Synthetic: a 6-day forecast.", NOT_VERIFIED_TEXT, WIND_DISPUTE_TEXT, ...RESEARCH_REASON_TEXTS, "Synthetic: a 6-day forecast."];
    expect(forecastOnlyReasons(mixed)).toEqual(["Synthetic: a 6-day forecast."]);
    expect(forecastReasonsOf({ forecast_confidence_reasons: [], confidence_reasons: mixed })).toEqual(["Synthetic: a 6-day forecast."]);
    expect(forecastReasonsOf({ forecast_confidence_reasons: ["Synthetic: own."], confidence_reasons: mixed })).toEqual(["Synthetic: own."]);
  });

  it("only turns http(s) URLs into links", () => {
    expect(safeExternalUrl("https://example.org/a")).toBe("https://example.org/a");
    expect(safeExternalUrl("http://example.org/")).toBe("http://example.org/");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("data:text/html,hi")).toBeNull();
    expect(safeExternalUrl("/relative")).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
  });
});

describe("ConfidenceBadge", () => {
  it.each([
    ["high", "High"],
    ["moderate", "Moderate"],
    ["low", "Low"],
    [null, "Not assessed yet"],
    [undefined, "Not assessed yet"],
  ])("renders %s as text, not colour alone", (value, text) => {
    render(<ConfidenceBadge value={value} />);
    const badge = screen.getByTestId("forecast-confidence-badge");
    expect(badge).toHaveTextContent(`Forecast confidence: ${text}`);
    expect(badge).toHaveAttribute("data-confidence", value ?? "none");
  });

  it("labels research confidence separately", () => {
    render(<ConfidenceBadge value="low" kind="research" />);
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: Low");
  });

  it("compact mode keeps the prefix for screen readers and as a tooltip", () => {
    render(<ConfidenceBadge value="moderate" compact />);
    const badge = screen.getByTestId("forecast-confidence-badge");
    expect(badge).toHaveTextContent("Forecast confidence: Moderate");
    expect(badge).toHaveAttribute("title", "Forecast confidence: Moderate");
    expect(screen.getByText("Forecast confidence:", { exact: false })).toHaveClass("sr-only");
  });

});

describe("ForecastConfidence", () => {
  const reasons = ["Synthetic reason one: the swell is about seven days away, so conditions may change.", "Synthetic reason two: the models agree."];

  it("shows the class and first reason, with every reason and the meaning behind a toggle", () => {
    render(
      <ForecastConfidence
        value="moderate"
        reasons={reasons}
        validTime="2026-10-12T06:00:00Z"
        runIssuedAt="2026-10-11T12:00:00Z"
        nowMs={Date.parse("2026-10-12T04:00:00Z")}
        legacyIndex={61}
      />,
    );
    expect(screen.getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: Moderate");
    expect(screen.getByTestId("forecast-confidence-reason")).toHaveTextContent(reasons[0]!);
    const toggle = screen.getByRole("button", { name: "Confidence details" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const panel = document.getElementById(toggle.getAttribute("aria-controls")!)!;
    expect(panel).not.toBeVisible();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(panel).toBeVisible();
    const items = within(screen.getByTestId("forecast-confidence-reasons")).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(reasons);
    expect(panel).toHaveTextContent(FORECAST_CONFIDENCE_MEANING);
    expect(panel).toHaveTextContent("It is not a probability.");
    // How far away the hour is and how old the run is, never "about N ahead" from the
    // run's issue time; the 0-100 index is named for what it is, an offshore spread index.
    expect(screen.getByTestId("forecast-confidence-timing")).toHaveTextContent("Valid in 2 h · model run 16 h ago.");
    expect(panel).not.toHaveTextContent(/about \d+ (hours?|days) ahead/);
    expect(screen.getByTestId("forecast-confidence-index")).toHaveTextContent("Offshore spread index 61 / 100 (not a probability).");
    expect(panel).not.toHaveTextContent(/confidence index/i);
    expect(document.body.textContent).not.toMatch(/\d\s*%/);
  });

  it("names a blend's run age as its oldest member's", () => {
    render(<ForecastConfidence value="high" reasons={reasons} runIssuedAt="2026-10-11T12:00:00Z" blend nowMs={Date.parse("2026-10-12T04:00:00Z")} />);
    expect(screen.getByTestId("forecast-confidence-timing")).toHaveTextContent("Oldest model run 16 h ago.");
  });

  it("never lists research, wind-dispute or the constant not-verified note as forecast reasons", () => {
    const mixed = [reasons[0]!, NOT_VERIFIED_TEXT, WIND_DISPUTE_TEXT, RESEARCH_REASON_TEXTS[1]];
    render(<ForecastConfidence value="high" reasons={mixed} />);
    fireEvent.click(screen.getByRole("button", { name: "Confidence details" }));
    const items = within(screen.getByTestId("forecast-confidence-reasons")).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([reasons[0]]);
    const fc = screen.getByTestId("forecast-confidence");
    expect(fc).not.toHaveTextContent("How this break responds to swell");
    expect(fc).not.toHaveTextContent(WIND_DISPUTE_TEXT);
    // The not-verified note is said once, below the reasons, not as a reason.
    expect(screen.getByTestId("forecast-confidence-unverified")).toHaveTextContent(NOT_VERIFIED_TEXT);
  });

  it("takes the not-verified note from the mixed list passed as notes", () => {
    render(<ForecastConfidence value="high" reasons={[reasons[0]!]} notes={[reasons[0]!, NOT_VERIFIED_TEXT, RESEARCH_REASON_TEXTS[0]]} />);
    expect(screen.getByTestId("forecast-confidence-unverified")).toHaveTextContent(NOT_VERIFIED_TEXT);
    expect(screen.getByTestId("forecast-confidence")).not.toHaveTextContent("How this break responds to swell");
  });

  it.each(["high", "low"])("renders the %s class from the API value", (value) => {
    render(<ForecastConfidence value={value} reasons={[]} />);
    expect(screen.getByTestId("forecast-confidence")).toHaveAttribute("data-confidence", value);
  });

  it("renders a null class as Not assessed yet without a default", () => {
    render(<ForecastConfidence value={null} reasons={[]} />);
    expect(screen.getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: Not assessed yet");
    expect(screen.queryByTestId("forecast-confidence-reason")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confidence details" }));
    expect(screen.getByText("Forecast confidence has not been assessed for this forecast yet.")).toBeVisible();
    expect(document.body.textContent).not.toContain("High");
  });

  it("keeps long reasons whole", () => {
    const long = `Synthetic long reason: ${"the ensemble members spread widely and the latest run moved the peak by a day; ".repeat(6)}`.trim();
    render(<ForecastConfidence value="low" reasons={[long, "Second synthetic reason."]} />);
    expect(screen.getByTestId("forecast-confidence-reason").textContent).toBe(long);
    fireEvent.click(screen.getByRole("button", { name: "Confidence details" }));
    expect(within(screen.getByTestId("forecast-confidence-reasons")).getAllByRole("listitem")).toHaveLength(2);
  });
});

describe("no combined confidence (spec 20)", () => {
  it("the module offers forecast and research confidence only", () => {
    expect(Object.keys(confidenceComponents).sort()).toEqual(["ConfidenceBadge", "ForecastConfidence", "ResearchConfidence"]);
  });
});

function research(overrides: Partial<Research> = {}): Research {
  return {
    research_class: "high",
    summary: "Synthetic summary: preferred swell direction, tide and wind are supported by several sources.",
    gaps: [],
    variables: [
      { variable: "swell_direction", label: "Swell direction", research_class: "high", summary: "Synthetic: three sources agree.", gaps: [], source_count: 3 },
      { variable: "tide", label: "Tide", research_class: "low", summary: "Synthetic: one source only.", gaps: ["Synthetic gap: only one source mentions tide."], source_count: 1 },
    ],
    sources: [
      { title: "Synthetic Guide (test fixture)", url: "https://example.org/synthetic-guide", publisher: "Example Press", source_type: "guidebook", published_year: 2020, accessed_at: "2026-09-01T00:00:00Z" },
      { title: "Unsafe synthetic link", url: "javascript:alert(1)", publisher: "Nobody", source_type: "community", published_year: null, accessed_at: null },
      { title: "Offline synthetic report", url: null, publisher: "Example Agency", source_type: "official", published_year: 2018, accessed_at: null },
    ],
    methodology_version: "research-v1-test",
    assessed_at: "2026-10-01T12:00:00Z",
    research_updated_at: "2026-09-30T12:00:00Z",
    ...overrides,
  };
}

describe("ResearchConfidence", () => {
  it("shows class, summary, details, safe source links, method and update date", () => {
    render(<ResearchConfidence research={research()} />);
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: High");
    expect(screen.getByTestId("research-summary")).toHaveTextContent("Synthetic summary");
    expect(screen.getByTestId("research-updated")).toHaveTextContent("Research updated Sep 30, 2026");
    const toggle = screen.getByRole("button", { name: "Research details" });
    fireEvent.click(toggle);
    const vars = within(screen.getByTestId("research-variables")).getAllByRole("listitem");
    expect(vars).toHaveLength(2);
    expect(vars[1]).toHaveTextContent("Gap: Synthetic gap: only one source mentions tide.");
    expect(vars[1]).toHaveTextContent("1 source");
    const links = within(screen.getByTestId("research-sources")).getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "https://example.org/synthetic-guide");
    expect(links[0]).toHaveAttribute("target", "_blank");
    expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText("Unsafe synthetic link").closest("a")).toBeNull();
    expect(screen.getByText("Offline synthetic report").closest("a")).toBeNull();
    expect(screen.getByTestId("research-method")).toHaveTextContent("Methodology research-v1-test");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the API's first gap with the count of the rest, and every gap in the details", () => {
    const gaps = ["Synthetic gap: no source describes the preferred tide.", "Synthetic gap: sources disagree on the best swell direction."];
    render(<ResearchConfidence research={research({ research_class: "low", gaps })} />);
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: Low");
    expect(screen.getByTestId("research-first-gap")).toHaveTextContent(gaps[0]!);
    expect(screen.getByTestId("research-first-gap")).toBeVisible();
    expect(screen.getByTestId("research-more-gaps")).toHaveTextContent("+1 more");
    const items = within(screen.getByTestId("research-gaps")).getAllByRole("listitem", { hidden: true });
    expect(items.map((i) => i.textContent)).toEqual(gaps);
    expect(items[1]).not.toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Research details" }));
    expect(items[1]).toBeVisible();
  });

  it("shows no count when there is a single gap, and nothing when there are none", () => {
    render(<ResearchConfidence research={research({ gaps: ["Synthetic gap: one only."] })} />);
    expect(screen.getByTestId("research-first-gap")).toHaveTextContent("Synthetic gap: one only.");
    expect(screen.queryByTestId("research-more-gaps")).toBeNull();
  });

  it("compact mode moves the summary into the details", () => {
    render(<ResearchConfidence research={research()} compact />);
    expect(screen.queryByTestId("research-summary")).toBeNull();
    expect(screen.getByText(/Synthetic summary/)).not.toBeVisible();
  });

  it("labels the assessment date as such when the research date is unknown", () => {
    render(<ResearchConfidence research={research({ research_updated_at: null })} />);
    expect(screen.getByTestId("research-updated")).toHaveTextContent("Assessed Oct 1, 2026");
    expect(screen.getByTestId("research-updated")).not.toHaveTextContent("Research updated");
  });

  it("renders a missing assessment as Not assessed yet", () => {
    render(<ResearchConfidence research={null} />);
    expect(screen.getByTestId("research-confidence")).toHaveAttribute("data-confidence", "none");
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: Not assessed yet");
  });

  it("keeps research and forecast confidence as separate labels", () => {
    render(
      <>
        <ForecastConfidence value="low" reasons={[]} />
        <ResearchConfidence research={research({ research_class: "high" })} />
      </>,
    );
    expect(screen.getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: Low");
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: High");
  });
});

function event(overrides: Partial<SwellEvent> = {}): SwellEvent {
  return {
    id: "00000000-0000-4000-8000-0000000000e9",
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
    start_time: "2026-10-20T06:00:00Z",
    end_time: "2026-10-22T18:00:00Z",
    peak_time: "2026-10-21T08:00:00Z",
    peak_score: 88,
    avg_score: 80,
    peak_label: "excellent",
    peak_breaking_height_min_ft: 6,
    peak_breaking_height_max_ft: 8,
    peak_swell_height_m: 2,
    peak_swell_period_s: 15,
    peak_swell_direction_deg: 220,
    peak_wind_speed_kmh: 9,
    peak_wind_direction_deg: 40,
    peak_wind_relation: "offshore",
    qualifying_hours: 12,
    confidence: 64,
    confidence_label: "moderate",
    lead_days: 6,
    version: 1,
    first_detected_at: "2026-10-12T00:00:00Z",
    last_updated_at: "2026-10-12T00:00:00Z",
    is_demo: false,
    source_code: "synthetic",
    history: [],
    forecast_confidence_class: "high",
    local_confidence_class: "high",
    confidence_reasons: [],
    forecast_confidence_reasons: [],
    stale: false,
    attribution_links: [],
    ...overrides,
  };
}

describe("EventCard confidence", () => {
  it("shows forecast confidence only, even when the deprecated combined class is lower (spec 20)", () => {
    const { unmount } = render(<EventCard event={event({ local_confidence_class: "low", local_confidence_reason: RESEARCH_REASON_TEXTS[0] })} />);
    expect(screen.getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: High");
    expect(screen.queryByTestId("local-confidence-badge")).toBeNull();
    expect(screen.queryByTestId("local-confidence")).toBeNull();
    expect(screen.getByTestId("event-card")).not.toHaveTextContent(RESEARCH_REASON_TEXTS[0]);
    unmount();
    render(<EventCard event={event()} />);
    expect(screen.getAllByTestId(/confidence-badge$/)).toHaveLength(1);
    expect(screen.queryByTestId("local-confidence")).toBeNull();
  });

  it("tags a stale forecast run, and nothing when fresh", () => {
    const { unmount } = render(<EventCard event={event({ stale: true })} />);
    expect(screen.getByTestId("stale-badge")).toHaveTextContent("Out of date");
    unmount();
    render(<EventCard event={event()} />);
    expect(screen.queryByTestId("stale-badge")).toBeNull();
  });
});
