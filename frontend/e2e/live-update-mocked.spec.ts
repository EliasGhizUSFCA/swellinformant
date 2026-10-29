import { expect, test, type Page, type Route } from "@playwright/test";

import { stubTiles } from "./map-helpers";

/**
 * Open pages follow a new forecast version without a reload (spec 12, design D11), with the
 * version endpoint and the versioned data served by page.route: version 101, then 102.
 * Version 101's data is the demo backend's own answer; everything version 102 changes, the
 * versions' times and the seen beacon's 204 are SYNTHETIC and exist only in this test. The
 * same flow against the real refresh pipeline is live-update.spec.ts.
 *
 * Each test proves, without a reload (a window marker set before the update survives it):
 * the new values appear within one poll interval (NEXT_PUBLIC_FORECAST_VERSION_POLL_MS, baked
 * into the build by scripts/run-e2e.sh), the "Forecast updated" label changes, the selected
 * spot and time are kept, the data is read under ?v=102, and the seen beacon reports 102 once.
 */

const POLL_MS = Number(process.env.NEXT_PUBLIC_FORECAST_VERSION_POLL_MS || 20_000);
// One poll interval, plus the versioned reads and the render (two animation frames).
const WITHIN = POLL_MS + 8_000;
const SLUG = "jeffreys-bay";

interface VersionMock {
  current: number;
  seen: number[];
  /** Data requests made for each version (?v=), and those without one. */
  reads: Map<string, string[]>;
}

/** The C4 body of a SYNTHETIC version; 101 was published 3 hours ago, 102 just now. */
function versionBody(id: number, publishedAt: string) {
  return {
    version_id: id,
    published_at: publishedAt,
    provider: "demo",
    source_code: "demo",
    wind_model: "demo-diurnal-wind",
    wind_model_run: null,
    wind_run_estimated: false,
    wave_model: "demo-swell-generator",
    wave_model_run: null,
    wave_run_estimated: false,
    spots_total: 50,
    spots_updated: 50,
    spots_stale: 0,
    map_run_id: null,
    map_cycle: null,
    attribution: [],
  };
}

async function serveVersions(page: Page): Promise<VersionMock> {
  const mock: VersionMock = { current: 101, seen: [], reads: new Map() };
  const published = new Map([
    [101, new Date(Date.now() - 3 * 3_600_000).toISOString()],
    [102, new Date(Date.now() - 5_000).toISOString()],
  ]);
  await page.route(/\/api\/forecasts\/version(\?|$)/, (route) =>
    route.fulfill({ json: versionBody(mock.current, published.get(mock.current)!), headers: { "Cache-Control": "no-cache" } }),
  );
  await page.route(/\/api\/forecasts\/seen$/, async (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as { version_id?: number };
    if (typeof body.version_id === "number") mock.seen.push(body.version_id);
    await route.fulfill({ status: 204 });
  });
  return mock;
}

/** Serves the backend's own answer for version 101 (or no version) and `patch(json)` of it
 * for version 102; records which version each read asked for. */
async function versioned(page: Page, mock: VersionMock, pattern: RegExp, patch: (json: unknown) => void) {
  await page.route(pattern, async (route: Route) => {
    const url = new URL(route.request().url());
    const v = url.searchParams.get("v") ?? "none";
    mock.reads.set(v, [...(mock.reads.get(v) ?? []), url.pathname + url.search]);
    // The backend answers the same data whatever ?v= says: ask it without one.
    url.searchParams.delete("v");
    const response = await route.fetch({ url: url.toString() });
    const json: unknown = await response.json();
    if (v === "102") patch(json);
    await route.fulfill({ response, json });
  });
}

const marker = (page: Page) => page.evaluate(() => (window as unknown as { __liveMarker?: number }).__liveMarker);
const setMarker = (page: Page) => page.evaluate(() => ((window as unknown as { __liveMarker?: number }).__liveMarker = 42));

