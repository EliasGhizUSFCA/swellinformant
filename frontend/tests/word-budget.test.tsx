import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConditionsSummary } from "@/components/conditions";
import { ForecastConfidence, ResearchConfidence } from "@/components/confidence";
import { EventCard } from "@/components/event-card";
import { ForecastBreakdown } from "@/components/forecast-breakdown";
import { OpportunityCard } from "@/components/opportunity-card";
import { RatingHeadline } from "@/components/rating";
import { forecastReasonsOf } from "@/lib/confidence";
import { ratingReason } from "@/lib/rating";
import type { Conditions, MatchSummary, Research, SwellEvent } from "@/types/api";

/**
 * The first screen stays short (spec 24): quality, wave height, wind, swell and confidence in
 * a few words, with the details one click away and the uncertainty still visible. Counted are
 * the words a sighted reader sees: text inside closed details, screen-reader-only text,
 * aria-hidden icons and tooltips is not. Every fixture is SYNTHETIC but shaped like a real
 * API hour, with reasons as long as the backend's own (forecast_confidence.MAX_REASON_CHARS).
 */
const BUDGET = 45;
// Conditional notes that must never be hidden (the disputed offshore wind, an out-of-date run)
// may add their own words on top: at most this many in all.
const NOTES_ALLOWANCE = 12;
const NOW = Date.parse("2026-10-12T04:00:00Z");
const at = (h: number) => new Date(NOW + h * 3_600_000).toISOString();

function hidden(el: Element | null): boolean {
  for (let e = el; e; e = e.parentElement) {
    if (e.hasAttribute("hidden") || e.getAttribute("aria-hidden") === "true" || e.classList.contains("sr-only")) return true;
  }
  return false;
}

/** Words of visible text (symbols such as "·", "—" and "→" are not words). */
function visibleWords(root: Element): string[] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const words: string[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (hidden(n.parentElement)) continue;
    for (const w of (n.textContent ?? "").split(/\s+/)) if (/[\p{L}\p{N}]/u.test(w)) words.push(w);
  }
  return words;
}

// Reasons as long as the backend writes them (forecast-v1 wording, <= 90 characters).
const FORECAST_REASON = "A 6-day forecast, and the ensemble members spread widely on the swell's size.";
const LOCAL_REASON = "How this break responds to swell is only partly documented.";
/** The combined "local confidence" the API sent before 2.0 removed it (spec 20); a response
 * cached before the upgrade can still carry it, and the UI must not show it. */
const LEGACY_LOCAL = { local_confidence_class: "low", local_confidence_reason: LOCAL_REASON };

function conditions(overrides: Partial<Conditions> = {}): Conditions {
  return {
    valid_time: at(2),
    score: 92,
    label: "excellent",
    breaking_height_min_ft: 6,
    breaking_height_max_ft: 7.5,
    breaking_height_mid_ft: 6.6,
    breaking_p10_ft: 4,
    breaking_p90_ft: 10,
    uncertainty_kind: "heuristic",
    swell_height_m: 2.2,
    swell_period_s: 16,
    swell_direction_deg: 225,
    wind_speed_kmh: 9,
    wind_direction_deg: 40,
    wind_relation: "offshore",
    confidence: 61,
    confidence_label: "moderate",
    is_daylight: true,
    sig_wave_height_m: 2.4,
    source_code: "blend",
    source_name: "Synthetic multi-model blend (test fixture)",
    is_demo: false,
    run_issued_at: at(-16),
    method: "linear_wave_v2",
    algorithm_version: "2.1.0",
    explanation:
      "2.2 m @ 16 s SW swell, well aligned with the break; offshore wind 9 km/h from NE; approx. 6–8 ft faces vs. a working range of 3–12 ft (synthetic).",
    forecast_confidence_class: "moderate",
    ...LEGACY_LOCAL,
    confidence_reasons: [FORECAST_REASON, "Not yet checked against buoys near this spot.", LOCAL_REASON],
    forecast_confidence_reasons: [FORECAST_REASON],
    lead_hours: 18,
    wind_strength: "light",
    wind_favourability: "favourable",
    ...overrides,
  };
}

const SPOT = {
  id: 1,
  slug: "synthetic-point",
  name: "Synthetic Point (test fixture)",
  country: "Testland",
  country_code: "TL",
  region: "Fixture Coast",
  region_group: "Synthetic",
  latitude: 0,
  longitude: 0,
  timezone: "Africa/Johannesburg",
  break_type: "point",
};

