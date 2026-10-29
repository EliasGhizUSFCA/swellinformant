import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EventCard } from "@/components/event-card";
import { OpportunityCard } from "@/components/opportunity-card";
import { StaleLine } from "@/components/stale-banner";
import { freshnessTitle } from "@/lib/forecast-fields";
import type { MatchDetail, MatchSummary, SwellEvent } from "@/types/api";

// SYNTHETIC test data: an invented trip, spot and swell that exist only to exercise the
// freshness labels of the trip page and the cards. Nothing here describes a real forecast.

const hooks = vi.hoisted(() => ({ match: undefined as unknown }));

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "00000000-0000-4000-8000-0000000000c1" }),
  usePathname: () => "/opportunities/00000000-0000-4000-8000-0000000000c1",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/layout/app-shell", () => ({ AppShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/components/require-auth", () => ({
  RequireAuth: ({ children }: { children: (user: unknown) => ReactNode }) => <>{children({ profile: { units: "ft" } })}</>,
}));
vi.mock("@/hooks/queries", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/queries")>()),
  useOpportunity: () => ({ data: hooks.match, isLoading: false, error: null }),
}));

// Imported after the mocks are registered (vi.mock is hoisted above imports).
const { default: OpportunityPage } = await import("@/app/opportunities/[id]/page");

const NOW = Date.now();
const hoursFromNow = (h: number) => new Date(NOW + h * 3_600_000).toISOString();

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
  timezone: "UTC",
  break_type: "point",
};

function event(overrides: Partial<SwellEvent> = {}): SwellEvent {
  return {
    id: "00000000-0000-4000-8000-0000000000e1",
    spot: SPOT,
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
    // The swell detection touches every served spot's event, a stale one's too.
    last_updated_at: hoursFromNow(-0.05),
    is_demo: false,
    source_code: "synthetic",
    history: [],
    forecast_confidence_class: "moderate",
    confidence_reasons: [],
    forecast_confidence_reasons: [],
    stale: false,
    attribution_links: [],
    ...overrides,
  } as SwellEvent;
}

const STALE = { version_id: 3, last_updated_at: hoursFromNow(-9), stale: true, reason: "refresh_failed" } as const;
const CURRENT = { version_id: 3, last_updated_at: hoursFromNow(-2), stale: false, reason: null } as const;

function trip(freshness: typeof STALE | typeof CURRENT): MatchDetail {
  return {
    id: "00000000-0000-4000-8000-0000000000c1",
    spot: SPOT,
    search_name: "Synthetic search",
    status: "surf_only",
    status_reason: "No itinerary fits (synthetic).",
    window_start: hoursFromNow(100),
    window_end: hoursFromNow(140),
    peak_label: "very_good",
    peak_score: 80,
    overall_score: 70,
    breaking_height_min_ft: 5,
    breaking_height_max_ft: 7,
    qualifying_hours: 12,
    forecast_confidence_class: "moderate",
    is_demo: false,
    has_mock_flights: false,
    stale: false,
    freshness,
    event: event({ freshness }),
    scores: {},
    daily: [],
    offers: [],
    inspection_links: [],
    spot_airports: [],
    limitations: [],
    accessibility_notes: "Synthetic access note.",
    recommended_arrival_date: "2030-01-01",
    recommended_departure_date: "2030-01-05",
    best_price: null,
    currency: "USD",
    quoted_currency: null,
    best_offer_expired: false,
    origin_iata: "AAA",
    destination_iata: "BBB",
    travel_window: null,
    flight_search_error: null,
  } as unknown as MatchDetail;
}

function renderPage(m: MatchDetail) {
  hooks.match = m;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <OpportunityPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => cleanup());

describe("trip page freshness (review r3-frontend-N1, spec 14)", () => {
  it("shows a stale spot's trip as out of date, with its last update and why, never as just updated", () => {
    renderPage(trip(STALE));
    const banner = screen.getByTestId("stale-banner");
    expect(banner).toHaveTextContent("Forecast out of date");
    expect(within(banner).getByTestId("stale-last-updated")).toHaveTextContent("Forecast last updated 9 hours ago.");
    expect(banner).toHaveTextContent("The latest refresh failed for this spot; the previous forecast is shown.");
    expect(banner).toHaveTextContent("Not a current forecast.");
    expect(banner).not.toHaveTextContent("capped at Moderate"); // the run itself is not stale
    expect(screen.getByTestId("trip-stale-line")).toHaveTextContent("Forecast last updated 9 hours ago. Not a current forecast.");
    expect(screen.queryByTestId("trip-updated")).toBeNull();
    expect(screen.getByTestId("swell-overview")).not.toHaveTextContent(/Forecast updated .* ago/);
  });

  it("says when the spot's forecast was updated (the version's time, not the detection's touch) when current", () => {
    renderPage(trip(CURRENT));
    expect(screen.queryByTestId("stale-banner")).toBeNull();
    expect(screen.getByTestId("trip-updated")).toHaveTextContent("Forecast updated 2 hours ago.");
  });
});

describe("card stale wording follows the reason (review r3-frontend-N5)", () => {
  const nowMs = NOW;
  it("an age reason never claims the latest refresh skipped the spot", () => {
    const wind = { versionId: 3, lastUpdatedAt: hoursFromNow(-1), stale: true, reason: "atmosphere_age" };
    render(<StaleLine freshness={wind} nowMs={nowMs} />);
    expect(screen.getByTestId("stale-line")).toHaveTextContent("Forecast last updated 1 hour ago. Not a current forecast. The wind run is overdue.");
    expect(freshnessTitle(wind)).toBe("The wind forecast is older than its model's update interval. It is not a current forecast.");
    expect(freshnessTitle(wind)).not.toMatch(/latest refresh/);
    expect(freshnessTitle({ ...wind, reason: null })).toBe("This spot's forecast is not current.");
  });

  it("a failed refresh says so on the line and in the tag's title", () => {
    const failed = { version_id: 3, last_updated_at: hoursFromNow(-9), stale: true, reason: "refresh_failed" } as const;
    render(<EventCard event={event({ freshness: failed })} />);
    expect(screen.getByTestId("event-card-stale")).toHaveTextContent("Forecast last updated 9 hours ago. Not a current forecast. Its latest refresh failed.");
    expect(screen.getByTestId("stale-badge")).toHaveAttribute("title", "The latest refresh failed for this spot; the previous forecast is shown. It is not a current forecast.");
    cleanup();
    const waves = { ...failed, last_updated_at: hoursFromNow(-1), reason: "wave_age" } as const;
    render(<OpportunityCard match={{ ...trip(CURRENT), freshness: waves } as unknown as MatchSummary} />);
    expect(screen.getByTestId("opportunity-card-stale")).toHaveTextContent("The wave run is overdue.");
    expect(screen.getByTestId("stale-badge")).toHaveAttribute("title", "The wave forecast is older than its model's update interval. It is not a current forecast.");
  });
});