test.describe("live forecast updates (route-mocked versions)", () => {
  test("the map's markers, spot panel and label follow version 102; spot and time are kept", { tag: "@mobile" }, async ({ page, isMobile }) => {
    const mock = await serveVersions(page);
    // Version 102 (SYNTHETIC): every spot's rating flips between poor and excellent and its
    // breaking heights grow by 3 ft, so every marker's label changes.
    await versioned(page, mock, /\/api\/map\/conditions(\?|$)/, (json) => {
      for (const s of (json as { spots: Array<{ conditions: null | { score: number; label: string; breaking_height_min_ft: number; breaking_height_max_ft: number } }> }).spots) {
        const c = s.conditions;
        if (!c) continue;
        const high = c.score >= 50;
        c.score = high ? 12 : 93;
        c.label = high ? "poor" : "excellent";
        c.breaking_height_min_ft += 3;
        c.breaking_height_max_ft += 3;
      }
    });
    await stubTiles(page);
    await page.goto("/map");
    await expect(page.locator("path.spot-marker")).toHaveCount(50);
    const label = page.getByTestId("forecast-updated");
    await expect(label).toHaveAttribute("data-version-id", "101");
    // The publication's clock time, with its date when that was another day (version 101 is
    // 3 h old: on a run in the first 3 h after midnight it was published yesterday).
    await expect(label).toContainText(/Forecast updated (?:[A-Z][a-z]{2} \d{1,2}, )?\d{2}:\d{2}/);
    await expect(page.getByTestId("forecast-updated-age")).toContainText("3 h ago");

    // Select J-Bay (isolated, so its own marker takes the tap) and a later time.
    await page.locator(`path.spot-${SLUG}`).click();
    const panel = page.getByTestId("map-spot-panel");
    await expect(panel.getByTestId("panel-spot-name")).toHaveText(/^Jeffreys Bay/);
    const track = page.getByTestId("timeline-track");
    await track.focus();
    for (let i = 0; i < 3; i++) await page.keyboard.press("ArrowRight");
    await expect(track).toHaveAttribute("aria-valuenow", "3");
    const time = (await page.getByTestId("time-label").textContent())!;
    // The selected time's prediction (version 101), as the panel and the marker show it.
    const rating = panel.getByTestId("our-prediction").getByTestId("rating-title");
    await expect(rating).toContainText(/\d\.\d\/10/);
    const score101 = /(\d+\.\d)\/10/.exec((await rating.textContent()) ?? "")?.[1];
    expect(score101, "a rating x.y/10 in the panel").toBeTruthy();
    const score102 = Number(score101) >= 5 ? "1.2" : "9.3";
    if (!isMobile) {
      await page.locator(`path.spot-${SLUG}`).focus();
      await expect(page.locator(".leaflet-tooltip")).toContainText(`${score101}/10`);
    }
    await setMarker(page);

    const switched = Date.now();
    mock.current = 102;
    await expect(label).toHaveAttribute("data-version-id", "102", { timeout: WITHIN });
    await expect(rating).toContainText(`${score102}/10`, { timeout: WITHIN });
    const shownAfter = Date.now() - switched;
    if (!isMobile) await expect(page.locator(".leaflet-tooltip")).toContainText(`${score102}/10`);
    await expect(page.getByTestId("forecast-updated-age")).toContainText("just now");
    await expect(page.getByTestId("forecast-updated-announcement")).toHaveText("New forecast loaded");
    // Same spot, same time, no reload.
    await expect(panel.getByTestId("panel-spot-name")).toHaveText(/^Jeffreys Bay/);
    await expect(page.getByTestId("time-label")).toHaveText(time);
    await expect(track).toHaveAttribute("aria-valuenow", "3");
    expect(await marker(page)).toBe(42);
    // Every read of the new version carried ?v=102, and none went out without a version.
    expect(mock.reads.get("102")?.length ?? 0).toBeGreaterThan(0);
    expect(mock.reads.get("none") ?? []).toEqual([]);
    await expect.poll(() => mock.seen, { timeout: 5_000 }).toEqual([102]);
    console.log(`map: version 102 on screen ${shownAfter} ms after the switch (poll ${POLL_MS} ms)`);
  });

  test("the spot page's charts and summary follow version 102; the selected step and scroll are kept", { tag: "@mobile" }, async ({ page }) => {
    const mock = await serveVersions(page);
    // Version 102 (SYNTHETIC): every step's breaking heights 1.5 ft higher.
    await versioned(page, mock, new RegExp(`/api/spots/${SLUG}/timeline`), (json) => {
      for (const s of (json as { steps: Array<{ breaking: { central_ft: number; typical_ft: number; sets_ft: number } }> }).steps) {
        s.breaking.central_ft = Math.round((s.breaking.central_ft + 1.5) * 100) / 100;
        s.breaking.typical_ft = Math.round((s.breaking.typical_ft + 1.5) * 10) / 10;
        s.breaking.sets_ft = Math.round((s.breaking.sets_ft + 1.5) * 10) / 10;
      }
    });
    await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (r) => r.fulfill({ status: 204 }));
    await page.goto(`/spots/${SLUG}`);
    const scroller = page.getByTestId("timeline-scroller");
    await expect(scroller).toBeVisible();
    const label = page.getByTestId("forecast-updated");
    await expect(label).toHaveAttribute("data-version-id", "101");

    // Select step 20 (scrolled into view, as a visitor would).
    const step = 20;
    const seg = page.locator(`[data-testid="quality-strip"] rect[data-step="${step}"]`);
    await scroller.evaluate((el, x) => {
      el.scrollIntoView({ block: "center" });
      el.scrollLeft = Math.max(0, x - 200);
    }, Number(await seg.getAttribute("x")));
    const box = (await seg.boundingBox())!;
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await expect(scroller).toHaveAttribute("aria-valuenow", String(step));
    const bar = page.locator(`[data-testid="wave-bars"] g[data-step="${step}"]`);
    const central101 = Number(await bar.getAttribute("data-central"));
    const expected = String(Math.round((central101 + 1.5) * 100) / 100);
    const time = await page.getByTestId("summary-time").textContent();
    const scrollLeft = await scroller.evaluate((el) => el.scrollLeft);
    await setMarker(page);

    const switched = Date.now();
    mock.current = 102;
    await expect(bar).toHaveAttribute("data-central", expected, { timeout: WITHIN });
    const shownAfter = Date.now() - switched;
    await expect(label).toHaveAttribute("data-version-id", "102");
    await expect(page.getByTestId("summary-breaking")).toHaveAttribute("data-central", expected);
    await expect(page.getByTestId("forecast-updated-announcement")).toHaveText("New forecast loaded");
    // Same step, same scroll position, same summary time, no reload, no skeleton in between.
    await expect(scroller).toHaveAttribute("aria-valuenow", String(step));
    await expect(bar).toHaveAttribute("data-selected", "");
    await expect(page.getByTestId("summary-time")).toHaveText(time!);
    expect(await scroller.evaluate((el) => el.scrollLeft)).toBe(scrollLeft);
    expect(await marker(page)).toBe(42);
    expect(mock.reads.get("102")?.length).toBe(1);
    expect(mock.reads.get("none") ?? []).toEqual([]);
    await expect.poll(() => mock.seen, { timeout: 5_000 }).toEqual([102]);
    console.log(`spot page: version 102 on screen ${shownAfter} ms after the switch (poll ${POLL_MS} ms)`);
  });
});