function event(overrides: Partial<SwellEvent> = {}): SwellEvent {
  return {
    id: "00000000-0000-4000-8000-0000000000b1",
    spot: SPOT,
    status: "active",
    start_time: at(100),
    end_time: at(150),
    peak_time: at(120),
    peak_score: 92,
    avg_score: 84,
    peak_label: "excellent",
    peak_breaking_height_min_ft: 6,
    peak_breaking_height_max_ft: 8,
    peak_swell_height_m: 2.2,
    peak_swell_period_s: 16,
    peak_swell_direction_deg: 225,
    peak_wind_speed_kmh: 9,
    peak_wind_direction_deg: 40,
    peak_wind_relation: "offshore",
    qualifying_hours: 18,
    confidence: 61,
    confidence_label: "moderate",
    lead_days: 4.2,
    version: 3,
    first_detected_at: at(-40),
    last_updated_at: at(-5),
    is_demo: false,
    source_code: "blend",
    history: [],
    forecast_confidence_class: "moderate",
    ...LEGACY_LOCAL,
    confidence_reasons: [FORECAST_REASON, LOCAL_REASON],
    forecast_confidence_reasons: [FORECAST_REASON],
    wind_dispute_note: null,
    wind_strength: "light",
    wind_favourability: "favourable",
    stale: false,
    attribution_links: [],
    ...overrides,
  };
}

function match(overrides: Partial<MatchSummary> = {}): MatchSummary {
  return {
    id: "00000000-0000-4000-8000-0000000000c1",
    search_id: "00000000-0000-4000-8000-0000000000c2",
    search_name: "Synthetic search",
    search_status: "active",
    status: "flight_found",
    status_reason: null,
    spot: SPOT,
    event_id: "00000000-0000-4000-8000-0000000000b1",
    event_status: "active",
    window_start: at(100),
    window_end: at(150),
    qualifying_hours: 18,
    peak_score: 92,
    avg_score: 84,
    peak_label: "excellent",
    breaking_height_min_ft: 6,
    breaking_height_max_ft: 8,
    confidence: 61,
    confidence_label: "moderate",
    destination_iata: "PLZ",
    transfer_minutes: 75,
    origin_iata: "SFO",
    recommended_arrival_date: "2026-10-14",
    recommended_departure_date: "2026-10-20",
    best_price: "1234",
    currency: "USD",
    overall_score: 80,
    scores: { surf: 92, affordability: 70, confidence: 61, convenience: 65 },
    is_demo: true,
    has_mock_flights: true,
    flight_checked_at: at(-2),
    notification_count: 1,
    lead_days: 4.2,
    created_at: at(-30),
    updated_at: at(-2),
    forecast_confidence_class: "moderate",
    ...LEGACY_LOCAL,
    stale: false,
    short_notice: true,
    best_offer_expired: false,
    quoted_currency: null,
    ...overrides,
  };
}

const NOTES = { wind_dispute_note: "Sources disagree about which wind is offshore here.", stale: true } as const;

function research(gaps: string[]): Research {
  return {
    research_class: "low",
    summary: "Synthetic summary: two sources describe the break; tide and the best wind are uncertain.",
    gaps,
    variables: [
      { variable: "tide", label: "Tide", research_class: "low", summary: "Synthetic: one source.", gaps: gaps.slice(0, 1), source_count: 1 },
      { variable: "wind", label: "Wind", research_class: "moderate", summary: "Synthetic: two sources.", gaps: [], source_count: 2 },
    ],
    sources: [{ title: "Synthetic Guide (test fixture)", url: "https://example.org/guide", publisher: "Example Press", source_type: "guidebook", published_year: 2020, accessed_at: "2026-09-01T00:00:00Z" }],
    methodology_version: "synthetic-v1",
    assessed_at: "2026-10-01T00:00:00Z",
    research_updated_at: "2026-09-30T00:00:00Z",
  };
}

const FOUR_GAPS = [
  "Synthetic gap: no source describes the preferred tide at this break.",
  "Synthetic gap: sources disagree on which wind is offshore here.",
  "Synthetic gap: no source gives the size range the break holds.",
  "Synthetic gap: the bathymetry near the break is not surveyed.",
];

/** The spot page's summary block: the one-sentence headline, then forecast confidence with
 * its first reason, as the page lays them out (spec 20: no combined "confidence at the break";
 * research confidence is its own block, measured below). */
function SpotSummary({ c }: { c: Conditions }) {
  return (
    <div data-testid="spot-summary">
      <RatingHeadline label={c.label} score={c.score} reason={ratingReason({ periodS: c.swell_period_s, windRelation: c.wind_relation, windStrength: c.wind_strength, explanation: c.explanation })} size="lg" inline />
      <ForecastConfidence value={c.forecast_confidence_class} reasons={forecastReasonsOf(c)} notes={c.confidence_reasons} validTime={c.valid_time} runIssuedAt={c.run_issued_at} nowMs={NOW} legacyIndex={c.confidence} />
    </div>
  );
}

function count(testId: string): string[] {
  return visibleWords(screen.getByTestId(testId));
}

