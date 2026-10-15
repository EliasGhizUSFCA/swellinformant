import { devices, expect, test, type Page } from "@playwright/test";

import { mixedCadence, realTimeline, routeTimeline, withSyntheticTide, type MockTimeline } from "./timeline.mock";

/**
 * The spot page's unified forecast timeline (spec 11-20, 27 chart items) against the demo
 * backend: rows, one time scale, one selection, values equal to /timeline, day navigation,
 * deep links, keyboard and touch. Tests named "(synthetic)" serve a route-mocked payload
 * derived from the demo's own response (see timeline.mock.ts).
 */

const SPOT = "jeffreys-bay";
const ROWS = ["timeline-header", "quality-strip", "wave-bars", "swell-partitions", "wind-row", "tide-row"];

async function blockTiles(page: Page) {
  await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (r) => r.fulfill({ status: 204 }));
}

async function openSpot(page: Page, query = "") {
  await blockTiles(page);
  await page.goto(`/spots/${SPOT}${query}`);
  await expect(page.getByTestId("timeline-scroller")).toBeVisible();
}

const scroller = (page: Page) => page.getByTestId("timeline-scroller");
const valueNow = async (page: Page) => Number(await scroller(page).getAttribute("aria-valuenow"));

/** Scrolls the page so the timeline is on screen and the timeline so that step i is in the
 * visible plot (clear of the sticky label column); returns its quality segment. */
async function revealStep(page: Page, i: number) {
  const seg = page.locator(`[data-testid="quality-strip"] rect[data-step="${i}"]`);
  await scroller(page).evaluate((el, x) => {
    el.scrollIntoView({ block: "center" });
    el.scrollLeft = Math.max(0, x - 200);
  }, Number(await seg.getAttribute("x")));
  await page.waitForTimeout(50);
  return seg;
}

