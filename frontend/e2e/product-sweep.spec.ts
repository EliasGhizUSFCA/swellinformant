import { expect, test, type Page } from "@playwright/test";

import type { SpotDetail } from "../types/api";

import { MATCH_ID, matchDetail, matchSummary, notifications, SCENARIOS, swellEvent, user, type Scenario } from "./confidence.mock";
import { apiGet } from "./helpers";
import { mockFieldsUnavailable } from "./map-fields.mock";

/**
 * Product review fixes, checked at 1440×900 and 390×844 (with touch):
 *
 * * the disputed-wind note (forecast-v1.1) is shown next to the wind it qualifies, not only
 *   inside "Confidence details";
 * * without ocean layers the map's notice is one short sentence that stays clear of the spot
 *   list and of the map, with the backend's setup message kept in the legend;
 * * keyboard users reach the map controls before the 50 markers, and Enter on a marker opens
 *   that spot;
 * * long job names on the status page wrap on a phone;
 * * the landing page's forecast panel says "demo forecast" in demo mode.
 *
 * The spot pages and the map use the demo backend as it is (its demo forecasts carry the
 * disputed-wind note at the spots the committed research marks). Signed-in pages, the
 * degraded data-health report and the shortened map message use SYNTHETIC page.route mocks
 * (confidence.mock.ts, map-fields.mock.ts, the report below), labelled as such. Set
 * UI_SCREENS_DIR to also save screenshots.
 */
const SCREENS = process.env.UI_SCREENS_DIR;
const DESKTOP = { width: 1440, height: 900 };
const MOBILE = { width: 390, height: 844 };
/** The backend's note, word for word (backend forecast_confidence.WIND_DISPUTE_REASON). */
const WIND_NOTE = "Sources disagree about which wind is offshore here.";

async function shot(page: Page, name: string) {
  if (SCREENS) await page.screenshot({ path: `${SCREENS}/${name}.png`, fullPage: true });
}

async function blockTiles(page: Page) {
  await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (r) => r.fulfill({ status: 204 }));
}

async function expectNoHorizontalScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
}