describe(`first-screen word budget (≤ ${BUDGET} visible words)`, () => {
  it("the spot page summary: rating, reason and forecast confidence (never a combined class)", () => {
    render(<SpotSummary c={conditions()} />);
    const words = count("spot-summary");
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET);
    // Short but complete: the uncertainty stays on the first screen.
    const root = screen.getByTestId("spot-summary");
    expect(root).toHaveTextContent("Excellent Surf — 9.2/10");
    expect(root).toHaveTextContent(`Forecast confidence: Moderate${FORECAST_REASON}`);
    // Spec 20: the combined class that mixed research and forecast confidence is gone.
    expect(root).not.toHaveTextContent("Confidence at the break");
    expect(root).not.toHaveTextContent(LOCAL_REASON);
  });

  it("the spot page research block: class, first gap and the gap count", () => {
    render(<ResearchConfidence research={research(FOUR_GAPS)} />);
    const words = count("research-confidence");
    // The one-line summary is the research's own; the rest of the block stays within 25 words.
    const summary = visibleWords(screen.getByTestId("research-summary")).length;
    expect(words.length - summary, words.join(" ")).toBeLessThanOrEqual(25);
    render(<ResearchConfidence research={research(FOUR_GAPS)} compact data-testid="research-compact" />);
    expect(count("research-compact").length).toBeLessThanOrEqual(25);
  });

  it("the forecast-step card (the spot page's forecast card and the map panel)", () => {
    const { unmount } = render(<ConditionsSummary c={conditions()} tz="Africa/Johannesburg" units="ft" title="Now" nowMs={NOW} />);
    let words = count("conditions-summary");
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET);
    const root = screen.getByTestId("conditions-summary");
    expect(root).toHaveTextContent("Likely range 4–10 ft (unverified)");
    expect(root).toHaveTextContent("Forecast confidence: Moderate");
    unmount();
    // With the disputed-wind note on top.
    const again = render(<ConditionsSummary c={conditions({ wind_dispute_note: NOTES.wind_dispute_note })} tz="Africa/Johannesburg" units="ft" title="Now" nowMs={NOW} />);
    words = count("conditions-summary");
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET + NOTES_ALLOWANCE);
    again.unmount();
    // The map panel names the card "Best surf window, next 7 days": five words more than "Forecast".
    render(<ConditionsSummary c={conditions()} tz="Africa/Johannesburg" units="ft" title="Best surf window, next 7 days" nowMs={NOW} />);
    words = count("conditions-summary");
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET + 5);
  });

  it("the selected-step breakdown's prediction block (spot page and map panel)", () => {
    render(<ForecastBreakdown c={conditions()} tz="Africa/Johannesburg" units="ft" nowMs={NOW} />);
    const words = count("our-prediction");
    // It also names the swell and wind behind the rating: five words more than a card.
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET + 5);
    expect(screen.getByTestId("our-prediction")).toHaveTextContent("Forecast confidence: Moderate");
    // Spec 20: no combined "at the break" class next to it.
    expect(screen.queryByTestId("local-confidence-badge")).toBeNull();
    // The raw model data is one header line until opened.
    expect(screen.getByTestId("raw-model").querySelector("dl")).not.toBeVisible();
  });

  it("dashboard swell cards", () => {
    const { unmount } = render(<EventCard event={event()} featured />);
    let words = count("event-card");
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET);
    expect(screen.queryByTestId("local-confidence-badge")).toBeNull();
    unmount();
    render(<EventCard event={event(NOTES)} featured />);
    words = count("event-card");
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET + NOTES_ALLOWANCE);
    expect(screen.getByTestId("stale-badge")).toBeVisible();
    expect(screen.getByTestId("wind-dispute-note")).toBeVisible();
  });

  it("opportunity cards", () => {
    const { unmount } = render(<OpportunityCard match={match()} />);
    let words = count("opportunity-card");
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET);
    expect(within(screen.getByTestId("opportunity-card")).getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: Moderate");
    unmount();
    render(<OpportunityCard match={match({ stale: true })} />);
    words = count("opportunity-card");
    expect(words.length, words.join(" ")).toBeLessThanOrEqual(BUDGET + NOTES_ALLOWANCE);
  });
});

describe("research confidence on the first screen", () => {
  it("shows the class, the first gap and '+3 more'; every gap is inside the details", () => {
    render(<ResearchConfidence research={research(FOUR_GAPS)} compact />);
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: Low");
    expect(screen.getByTestId("research-first-gap")).toHaveTextContent(FOUR_GAPS[0]!);
    expect(screen.getByTestId("research-more-gaps")).toHaveTextContent("+3 more");
    const items = within(screen.getByTestId("research-gaps")).getAllByRole("listitem", { hidden: true });
    expect(items.map((li) => li.textContent)).toEqual(FOUR_GAPS);
    for (const li of items) expect(li).not.toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Research details" }));
    for (const li of items) expect(li).toBeVisible();
  });
});