/** Clicks (or taps) the quality segment of step i. */
async function pickStep(page: Page, i: number, how: "click" | "tap" = "click") {
  const seg = await revealStep(page, i);
  const box = (await seg.boundingBox())!;
  if (how === "tap") await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
  else await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

test.describe("spot forecast timeline", () => {
  test("every row loads and the values equal /timeline", async ({ page }) => {
    const api = await realTimeline(page);
    expect(api.available).toBe(true);
    await openSpot(page);
    for (const id of ROWS) await expect(page.getByTestId(id)).toBeVisible();
    await expect(page.locator('[data-testid="quality-strip"] rect[data-step]')).toHaveCount(api.steps.length);
    await expect(page.getByTestId("tide-row")).toContainText("Tide data unavailable");

    const i = await valueNow(page);
    const s = api.steps[i]!;
    await expect(page.locator(`[data-testid="wave-bars"] g[data-step="${i}"]`)).toHaveAttribute("data-central", String(s.breaking.central_ft));
    await expect(page.locator(`[data-testid="wind-row"] g[data-step="${i}"]`)).toHaveAttribute("data-speed", String(s.wind!.speed_kmh));
    await expect(page.getByTestId("summary-breaking")).toHaveAttribute("data-typical", String(s.breaking.typical_ft));
    await expect(page.getByTestId("summary-quality")).toHaveAttribute("data-score", String(s.quality.score));
    await expect(page.getByTestId("summary-time")).toContainText(/SAST$/);
    // Wind arrows point where the wind goes: FROM + 180, the map particles' convention.
    const arrows = await page.locator('[data-testid="wind-row"] g[data-step]').evaluateAll((gs) =>
      gs.flatMap((g) => {
        const a = g.querySelector(".tl-wind-arrow");
        return a ? [[Number(g.getAttribute("data-dir-from")), Number(a.getAttribute("data-arrow-rotation"))]] : [];
      }),
    );
    expect(arrows.length).toBeGreaterThan(api.steps.length / 3);
    for (const [from, rot] of arrows) expect(rot).toBe((from! + 180) % 360);
    await expect(page.getByTestId("quality-chart-legend")).toContainText("Arrows point where the wind or waves are going.");
  });

  test("day boundaries and the selected step line up in every row", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await openSpot(page);
    const dates = await page.locator('[data-testid="quality-strip"] [data-day-boundary]').evaluateAll((els) => els.map((e) => e.getAttribute("data-day-boundary")!));
    expect(dates.length).toBeGreaterThan(3);
    for (const date of dates.slice(0, 4)) {
      const xs = await Promise.all(ROWS.map(async (id) => (await page.getByTestId(id).locator(`[data-day-boundary="${date}"]`).boundingBox())!.x));
      expect(Math.max(...xs) - Math.min(...xs)).toBeLessThanOrEqual(1);
    }
    await pickStep(page, 30);
    const cursor = (await page.locator('[data-cursor="selected"] line').boundingBox())!;
    for (const sel of ['[data-testid="quality-strip"] rect[data-selected]', '[data-testid="wave-bars"] g[data-selected] .tl-bar', '[data-testid="wind-row"] g[data-selected] .tl-bar']) {
      const b = (await page.locator(sel).boundingBox())!;
      expect(Math.abs(b.x + b.width / 2 - (cursor.x + cursor.width / 2)), sel).toBeLessThanOrEqual(1);
    }
    expect(cursor.height).toBeGreaterThan(300); // one line through every row
  });

  test("selecting a time updates every row and the summary without a request", async ({ page }) => {
    const api = await realTimeline(page);
    const calls: string[] = [];
    page.on("request", (r) => {
      if (/\/api\/spots\/[^/]+\/(timeline|forecast)/.test(r.url())) calls.push(r.url());
    });
    await openSpot(page);
    await page.waitForLoadState("networkidle");
    const before = calls.length;
    await pickStep(page, 20);
    expect(await valueNow(page)).toBe(20);
    const t = api.steps[20]!.t;
    for (const id of ["quality-strip", "wave-bars", "swell-partitions", "wind-row"]) {
      await expect(page.getByTestId(id).locator("[data-selected]").first()).toHaveAttribute("data-step", "20");
    }
    await expect(page.getByTestId("summary-breaking")).toHaveAttribute("data-central", String(api.steps[20]!.breaking.central_ft));
    // The selected step is linkable (?t=), written without a navigation.
    await expect.poll(() => new URL(page.url()).searchParams.get("t")).toBe(t);
    // Keyboard: one API step per arrow press.
    await scroller(page).focus();
    await page.keyboard.press("ArrowRight");
    expect(await valueNow(page)).toBe(21);
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    expect(await valueNow(page)).toBe(19);
    await page.getByRole("button", { name: "Next forecast step" }).click();
    expect(await valueNow(page)).toBe(20);
    // The detailed breakdown below follows the same selection.
    await expect(page.getByTestId("forecast-hour").getByTestId("forecast-breakdown")).toBeVisible();
    expect(calls.length).toBe(before);
  });

  test("hover shows the step's values in a tooltip", async ({ page }) => {
    const api = await realTimeline(page);
    await openSpot(page);
    const seg = await revealStep(page, 10);
    const box = (await seg.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + 60);
    const tip = page.getByTestId("timeline-tooltip");
    await expect(tip).toBeVisible();
    const step = Number(await tip.getAttribute("data-step"));
    await expect(tip).toContainText(`${api.steps[step]!.step_hours}-hour step`);
    if (api.steps[step]!.wind?.gust_kmh != null) await expect(tip).toContainText("gust");
  });

  test("day buttons scroll the timeline and keep aria-pressed", async ({ page }) => {
    const api = await realTimeline(page);
    await openSpot(page);
    const day = api.days[4]!;
    const left0 = await scroller(page).evaluate((el) => el.scrollLeft);
    await page.getByTestId(`day-${day.date}`).click();
    await expect(page.getByTestId(`day-${day.date}`)).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => scroller(page).evaluate((el) => el.scrollLeft)).toBeGreaterThan(left0 + 200);
    const i = await valueNow(page);
    if (day.best_time) expect(api.steps[i]!.t).toBe(day.best_time);
    await page.getByRole("button", { name: "Next day" }).click();
    await expect(page.getByTestId(`day-${api.days[5]!.date}`)).toHaveAttribute("aria-pressed", "true");
  });

  test("?t= opens the page on that step", async ({ page }) => {
    const api = await realTimeline(page);
    const s = api.steps[40]!;
    await openSpot(page, `?t=${s.t}`);
    await expect.poll(() => valueNow(page)).toBe(40);
    await expect(page.getByTestId("summary-quality")).toHaveAttribute("data-score", String(s.quality.score));
  });

  test("tide row draws a supplied tide with highs, lows and its datum (synthetic)", async ({ page }) => {
    const tl = withSyntheticTide(await realTimeline(page));
    await routeTimeline(page, tl);
    await openSpot(page);
    await expect(page.locator("[data-tide-curve]")).toHaveCount(1);
    const events = page.locator("[data-tide-event]");
    expect(await events.count()).toBeGreaterThan(10);
    await expect(events.first()).toContainText(/^[HL] \d{1,2}:\d{2} [AP]M$/);
    await expect(page.getByTestId("tide-caption")).toContainText("Synthetic test station (3 km away)");
    await expect(page.getByTestId("tide-caption")).toContainText("heights above model mean sea level");
    await expect(page.getByTestId("tide-preference")).toContainText("Mid tide (synthetic e2e preference).");
    await pickStep(page, 12);
    const marker = page.locator("[data-tide-marker]");
    await expect(marker).toHaveAttribute("data-height", String(tl.steps[12]!.tide!.height_m));
    const m = (await marker.boundingBox())!;
    const cursor = (await page.locator('[data-cursor="selected"] line').boundingBox())!;
    expect(Math.abs(m.x + m.width / 2 - (cursor.x + cursor.width / 2))).toBeLessThanOrEqual(1);
    // ...and sits on the drawn curve (the overlay's row offsets match the rows as laid out).
    const curveY = await page.locator("[data-tide-curve]").evaluate((path: SVGPathElement, x) => {
      const svg = path.ownerSVGElement!.getBoundingClientRect();
      let best = { dx: Infinity, y: 0 };
      for (let l = 0; l <= path.getTotalLength(); l += 0.5) {
        const pt = path.getPointAtLength(l);
        const dx = Math.abs(svg.left + pt.x - x);
        if (dx < best.dx) best = { dx, y: svg.top + pt.y };
      }
      return best.y;
    }, m.x + m.width / 2);
    expect(Math.abs(m.y + m.height / 2 - curveY)).toBeLessThanOrEqual(1.5);
    // Nothing is clipped at the bottom of the frame (the last row fits the content height).
    const fit = await scroller(page).evaluate((el) => el.scrollHeight - el.clientHeight);
    expect(fit).toBeLessThanOrEqual(0);
    await expect(page.getByTestId("summary-tide")).toContainText(/(rising|falling)/);
  });

  test("3-hourly then 6-hourly steps are drawn as wide as they are, never as hourly bars (synthetic)", async ({ page }) => {
    const tl = mixedCadence(await realTimeline(page));
    await routeTimeline(page, tl);
    await openSpot(page);
    const rects = page.locator('[data-testid="quality-strip"] rect[data-step]');
    await expect(rects).toHaveCount(tl.steps.length);
    const w = async (i: number) => Number(await rects.nth(i).getAttribute("width")) + 1;
    const last = tl.steps.length - 2;
    expect((await w(last)) / (await w(3))).toBeCloseTo(2, 3);
    await expect(page.getByTestId("timeline-zone")).toContainText("3-hour steps, from");
    await pickStep(page, last);
    await expect(page.getByTestId("summary-step-length")).toHaveText("6-hour step");
  });

  test("a failed or empty timeline shows a clear state, the rest of the page still works", async ({ page }) => {
    await routeTimeline(page, { detail: "SYNTHETIC failure" }, 500);
    await blockTiles(page);
    await page.goto(`/spots/${SPOT}`);
    // Server errors are retried twice before the error shows.
    await expect(page.getByTestId("timeline-error")).toContainText("could not be loaded", { timeout: 30_000 });
    await expect(page.getByText("Spot characteristics")).toBeVisible();
    await page.unrouteAll();
    const tl: MockTimeline = { ...(await realTimeline(page)), available: false, message: "No forecast has been ingested for this spot yet.", steps: [], days: [] };
    await routeTimeline(page, tl);
    await page.reload();
    await expect(page.getByTestId("timeline-unavailable")).toHaveText("No forecast has been ingested for this spot yet.");
  });
});