function overlaps(a: { x: number; y: number; width: number; height: number }, b: { x: number; y: number; width: number; height: number }) {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

for (const [device, viewport] of [
  ["desktop", DESKTOP],
  ["mobile", MOBILE],
] as const) {
  test.describe(`disputed offshore wind · ${device}`, () => {
    test.use({ viewport, hasTouch: device === "mobile", isMobile: device === "mobile" });

    test("the note sits next to the wind on the spot page, without opening any details", async ({ page }) => {
      await blockTiles(page);
      const disputed = (await apiGet(page.request, "/api/spots/chicama")) as SpotDetail;
      const calm = (await apiGet(page.request, "/api/spots/pipeline")) as SpotDetail;
      // Precondition from the API itself: Chicama's hours carry the note, Pipeline's do not.
      expect(disputed.best_upcoming?.confidence_reasons).toContain(WIND_NOTE);
      expect(calm.best_upcoming?.confidence_reasons ?? []).not.toContain(WIND_NOTE);

      await page.goto("/spots/chicama");
      const outlook = page.getByTestId("surf-outlook");
      await expect(outlook.getByTestId("wind-dispute-note")).toHaveText(WIND_NOTE);
      await expect(outlook.getByTestId("wind-dispute-note")).toBeVisible();
      // Every confidence detail stays closed: the note is visible on its own.
      for (const toggle of await outlook.getByRole("button", { name: "Confidence details" }).all()) await expect(toggle).toHaveAttribute("aria-expanded", "false");
      // The hour-by-hour breakdown (next to the model's wind) and the "Now" card show it too.
      await expect(page.getByTestId("forecast-breakdown").getByTestId("wind-dispute-note")).toHaveText(WIND_NOTE);
      await expect(page.getByTestId("conditions-summary").getByTestId("wind-dispute-note")).toHaveText(WIND_NOTE);
      // It is not a second copy of the class: the badge and its first reason are unchanged.
      await expect(outlook.getByTestId("forecast-confidence-badge")).toHaveText(/^Forecast confidence: (High|Moderate|Low|Not assessed yet)$/);
      await expectNoHorizontalScroll(page);
      await shot(page, `sweep-wind-note-spot-${device}`);

      await page.goto("/spots/pipeline");
      await expect(page.getByTestId("surf-outlook")).toBeVisible();
      await expect(page.getByTestId("wind-dispute-note")).toHaveCount(0);
    });

    test("the map's spot panel shows the note for a disputed spot", async ({ page }) => {
      await blockTiles(page);
      await page.goto("/map");
      await expect(page.locator("path.spot-marker")).toHaveCount(50);
      if (device === "mobile") {
        // On a phone the spot is opened from the list (Cloudbreak is in the demo's top 15).
        await page.getByRole("button", { name: /Best spots now/ }).tap();
        await page.getByTestId("top-spots").getByRole("button", { name: /^Cloudbreak/ }).tap();
      } else {
        await page.getByTestId("top-spots").getByRole("button", { name: /^Cloudbreak/ }).click();
      }
      const panel = page.getByTestId("map-spot-panel");
      await expect(panel.getByTestId("panel-spot-name")).toHaveText("Cloudbreak");
      await expect(panel.getByTestId("forecast-breakdown").getByTestId("wind-dispute-note")).toHaveText(WIND_NOTE);
      await shot(page, `sweep-wind-note-map-${device}`);
    });

    test("dashboard cards and the opportunity show the note with the wind (synthetic)", async ({ page, baseURL }) => {
      const sc: Scenario = { ...SCENARIOS.highResearchLowForecast, reasons: [...SCENARIOS.highResearchLowForecast.reasons, WIND_NOTE] };
      await blockTiles(page);
      await page.context().addCookies([{ name: "sta_session", value: "synthetic-test-session", url: baseURL ?? "http://localhost:3000" }]);
      await page.route(/\/api\/auth\/me$/, (r) => r.fulfill({ json: user() }));
      await page.route(/\/api\/searches$/, (r) => r.fulfill({ json: [] }));
      await page.route(/\/api\/opportunities\?/, (r) => r.fulfill({ json: [matchSummary(sc)] }));
      await page.route(new RegExp(`/api/opportunities/${MATCH_ID}$`), (r) => r.fulfill({ json: matchDetail(sc) }));
      await page.route(/\/api\/notifications\?/, (r) => r.fulfill({ json: notifications(sc) }));
      await page.route(/\/api\/events\?/, (r) =>
        r.fulfill({
          json: [
            swellEvent(sc, { id: "00000000-0000-4000-8000-0000000000e1", name: "Synthetic Point (test fixture)", startH: 150, score: 89 }),
            swellEvent(SCENARIOS.lowResearchHighForecast, { id: "00000000-0000-4000-8000-0000000000e2", name: "Test Reef (synthetic)", startH: 60, score: 74 }),
          ],
        }),
      );
      await page.goto("/dashboard");
      const cards = page.getByTestId("event-card");
      await expect(cards).toHaveCount(2);
      await expect(cards.nth(0).getByTestId("wind-dispute-note")).toHaveText(WIND_NOTE);
      await expect(cards.nth(1).getByTestId("wind-dispute-note")).toHaveCount(0);
      await expectNoHorizontalScroll(page);
      await shot(page, `sweep-wind-note-dashboard-${device}`);

      await page.goto(`/opportunities/${MATCH_ID}`);
      await expect(page.getByTestId("swell-overview").getByTestId("wind-dispute-note")).toHaveText(WIND_NOTE);
      await expectNoHorizontalScroll(page);
      await shot(page, `sweep-wind-note-opportunity-${device}`);
    });
  });

  test.describe(`map without ocean layers · ${device}`, () => {
    test.use({ viewport, hasTouch: device === "mobile", isMobile: device === "mobile" });

    test("the notice is one short sentence, clear of the spot list; the setup message is in the legend", async ({ page }) => {
      // SYNTHETIC: the backend's longest "unavailable" message, served by a mock.
      const message =
        "Ocean layers need live NOAA model data: add noaa_gfs to FORECAST_PROVIDERS, or set MAP_FIELDS_ENABLED=true to fetch only the global NOAA GFS-Wave and GFS fields (python -m app.cli engine map-fields).";
      await mockFieldsUnavailable(page, message);
      await blockTiles(page);
      await page.goto("/map");
      await expect(page.locator("path.spot-marker")).toHaveCount(50);
      const notice = page.getByTestId("fields-notice");
      await expect(notice).toHaveText("Ocean layers are unavailable right now.");
      const box = (await notice.boundingBox())!;
      // One line: it never covers a block of the map (or its markers) on a phone.
      expect(box.height).toBeLessThanOrEqual(40);
      if (device === "desktop") {
        const list = (await page.getByRole("region", { name: "Best spots now" }).boundingBox())!;
        expect(overlaps(box, list), "notice under the spot list").toBe(false);
        await page.getByTestId("top-spots").getByRole("button").first().click();
        const panel = (await page.getByTestId("map-spot-panel").boundingBox())!;
        expect(overlaps(box, panel), "notice under the spot panel").toBe(false);
        await page.getByRole("button", { name: "Close spot details" }).click();
      }
      await shot(page, `sweep-map-unavailable-${device}`);
      await page.getByRole("button", { name: "Legend", exact: true }).click();
      await expect(page.getByTestId("legend-source")).toHaveText(message);
    });
  });
}

test.describe("map keyboard access", () => {
  test.use({ viewport: DESKTOP });

  test("Tab reaches the layer tabs, settings, legend and timeline before the markers, and Enter opens a marker's spot", async ({ page }) => {
    await blockTiles(page);
    await page.goto("/map");
    await expect(page.locator("path.spot-marker")).toHaveCount(50);
    const reached: string[] = [];
    let firstMarker = -1;
    for (let i = 0; i < 80 && firstMarker < 0; i++) {
      await page.keyboard.press("Tab");
      const what = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el) return "";
        if (el.matches("path.spot-marker")) return "marker";
        return (el.getAttribute("aria-label") ?? el.textContent ?? "").trim();
      });
      if (what === "marker") firstMarker = i;
      else reached.push(what);
    }
    expect(firstMarker, "a marker is reachable by Tab").toBeGreaterThan(0);
    for (const control of ["Wave Height", "Pressure", "Settings", "Legend", "Forecast time"]) {
      expect(reached.some((r) => r.startsWith(control)), `${control} before the first marker (reached: ${reached.join(" | ")})`).toBe(true);
    }
    // The focused marker opens its spot with Enter (and its tooltip names it).
    const slug = await page.evaluate(() => [...(document.activeElement?.classList ?? [])].find((c) => c.startsWith("spot-") && c !== "spot-marker")?.slice(5));
    const spot = (await apiGet(page.request, `/api/spots/${slug}`)) as SpotDetail;
    await expect(page.locator(".leaflet-tooltip")).toContainText(spot.name);
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("panel-spot-name")).toHaveText(spot.name);
    await page.getByRole("button", { name: "Close spot details" }).click();
    await expect(page.getByTestId("map-spot-panel")).toHaveCount(0);
    // Space works too.
    await page.locator(`path.spot-${slug}`).focus();
    await page.keyboard.press("Space");
    await expect(page.getByTestId("panel-spot-name")).toHaveText(spot.name);
  });
});

