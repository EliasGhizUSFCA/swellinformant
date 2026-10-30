import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SpotSummary } from "@/types/api";

// SYNTHETIC test data: invented spots that exist only to exercise the credit line of the
// /spots index. Nothing here describes a real forecast.

const hooks = vi.hoisted(() => ({ spots: [] as unknown[] }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/spots",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/layout/app-shell", () => ({ AppShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/hooks/queries", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/queries")>()),
  useSpots: () => ({ data: hooks.spots, isLoading: false, error: null }),
  useMe: () => ({ data: undefined, isLoading: false, error: null }),
}));

// Imported after the mocks are registered (vi.mock is hoisted above imports).
const { default: SpotsPage } = await import("@/app/spots/page");

// The credit the Open-Meteo GFS source asks for (backend forecasts/queries.py), as the API
// sends it on each spot served from such a run.
const OPEN_METEO = [
  { label: "Weather data by Open-Meteo.com", href: "https://open-meteo.com/" },
  { label: "CC BY 4.0", href: "https://creativecommons.org/licenses/by/4.0/" },
];

function spot(id: number, links: typeof OPEN_METEO): SpotSummary {
  return {
    id,
    slug: `synthetic-${id}`,
    name: `Synthetic Point ${id} (test fixture)`,
    country: "Testland",
    country_code: "TL",
    region: "Fixture Coast",
    region_group: "Synthetic",
    latitude: 0,
    longitude: 0,
    timezone: "UTC",
    break_type: "point",
    wave_direction: "right",
    is_big_wave: false,
    skill_level: "intermediate",
    wave_height_min_ft: 3,
    wave_height_max_ft: 8,
    best_months: [],
    primary_airport: null,
    current: null,
    best_upcoming: null,
    next_event: null,
    upcoming_event_count: 0,
    research_class: null,
    freshness: null,
    attribution_links: links,
  } as SpotSummary;
}

function renderPage(spots: SpotSummary[]) {
  hooks.spots = spots;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SpotsPage />
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe("the /spots index credits the sources of its ratings (review r4-spec-R4-SPEC-1)", () => {
  it("shows the Open-Meteo credit once under the spot list", () => {
    renderPage([spot(1, OPEN_METEO), spot(2, OPEN_METEO), spot(3, [])]);
    expect(screen.getAllByTestId("spot-card")).toHaveLength(3);
    const [credit] = screen.getAllByTestId("attribution-links");
    expect(screen.getAllByTestId("attribution-links")).toHaveLength(1);
    expect(credit).toHaveTextContent("Weather data by Open-Meteo.com");
    expect(within(credit!).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["https://open-meteo.com/", "https://creativecommons.org/licenses/by/4.0/"]);
  });

  it("shows no credit when no spot's source asks for one (the demo source)", () => {
    renderPage([spot(1, []), spot(2, [])]);
    expect(screen.queryByTestId("attribution-links")).toBeNull();
  });
});