// Pixel 7 emulation in every project (the "mobile" project runs it as @mobile; the
// browser type cannot be set per describe, so it is left out).
// eslint-disable-next-line @typescript-eslint/no-unused-vars
const { defaultBrowserType: _browser, ...PIXEL_7 } = devices["Pixel 7"];

test.describe("spot forecast timeline on a phone", () => {
  test.use(PIXEL_7);

  test("about one day in view, swipe to scroll, tap to select, no page-wide horizontal scroll", { tag: "@mobile" }, async ({ page }) => {
    const api = await realTimeline(page);
    await openSpot(page);
    const sc = scroller(page);
    await sc.evaluate((el) => el.scrollIntoView({ block: "center" }));
    const dims = await sc.evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth, left: el.scrollLeft }));
    expect(dims.sw).toBeGreaterThan(dims.cw * 4);
    // About one day across the visible plot.
    const strip = page.locator('[data-testid="quality-strip"] rect[data-step]');
    const pxPerStep = (await strip.nth(5).boundingBox())!.x - (await strip.nth(4).boundingBox())!.x;
    const hoursInView = ((dims.cw - 66) / pxPerStep) * api.steps[4]!.step_hours;
    expect(hoursInView).toBeGreaterThan(18);
    expect(hoursInView).toBeLessThan(32);
    // A touch swipe (raw touch events; the browser scrolls natively) scrolls the timeline, not
    // the page, and selects nothing.
    const selectedBefore = await valueNow(page);
    const box = (await sc.boundingBox())!;
    const cdp = await page.context().newCDPSession(page);
    const [x0, y0] = [Math.round(box.x + box.width * 0.8), Math.round(box.y + 140)];
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: x0, y: y0 }] });
    for (let k = 1; k <= 12; k++) await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: x0 - k * 20, y: y0 }] });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect.poll(() => sc.evaluate((el) => el.scrollLeft)).toBeGreaterThan(dims.left + 120);
    expect(await valueNow(page)).toBe(selectedBefore);
    // Tap selects the step under the finger.
    const before = await valueNow(page);
    await pickStep(page, before + 5, "tap");
    expect(await valueNow(page)).toBe(before + 5);
    await expect(page.getByTestId("summary-quality")).toHaveAttribute("data-score", String(api.steps[before + 5]!.quality.score));
    // Day boundaries line up on the phone too.
    const date = await page.locator('[data-testid="quality-strip"] [data-day-boundary]').first().getAttribute("data-day-boundary");
    const xs = await Promise.all(ROWS.map(async (id) => (await page.getByTestId(id).locator(`[data-day-boundary="${date}"]`).boundingBox())!.x));
    expect(Math.max(...xs) - Math.min(...xs)).toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  });
});
