import { expect, test, type Page } from "@playwright/test";

import { apiGet, apiPost, verifyEmailFromOutbox } from "./helpers";

/**
 * Interactive map against the production build. Tile servers are not reachable from CI
 * sandboxes, so by default tile requests are answered with a local PNG. Set
 * E2E_REAL_TILES=1 to load the real OpenStreetMap tiles instead (requires network).
 */
const REAL_TILES = process.env.E2E_REAL_TILES === "1";
const TILE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

interface Snapshot {
  valid_time: string | null;
  spots: Array<{ slug: string; event_at_time: boolean; conditions: null | { score: number; breaking_height_min_ft: number; breaking_height_max_ft: number } }>;
}
interface Spot {
  slug: string;
  name: string;
  country: string;
  region_group: string;
  break_type: string;
}

async function openMap(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const tiles: string[] = [];
  page.on("requestfinished", (r) => {
    if (/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/.test(r.url())) tiles.push(r.url());
  });
  if (!REAL_TILES) {
    await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (route) =>
      route.fulfill({ status: 200, contentType: "image/png", body: TILE_PNG }),
    );
  }
  await page.goto("/map");
  await expect(page.getByLabel("Map of monitored surf spots")).toBeVisible();
  await expect(page.locator("path.spot-marker")).toHaveCount(50);
  return { errors, tiles };
}

const visibleCount = async (page: Page) => Number(/Showing (\d+) of/.exec(await page.getByTestId("visible-count").innerText())![1]);