test.describe("status page with degraded data health on a phone (synthetic)", () => {
  test.use({ viewport: MOBILE, hasTouch: true, isMobile: true });

  test("long job names wrap instead of widening the page", async ({ page }) => {
    const now = Date.now();
    const ago = (h: number) => new Date(now - h * 3_600_000).toISOString();
    const message = "Job verification.collect_point_forecasts has not run for 14.0 h (schedule: every 6 h) (synthetic test report).";
    // SYNTHETIC data-health report (shape of GET /api/health/data); describes no real run.
    await page.route(/\/api\/health\/data/, (r) =>
      r.fulfill({
        json: {
          status: "degraded",
          checked_at: ago(0),
          reasons: [message],
          problems: [{ key: "job_overdue:verification.collect_point_forecasts", severity: "degraded", message }],
          forecast_sources: [
            { code: "synthetic", name: "Synthetic source (test fixture)", role: "forecast", status: "ok", newest_run_key: "synthetic:1", newest_issued_at: ago(3), age_hours: 3, stale_after_hours: 16, latest_attempt_status: "success", latest_attempt_started_at: ago(3) },
          ],
          jobs: [
            { job: "verification.collect_point_forecasts", schedule: "every 6 h", status: "overdue", last_status: "success", last_started_at: ago(14), last_finished_at: ago(14), last_duration_ms: 2100, last_success_at: ago(14), consecutive_failures: 0, overdue_after_minutes: 750 },
            { job: "forecasts.check_staleness", schedule: "every hour", status: "ok", last_status: "success", last_started_at: ago(0.5), last_finished_at: ago(0.5), last_duration_ms: 30, last_success_at: ago(0.5), consecutive_failures: 0, overdue_after_minutes: 150 },
          ],
          map_fields: { mode: "off", status: "off", run_key: null, cycle_time: null, age_hours: null, stale_after_hours: null, complete_valid_times: 0, last_valid_time: null },
          observations: { enabled: false, providers: [] },
          heartbeat: { last_job_finished_at: ago(0.02), age_minutes: 1, limit_minutes: 30 },
          storage: { database_bytes: 150 * 1024 ** 2, database_max_gb: null },
        },
      }),
    );
    await page.goto("/status");
    await expect(page.getByTestId("data-health-summary")).toContainText("Data health: Degraded");
    await expect(page.getByTestId("data-health-reasons").getByRole("listitem")).toHaveText([message]);
    await expect(page.getByTestId("data-health-jobs")).toContainText("verification.collect_point_forecasts");
    await expectNoHorizontalScroll(page);
    await shot(page, "sweep-status-degraded-mobile");
  });
});

test.describe("landing page in demo mode", () => {
  test.use({ viewport: DESKTOP });

  test("the forecast panel says demo, not live", async ({ page }) => {
    const status = (await apiGet(page.request, "/api/system/status")) as { mode: { forecast_providers: string[] } };
    expect(status.mode.forecast_providers).toContain("demo");
    await page.goto("/");
    const panel = page.getByRole("heading", { name: "Swells on the radar" }).locator("..");
    await expect(panel).toContainText("demo forecast");
    await expect(panel).not.toContainText("live forecast");
  });
});