test.describe("interactive map", () => {
  test("loads with 50 markers, working tiles and no uncaught errors", async ({ page }) => {
    const { errors, tiles } = await openMap(page);
    await expect.poll(async () => page.locator("img.leaflet-tile-loaded").count()).toBeGreaterThan(0);
    expect(tiles.length).toBeGreaterThan(0);
    expect(tiles.every((u) => u.startsWith("https://tile.openstreetmap.org/"))).toBe(true);
    await expect(page.locator(".leaflet-control-attribution")).toContainText("OpenStreetMap");
    await expect(page.getByTestId("map-legend")).toContainText("No usable forecast");
    await expect(page.getByTestId("visible-count")).toHaveText(/Showing 50 of 50 spots/);
    // zoom and pan
    await page.locator(".leaflet-control-zoom-in").click();
    await page.mouse.move(400, 600);
    await page.mouse.down();
    await page.mouse.move(520, 640, { steps: 5 });
    await page.mouse.up();
    expect(errors).toEqual([]);
    if (REAL_TILES) {
      const ok = await page.evaluate(() => [...document.querySelectorAll<HTMLImageElement>("img.leaflet-tile")].every((t) => t.naturalWidth > 0));
      expect(ok, "real OpenStreetMap tiles decoded").toBe(true);
    }
  });

  test("clicking a marker opens that spot's raw model data and our prediction", async ({ page, request }) => {
    await openMap(page);
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const jbay = snap.spots.find((s) => s.slug === "jeffreys-bay")!;
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    const panel = page.getByTestId("map-spot-panel");
    await expect(panel.getByTestId("panel-spot-name")).toHaveText("Jeffreys Bay (Supertubes)");
    await expect(panel.getByTestId("panel-country")).toHaveText("South Africa");
    if (jbay.conditions) {
      await expect(panel.getByTestId("raw-model")).toContainText("Significant wave height (offshore)");
      await expect(panel.getByTestId("raw-model")).toContainText("Main component direction");
      await expect(panel.getByTestId("our-prediction")).toContainText("est. breaking faces");
      await expect(panel.getByTestId("our-prediction")).toContainText(String(jbay.conditions.score));
    } else {
      await expect(panel.getByTestId("no-forecast")).toBeVisible();
    }
    await expect(panel.getByText("Upcoming swell events")).toBeVisible();
    await expect(panel.getByTestId("opps-signed-out")).toBeVisible(); // signed out: no private data
    await panel.getByRole("button", { name: "Close spot details" }).click();
    await expect(panel).toHaveCount(0);
  });

  test("the forecast time control requests cached predictions for the chosen hour", async ({ page }) => {
    await openMap(page);
    const before = await page.getByTestId("time-label").innerText();
    const req = page.waitForRequest((r) => r.url().includes("/api/map/conditions?at="));
    for (let i = 0; i < 8; i++) await page.getByRole("button", { name: "Later" }).click();
    const url = new URL((await req).url());
    const at = new Date(url.searchParams.get("at")!);
    expect(at.getTime() - Date.now()).toBeGreaterThan(20 * 3600_000); // ≥ ~1 day ahead
    await expect(page.getByTestId("time-label")).not.toHaveText(before);
    await page.locator("path.spot-nazare").click({ force: true });
    const shown = await page.getByTestId("forecast-breakdown").innerText();
    expect(shown).toMatch(/local/);
    await page.getByRole("button", { name: "Now" }).click();
    await expect(page.getByTestId("time-label")).toHaveText(before);
  });

  test("filters narrow the visible spots consistently with the data", async ({ page, request }) => {
    await openMap(page);
    const spots = (await apiGet(request, "/api/spots")) as Spot[];
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const bySlug = new Map(snap.spots.map((s) => [s.slug, s]));

    await page.getByRole("button", { name: "Hawaii", exact: true }).click();
    const hawaii = spots.filter((s) => s.region_group === "Hawaii").length;
    await expect.poll(() => visibleCount(page)).toBe(hawaii);
    await expect(page.locator("path.spot-marker")).toHaveCount(hawaii);

    await page.getByRole("button", { name: "All", exact: true }).click();
    await page.getByLabel("Break type").selectOption("reef");
    await expect.poll(() => visibleCount(page)).toBe(spots.filter((s) => s.break_type === "reef").length);

    await page.getByLabel("Break type").selectOption("any");
    await page.getByLabel("Country").selectOption("Indonesia");
    await expect.poll(() => visibleCount(page)).toBe(spots.filter((s) => s.country === "Indonesia").length);

    await page.getByLabel("Country").selectOption("All");
    await page.getByLabel("Minimum quality").selectOption("good");
    const good = snap.spots.filter((s) => s.conditions && s.conditions.score >= 57).length;
    await expect.poll(() => visibleCount(page)).toBe(good);

    await page.getByLabel("Minimum quality").selectOption("");
    await page.getByLabel("Minimum breaking wave height").fill("6");
    const tall = [...bySlug.values()].filter((s) => s.conditions && (s.conditions.breaking_height_min_ft + s.conditions.breaking_height_max_ft) / 2 >= 6).length;
    await expect.poll(() => visibleCount(page)).toBe(tall);

    await page.getByLabel("Minimum breaking wave height").fill("");
    await page.getByLabel("Swell event at this time").check();
    await expect.poll(() => visibleCount(page)).toBe(snap.spots.filter((s) => s.event_at_time).length);

    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect.poll(() => visibleCount(page)).toBe(50);
  });

  test("still works after a reload", async ({ page }) => {
    const { errors } = await openMap(page);
    await page.reload();
    await expect(page.locator("path.spot-marker")).toHaveCount(50);
    await page.locator("path.spot-uluwatu").click({ force: true });
    await expect(page.getByTestId("panel-spot-name")).toHaveText(/Uluwatu/);
    expect(errors).toEqual([]);
  });

  test("signed-in users see their own opportunities in the spot panel", async ({ page }) => {
    const email = `map-${Date.now()}@example.com`;
    await page.goto("/register");
    await page.fill("#full_name", "Map Tester");
    await page.fill("#email", email);
    await page.fill("#password", "Map-tester-pass-1!");
    await page.fill("#confirm_password", "Map-tester-pass-1!");
    await page.getByRole("button", { name: "Create account" }).click();
    await page.waitForURL(/dashboard/);
    await verifyEmailFromOutbox(page, email);
    await apiPost(page, "/api/searches", {
      name: "Map test",
      date_mode: "flexible",
      horizon_days: 30,
      units: "ft",
      wave_min: 3,
      wave_max: 20,
      min_quality: "fair",
      destination_mode: "spots",
      destinations: { spot_slugs: ["jeffreys-bay"] },
      origins: ["SFO"],
      notification_channel: "dashboard",
      priority: "balanced",
      min_window_hours: 3,
      travel: { max_price: 5000, currency: "USD", max_layovers: 2, max_flight_hours: 45, min_days_at_destination: 3, max_trip_days: 14, travelers: 1, arrival_buffer_days: 2, departure_buffer_days: 1 },
    });
    await apiPost(page, "/api/dev/simulate-swell", { spot_slug: "jeffreys-bay", days_ahead: 7, duration_hours: 48, target_breaking_ft: 8, period_s: 16 });
    await openMap(page);
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    const panel = page.getByTestId("map-spot-panel");
    await expect(panel.getByText("Your travel opportunities")).toBeVisible();
    await expect(panel.getByTestId("opps-signed-out")).toHaveCount(0);
    await expect(panel.getByRole("link", { name: /\$|Surf only/ }).first()).toBeVisible();
  });
});
