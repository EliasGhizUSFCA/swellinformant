import { expect, test, type Page } from "@playwright/test";

import { apiGet } from "./helpers";
import { mockFields, SYNTHETIC_SOURCE, syntheticValidTimes } from "./map-fields.mock";
import {
  installMapProbe,
  liveView,
  mapMoves,
  mapSettled,
  markerCalls,
  markerCentre,
  markerCoverage,
  mouseDrag,
  openMap,
  openMapWithFields,
  pinch,
  resetProbe,
  setMapView,
  shot,
  stubTiles,
  touchDrag,
  type LiveView,
  type ProbeWindow,
  type Snapshot,
  type Spot,
} from "./map-helpers";

/**
 * Map navigation against the production build: the latitude band, the one-world minimum zoom,
 * endless panning across the date line with markers in their nearest world copy, flights to a
 * spot, marker update counts and the phone gestures. Leaflet's own map object is read through
 * the probe in map-helpers.ts (live zoom, centre and projection). Ocean fields, where used, are
 * route-mocked SYNTHETIC data from map-fields.mock.ts, not a forecast.
 */

// The navigation limits, restated here so the test checks the map against the spec rather
// than against lib/map-bounds.ts itself.
const LAT_NORTH = 84;
const LAT_SOUTH = -78.5;
const mercY = (lat: number) => 0.5 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / (2 * Math.PI);
const BAND = mercY(LAT_SOUTH) - mercY(LAT_NORTH);
const minZoomFor = (w: number, h: number) => Math.max(1, Math.log2(w / 256), Math.log2(h / (256 * BAND)));

type SpotPos = Spot & { latitude: number; longitude: number; timezone: string };

async function spotList(page: Page): Promise<SpotPos[]> {
  return (await apiGet(page.request, "/api/spots")) as SpotPos[];
}

// The most rows outside the band seen in the current test, reported as an annotation.
let worstRows = { rows: 0, blank: 0, checks: 0 };
test.afterEach(() => {
  if (worstRows.checks) test.info().annotations.push({ type: "rows outside the band (worst view)", description: JSON.stringify(worstRows) });
  worstRows = { rows: 0, blank: 0, checks: 0 };
});

/** One world wide at most, at the minimum zoom for the container, with no rows past the band. */
function expectBounded(v: LiveView, label: string) {
  worstRows = { rows: Math.max(worstRows.rows, v.rowsOutsideBand), blank: Math.max(worstRows.blank, v.blankRows), checks: worstRows.checks + 1 };
  expect(v.zoom, `${label}: zoom ${v.zoom} >= minZoom ${v.minZoom}`).toBeGreaterThanOrEqual(v.minZoom - 1e-9);
  expect(v.minZoom, `${label}: minZoom follows the container`).toBeCloseTo(minZoomFor(v.size.x, v.size.y), 6);
  expect(v.size.x, `${label}: the view shows no longitude twice`).toBeLessThanOrEqual(v.worldPx + 0.5);
  expect(v.rowsOutsideBand, `${label}: rows outside 84N/78.5S`).toBeLessThanOrEqual(2);
  expect(v.blankRows, `${label}: rows past the Mercator edge`).toBe(0);
}

/** A point inside the map, clear of the floating controls. */
async function mapPoint(page: Page, fx = 0.4, fy = 0.45) {
  const box = (await page.locator(".leaflet-container").boundingBox())!;
  return { x: box.x + box.width * fx, y: box.y + box.height * fy };
}

/**
 * The band of open map between the top controls (layer tabs, notices) and the bottom stack
 * (locate, best spots, timeline), with a horizontal position clear of the edge controls.
 */
async function freeStrip(page: Page) {
  const map = (await page.locator(".leaflet-container").boundingBox())!;
  let top = map.y;
  for (const l of [page.getByTestId("layer-tabs"), page.getByTestId("fields-notice")]) {
    const b = (await l.count()) ? await l.first().boundingBox() : null;
    if (b) top = Math.max(top, b.y + b.height);
  }
  const locate = (await page.getByRole("button", { name: "Show my location" }).boundingBox())!;
  return { top: top + 6, bottom: locate.y - 6, x: map.x + map.width * 0.5 };
}

const lngTravel = (moves: Array<{ lng: number }>) => {
  const lngs = moves.map((m) => m.lng);
  return Math.max(...lngs) - Math.min(...lngs);
};

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
  { width: 1280, height: 720 },
]) {
  test.describe(`map navigation · desktop ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("opens one world wide with every marker, and drags and zooms stay inside the band", async ({ page }) => {
      const { errors } = await openMap(page);
      const spots = await spotList(page);
      await mapSettled(page);
      const v0 = await liveView(page);
      expect(v0.options).toEqual({ worldCopyJump: false, maxBoundsViscosity: 1, bounceAtZoomLimits: false, maxBounds: true });
      expect(v0.zoom).toBeCloseTo(Math.max(2, minZoomFor(v0.size.x, v0.size.y)), 6);
      expectBounded(v0, "initial view");
      test.info().annotations.push({ type: "initial view", description: JSON.stringify({ size: v0.size, zoom: v0.zoom, minZoom: v0.minZoom, worldPx: v0.worldPx, rowsOutsideBand: v0.rowsOutsideBand }) });
      const cover = await markerCoverage(page, spots);
      expect(cover.missing).toEqual([]);
      expect(cover.onScreen).toBe(50);
      await shot(page, `nav-${viewport.width}x${viewport.height}-initial`);

      // Three 600 px drags towards the north (the map moves down), then four to the south.
      for (let i = 0; i < 3; i++) {
        await mouseDrag(page, await mapPoint(page, 0.4, 0.2), 0, 600);
        expectBounded(await liveView(page), `after drag north ${i + 1}`);
      }
      await shot(page, `nav-${viewport.width}x${viewport.height}-dragged-north`);
      expect((await liveView(page)).centre.lat).toBeGreaterThan(20);
      for (let i = 0; i < 4; i++) {
        await mouseDrag(page, await mapPoint(page, 0.4, 0.85), 0, -600);
        expectBounded(await liveView(page), `after drag south ${i + 1}`);
      }
      await shot(page, `nav-${viewport.width}x${viewport.height}-dragged-south`);
      expect((await liveView(page)).centre.lat).toBeLessThan(0);

      // Zoom in, then six wheel zoom-outs: never below the one-world zoom, never a blank row.
      await page.locator(".leaflet-control-zoom-in").click();
      await mapSettled(page);
      const p = await mapPoint(page, 0.3, 0.3);
      await page.mouse.move(p.x, p.y);
      for (let i = 0; i < 6; i++) {
        await page.mouse.wheel(0, 400);
        await mapSettled(page, 300);
        expectBounded(await liveView(page), `after wheel zoom-out ${i + 1}`);
      }
      const v1 = await liveView(page);
      expect(v1.zoom).toBeCloseTo(v1.minZoom, 6);
      expect(errors).toEqual([]);
    });
  });
}

test.describe("map navigation · date line (1440x900)", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("centred on 179E and on 170W, every spot in view has its marker there", async ({ page }) => {
    await openMap(page);
    const spots = await spotList(page);
    for (const lng of [179, -170]) {
      await setMapView(page, 15, lng, 3);
      await mapSettled(page);
      const c = await markerCoverage(page, spots);
      expect(c.missing, `centre ${lng}`).toEqual([]);
      expect(c.inView, `centre ${lng}: spots in view`).toBe(38);
      expect(c.onScreen).toBe(38);
      await shot(page, `nav-1440x900-centre-${lng}-z3`);
    }
  });

  test("Cloudbreak to Pipeline flies the short way, and Pipeline's marker stays put during a drag", async ({ page }) => {
    await openMap(page);
    const spots = await spotList(page);
    const pipeline = spots.find((s) => s.slug === "pipeline")!;
    await page.locator("path.spot-cloudbreak").dispatchEvent("click");
    await expect(page.getByTestId("panel-spot-name")).toHaveText("Cloudbreak");
    await mapSettled(page);
    await resetProbe(page);
    await page.locator("path.spot-pipeline").dispatchEvent("click");
    await expect(page.getByTestId("panel-spot-name")).toHaveText(/Pipeline/);
    await mapSettled(page);
    const travel = lngTravel(await mapMoves(page));
    test.info().annotations.push({ type: "Cloudbreak to Pipeline flight", description: `${travel.toFixed(2)} degrees of longitude` });
    expect(travel, "degrees of longitude flown").toBeLessThan(40);
    expect(travel).toBeGreaterThan(20); // it did fly (Fiji to Hawaii is 24.8 degrees)

    // A slow 25-frame drag after the flight: the marker sits where Leaflet projects Pipeline's
    // nearest copy in every frame.
    const start = await mapPoint(page, 0.35, 0.5);
    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    let worst = 0;
    for (let i = 1; i <= 25; i++) {
      await page.mouse.move(start.x - 12 * i, start.y + 2 * i);
      const off = await page.evaluate(
        ({ lat, lon }) =>
          new Promise<number>((resolve) =>
            requestAnimationFrame(() =>
              requestAnimationFrame(() => {
                const maps = (window as unknown as ProbeWindow).__maps!;
                const m = maps[maps.length - 1]!;
                const c = m.getCenter().lng;
                const p = m.latLngToContainerPoint([lat, lon + 360 * Math.round((c - lon) / 360)]);
                const box = m.getContainer().getBoundingClientRect();
                const r = document.querySelector("path.spot-pipeline")!.getBoundingClientRect();
                resolve(Math.hypot(r.x + r.width / 2 - box.left - p.x, r.y + r.height / 2 - box.top - p.y));
              }),
            ),
          ),
        { lat: pipeline.latitude, lon: pipeline.longitude },
      );
      worst = Math.max(worst, off);
    }
    await page.mouse.up();
    test.info().annotations.push({ type: "Pipeline marker offset during a 25-frame drag", description: `max ${worst.toFixed(2)} px` });
    expect(worst, "largest marker offset from its projected position (px)").toBeLessThanOrEqual(2);
    await mapSettled(page);
    await shot(page, "nav-1440x900-after-dateline-drag");
  });

  test("two long inertial drags west keep all 50 markers on screen once they settle", async ({ page }) => {
    await openMap(page);
    const spots = await spotList(page);
    for (let i = 0; i < 2; i++) {
      // Few steps make a fast flick, so Leaflet continues the pan with inertia.
      await mouseDrag(page, await mapPoint(page, 0.75, 0.5), -900, 0, 4);
      const c = await markerCoverage(page, spots);
      expect(c.missing, `after inertial drag ${i + 1}`).toEqual([]);
      expect(c.onScreen).toBe(50);
    }
    const v = await liveView(page);
    // How far the view travelled, from the recorded (unwrapped) centres: two 900 px flicks at
    // the one-world zoom are about 450 degrees plus inertia, so the view can pass the +-540
    // re-centre and land anywhere once wrapped; the end position alone cannot tell.
    const travelled = Math.max(...(await mapMoves(page)).map((m) => Math.abs(m.lng - 10)));
    expect(travelled, "the drags moved the map").toBeGreaterThan(90);
    expect(Math.abs(v.centre.lng), "re-centred within +-540 degrees").toBeLessThanOrEqual(540);
    expectBounded(v, "after the inertial drags");
    await shot(page, "nav-1440x900-after-inertial-drags");
  });

  test("the user's position marker is drawn in its nearest world copy too", async ({ page, context, baseURL }) => {
    // A position with no spot within 1.5 degrees of its longitude (the central United States),
    // so no spot changes copy while the view crosses the position's antimeridian.
    const me = { latitude: 39, longitude: -100 };
    await context.grantPermissions(["geolocation"], { origin: baseURL });
    await context.setGeolocation(me);
    await openMap(page);
    const spots = await spotList(page);
    expect(spots.filter((s) => Math.abs(((((s.longitude - me.longitude) % 360) + 540) % 360) - 180) < 1.5)).toEqual([]);
    await page.getByRole("button", { name: "Show my location" }).click();
    await expect(page.locator("path.user-location-marker")).toBeVisible();
    await mapSettled(page);
    const zoom = (await liveView(page)).minZoom;
    // Just before and just after the position's antimeridian, both ways: the marker sits where
    // Leaflet projects its copy nearest the view centre (at the screen edge, one world wide).
    for (const centre of [me.longitude + 179, me.longitude + 181, me.longitude - 179, me.longitude - 181]) {
      await setMapView(page, 20, centre, zoom);
      await mapSettled(page);
      const off = await page.evaluate(
        ({ lat, lon }) => {
          const maps = (window as unknown as ProbeWindow).__maps!;
          const m = maps[maps.length - 1]!;
          const c = m.getCenter().lng;
          const p = m.latLngToContainerPoint([lat, lon + 360 * Math.round((c - lon) / 360)]);
          const box = m.getContainer().getBoundingClientRect();
          const r = document.querySelector("path.user-location-marker")!.getBoundingClientRect();
          return Math.hypot(r.x + r.width / 2 - box.left - p.x, r.y + r.height / 2 - box.top - p.y);
        },
        { lat: me.latitude, lon: me.longitude },
      );
      expect(off, `centre ${centre}: marker offset from its nearest copy (px)`).toBeLessThanOrEqual(2);
    }
  });

  test("endless panning east re-centres silently on the same meridian", async ({ page }) => {
    await openMap(page);
    const spots = await spotList(page);
    await setMapView(page, 15, 530, 3);
    await mapSettled(page);
    await mouseDrag(page, await mapPoint(page, 0.7, 0.5), -500, 0, 25);
    const v = await liveView(page);
    // 530 degrees plus the drag is past 540: the view is back in [-180, 180).
    expect(v.centre.lng).toBeGreaterThanOrEqual(-180);
    expect(v.centre.lng).toBeLessThan(180);
    const c = await markerCoverage(page, spots);
    expect(c.missing).toEqual([]);
    expect(c.inView).toBeGreaterThan(20);
  });
});

test.describe("map navigation · regressions (1440x900)", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a filter round trip that hides and re-shows the selected spot does not move the map", async ({ page }) => {
    await openMap(page);
    await page.locator("path.spot-nazare").dispatchEvent("click");
    await expect(page.getByTestId("panel-spot-name")).toHaveText(/Nazar/);
    await mapSettled(page);
    await setMapView(page, -30, 140, 4);
    await mapSettled(page);
    const before = await liveView(page);
    await page.getByRole("button", { name: "Close spot details" }).click();
    await page.getByRole("button", { name: "Settings" }).click();
    const settings = page.getByTestId("map-settings");
    await settings.getByRole("button", { name: "Australia & New Zealand", exact: true }).click();
    await expect(page.getByTestId("visible-count")).not.toHaveText(/Showing 50 of 50/);
    await settings.getByRole("button", { name: "All", exact: true }).click();
    await expect(page.getByTestId("visible-count")).toHaveText(/Showing 50 of 50/);
    await page.keyboard.press("Escape");
    await mapSettled(page);
    const after = await liveView(page);
    expect(after.centre.lat).toBeCloseTo(before.centre.lat, 6);
    expect(after.centre.lng).toBeCloseTo(before.centre.lng, 6);
    expect(after.zoom).toBe(before.zoom);
  });

  test("the selected spot stays put when a filter hides and re-shows it while it is open", async ({ page }) => {
    await openMap(page);
    await page.locator("path.spot-nazare").dispatchEvent("click");
    await expect(page.getByTestId("panel-spot-name")).toHaveText(/Nazar/);
    await mapSettled(page);
    await setMapView(page, -30, 140, 4);
    await mapSettled(page);
    const before = await liveView(page);
    await resetProbe(page);
    await page.getByRole("button", { name: "Settings" }).click();
    const settings = page.getByTestId("map-settings");
    await settings.getByRole("button", { name: "Hawaii", exact: true }).click();
    await settings.getByRole("button", { name: "All", exact: true }).click();
    await page.keyboard.press("Escape");
    await page.waitForTimeout(1200); // a flight would take 0.8 s
    expect(await mapMoves(page)).toEqual([]);
    const after = await liveView(page);
    expect(after.centre.lng).toBeCloseTo(before.centre.lng, 6);
  });

  test("keyboard: Enter on a marker opens the spot; '-' and the arrow keys stay inside the limits", async ({ page }) => {
    await openMap(page);
    await page.locator("path.spot-pipeline").focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("panel-spot-name")).toHaveText(/Pipeline/);
    await mapSettled(page);
    expect((await liveView(page)).zoom, "the map flew in to the spot").toBeGreaterThanOrEqual(5);
    await page.getByRole("button", { name: "Close spot details" }).click();
    await page.locator(".leaflet-container").focus();
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press("Minus");
      await mapSettled(page, 300);
    }
    const out = await liveView(page);
    expect(out.zoom, "'-' stops at the one-world zoom").toBeCloseTo(out.minZoom, 6);
    let lat = out.centre.lat;
    for (const key of ["ArrowUp", "ArrowDown"]) {
      for (let i = 0; i < 12; i++) await page.keyboard.press(key);
      await mapSettled(page);
      const v = await liveView(page);
      expectBounded(v, `after 12 ${key} presses`);
      expect(key === "ArrowUp" ? v.centre.lat > lat : v.centre.lat < lat, `${key} moved the map`).toBe(true);
      lat = v.centre.lat;
    }
  });

  test("timeline steps re-style only markers whose colour changes; a zoom click touches no marker", async ({ page }) => {
    await openMap(page);
    const snap0 = (await apiGet(page.request, "/api/map/conditions")) as Snapshot;
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await mapSettled(page);
    await resetProbe(page);
    // The colour classes the API gives each spot over the next five steps.
    const label = (s: Snapshot["spots"][number]) => s.conditions?.label ?? "none";
    let prev = new Map(snap0.spots.map((s) => [s.slug, label(s)]));
    let changed = 0;
    await slider.focus();
    for (let i = 1; i <= 5; i++) {
      const at = new Date(Date.parse(snap0.valid_time!) + i * snap0.step_hours * 3_600_000).toISOString();
      const req = page.waitForResponse((r) => r.url().includes("/api/map/conditions?at=") && Date.parse(new URL(r.url()).searchParams.get("at")!) === Date.parse(at));
      await page.keyboard.press("ArrowRight");
      const snap = (await (await req).json()) as Snapshot;
      const next = new Map(snap.spots.map((s) => [s.slug, label(s)]));
      for (const [slug, l] of next) if (prev.get(slug) !== l) changed += 1;
      prev = next;
      await page.waitForTimeout(300);
    }
    const steps = await markerCalls(page);
    expect(steps.setLatLng, "setLatLng over 5 timeline steps").toBe(0);
    expect(steps.on + steps.off, "event handler re-binds").toBe(0);
    expect(steps.setStyle, `setStyle calls (${changed} colour changes)`).toBeLessThanOrEqual(changed);
    test.info().annotations.push({ type: "marker updates over 5 timeline steps", description: JSON.stringify({ ...steps, colourChanges: changed }) });

    await resetProbe(page);
    await page.locator(".leaflet-control-zoom-in").click();
    await mapSettled(page);
    expect(await markerCalls(page)).toEqual({ setLatLng: 0, setStyle: 0, setRadius: 0, on: 0, off: 0 });
  });
});

test.describe("map freshness (synthetic fields)", () => {
  test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "America/Los_Angeles" });

  test("the timeline names the model run and lead time; the run's own time reads 'analysis'", async ({ page }) => {
    const { mock, nowIndex } = await openMapWithFields(page);
    const cycle = new Date(mock.validTimes[0]!);
    const run = `${SYNTHETIC_SOURCE.name} ${String(cycle.getUTCHours()).padStart(2, "0")}Z ${cycle.toLocaleString("en-US", { month: "short", timeZone: "UTC" })} ${cycle.getUTCDate()}`;
    const lead = page.getByTestId("map-lead-time");
    const hours = Math.round((Date.parse(mock.validTimes[nowIndex]!) - cycle.getTime()) / 3_600_000);
    await expect(lead).toHaveText(`Model run ${run} · +${hours} h`);
    await expect(page.getByTestId("map-stale")).toHaveCount(0);
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await slider.focus();
    await page.keyboard.press("Home");
    await expect(lead).toHaveText(`Model run ${run} · analysis`);
    await page.keyboard.press("ArrowRight");
    await expect(lead).toHaveText(`Model run ${run} · +3 h`);
    await shot(page, "freshness-1440x900-lead-time");
  });

  test("an out-of-date model run shows a warning chip, and the layers still draw", async ({ page }) => {
    const validTimes = syntheticValidTimes();
    await mockFields(page, { validTimes });
    // SYNTHETIC: the same index, reported 54 h old (stale after 12 h). Registered later, so it
    // answers before the mock's own index route.
    await page.route(/\/api\/map\/fields(\?.*)?$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          available: true,
          message: null,
          source: SYNTHETIC_SOURCE,
          run_key: "synthetic-run",
          cycle_time: validTimes[0],
          valid_times: validTimes,
          variables: [
            { name: "hs", label: "Significant wave height", unit: "m", display_min: 0, display_max: 10, group: "core", system: null },
            { name: "tp", label: "Peak period", unit: "s", display_min: 0, display_max: 20, group: "core", system: null },
            { name: "wave_dir", label: "Mean wave direction (from)", unit: "deg", display_min: 0, display_max: 360, group: "core", system: null },
            { name: "wind_u", label: "Wind u", unit: "m/s", display_min: -30, display_max: 30, group: "core", system: null },
            { name: "wind_v", label: "Wind v", unit: "m/s", display_min: -30, display_max: 30, group: "core", system: null },
            { name: "pressure", label: "Sea-level pressure", unit: "hPa", display_min: 960, display_max: 1050, group: "core", system: null },
          ],
          updated_at: validTimes[0],
          age_hours: 54,
          stale_after_hours: 12,
          stale: true,
        }),
      }),
    );
    await openMap(page);
    await expect(page.getByTestId("map-stale")).toHaveText("Forecast out of date — model run 54 h old");
    await expect.poll(async () => Number((await page.locator("canvas.map-field-canvas").getAttribute("data-painted")) ?? 0)).toBeGreaterThan(10_000);
    await expect(page.getByTestId("map-lead-time")).toBeVisible();
    await shot(page, "freshness-1440x900-stale");
  });
});

for (const viewport of [
  { width: 390, height: 844 },
  { width: 844, height: 390 },
]) {
  test.describe(`map navigation · phone ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport, hasTouch: true });

    test("touch drags and pinches stay inside the band; the minimum zoom follows rotation", { tag: "@mobile" }, async ({ page }) => {
      const { errors } = await openMap(page);
      await mapSettled(page);
      const v0 = await liveView(page);
      expect(v0.zoom).toBeCloseTo(Math.max(2, minZoomFor(v0.size.x, v0.size.y)), 6);
      expectBounded(v0, "initial view");
      await shot(page, `nav-phone-${viewport.width}x${viewport.height}-initial`);
      // Gestures start on open map, between the top controls and the bottom stack.
      const strip = await freeStrip(page);
      const mid = { x: strip.x, y: (strip.top + strip.bottom) / 2 };
      const reach = Math.round(Math.max(120, (strip.bottom - strip.top) * 0.9));
      for (let i = 0; i < 3; i++) {
        await touchDrag(page, { x: mid.x, y: strip.top + 4 }, 0, reach);
        expectBounded(await liveView(page), `after touch drag north ${i + 1}`);
      }
      const north = await liveView(page);
      expect(north.centre.lat, "the drags moved the map north").toBeGreaterThan(v0.centre.lat + 10);
      await shot(page, `nav-phone-${viewport.width}x${viewport.height}-dragged-north`);
      for (let i = 0; i < 4; i++) {
        await touchDrag(page, { x: mid.x, y: strip.bottom - 4 }, 0, -reach);
        expectBounded(await liveView(page), `after touch drag south ${i + 1}`);
      }
      expect((await liveView(page)).centre.lat, "the drags moved the map south").toBeLessThan(north.centre.lat - 10);
      await shot(page, `nav-phone-${viewport.width}x${viewport.height}-dragged-south`);
      // Two pinch-outs (fingers together) past the minimum zoom.
      const spread = Math.min(140, (strip.bottom - strip.top) / 2 - 4, strip.x - 8);
      const zoomBefore = (await liveView(page)).zoom;
      for (let i = 0; i < 2; i++) {
        await pinch(page, mid.x, mid.y, spread, 10);
        expectBounded(await liveView(page), `after pinch-out ${i + 1}`);
      }
      expect((await liveView(page)).zoom).toBeLessThanOrEqual(zoomBefore);
      // A pinch-in zooms in; then a rotation changes the minimum zoom with the container.
      await pinch(page, mid.x, mid.y, 10, spread);
      const zoomedIn = await liveView(page);
      expectBounded(zoomedIn, "after pinch-in");
      expect(zoomedIn.zoom, "the pinch-in zoomed in").toBeGreaterThan(zoomedIn.minZoom + 0.3);
      await page.setViewportSize({ width: viewport.height, height: viewport.width });
      await expect.poll(async () => {
        const v = await liveView(page);
        return Math.abs(v.minZoom - minZoomFor(v.size.x, v.size.y));
      }).toBeLessThan(1e-6);
      await mapSettled(page);
      expectBounded(await liveView(page), "after rotation");
      await page.setViewportSize(viewport);
      await expect.poll(async () => {
        const v = await liveView(page);
        return Math.abs(v.minZoom - minZoomFor(v.size.x, v.size.y));
      }).toBeLessThan(1e-6);
      await mapSettled(page);
      expectBounded(await liveView(page), "after rotating back");
      await shot(page, `nav-phone-${viewport.width}x${viewport.height}-after-rotation`);
      expect(errors).toEqual([]);
    });
  });
}

for (const viewport of [
  { width: 390, height: 844 },
  { width: 844, height: 390 },
]) {
  test.describe(`map navigation · phone layout ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport, hasTouch: true });

    test("a selected spot lands clear of the open sheet and below the tabs; controls never overlap", { tag: "@mobile" }, async ({ page }) => {
      await openMapWithFields(page);
      const overlaps = (a: { x: number; y: number; width: number; height: number }, b: typeof a) =>
        a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
      const controls = {
        tabs: page.getByTestId("layer-tabs"),
        settings: page.getByRole("button", { name: "Settings" }),
        zoom: page.locator(".leaflet-control-zoom"),
        attribution: page.locator(".leaflet-control-attribution"),
        locate: page.getByRole("button", { name: "Show my location" }),
        bestSpots: page.getByRole("button", { name: /Best spots now/ }),
        timeline: page.getByTestId("time-control"),
      };
      const found = await Promise.all(Object.entries(controls).map(async ([k, l]) => [k, await l.boundingBox()] as const));
      // Phones in landscape leave out the zoom buttons (pinch zooms; they would sit under Settings).
      const short = viewport.height < 480;
      for (const [k, b] of found) expect(b === null, `${k} ${b ? "shown" : "hidden"}`).toBe(short && k === "zoom");
      const boxes = Object.fromEntries(found.filter((e): e is readonly [string, NonNullable<(typeof e)[1]>] => e[1] !== null));
      const names = Object.keys(boxes);
      for (const [i, a] of names.entries()) {
        for (const b of names.slice(i + 1)) expect(overlaps(boxes[a]!, boxes[b]!), `${a} overlaps ${b}`).toBe(false);
      }
      // The model run and lead time sit beside the time bubble, inside the timeline's box.
      const run = (await page.getByTestId("map-lead-time").boundingBox())!;
      const tl = boxes.timeline!;
      expect(run.y).toBeGreaterThanOrEqual(tl.y);
      expect(run.x).toBeGreaterThanOrEqual(tl.x);
      expect(run.x + run.width).toBeLessThanOrEqual(tl.x + tl.width);
      await shot(page, `nav-phone-${viewport.width}x${viewport.height}-controls`);

      const tabsBottom = boxes.tabs!.y + boxes.tabs!.height;
      for (const slug of ["pipeline", "cloudbreak", "nazare"]) {
        await page.locator(`path.spot-${slug}`).dispatchEvent("click");
        const panel = page.getByTestId("map-spot-panel");
        await expect(panel).toBeVisible();
        // The selection flies the map to the spot (0.8 s) once the sheet has opened, which can
        // start after a quiet moment: wait until the marker has landed clear of the sheet and
        // below the tabs, then for the map to stop, and check everything below on the settled
        // map (the flight itself can still be under way when the sheet first shows).
        await expect
          .poll(
            async () => {
              const box = await panel.boundingBox();
              const c = await markerCentre(page, slug);
              return !!box && c.y > tabsBottom && (short ? c.x < box.x : c.y < box.y);
            },
            { message: `${slug} lands clear of the sheet`, timeout: 10_000 },
          )
          .toBe(true);
        await mapSettled(page);
        const sheet = (await panel.boundingBox())!;
        const m = await markerCentre(page, slug);
        expect(m.y, `${slug} below the tabs`).toBeGreaterThan(tabsBottom);
        expect(m.x).toBeGreaterThan(0);
        if (short) {
          // Landscape: a side sheet below Settings; the marker, and the whole timeline, sit left of it.
          const timeline = (await controls.timeline.boundingBox())!;
          expect(sheet.y, "the side sheet starts below Settings").toBeGreaterThanOrEqual(boxes.settings!.y + boxes.settings!.height);
          const map = (await page.locator(".leaflet-container").boundingBox())!;
          expect(sheet.height, `the side sheet is most of the map's height (${map.height})`).toBeGreaterThan(map.height * 0.7);
          expect(timeline.x + timeline.width, "the timeline ends left of the side sheet").toBeLessThanOrEqual(sheet.x);
          expect(m.x, `${slug} left of the sheet (left ${sheet.x})`).toBeLessThan(sheet.x);
          expect(m.y, `${slug} above the timeline (top ${timeline.y})`).toBeLessThan(timeline.y);
        } else {
          expect(m.y, `${slug} above the sheet (top ${sheet.y})`).toBeLessThan(sheet.y);
          expect(m.x).toBeLessThan(viewport.width);
        }
        await shot(page, `nav-phone-${viewport.width}x${viewport.height}-selected-${slug}`);
      }
    });
  });
}

test.describe("spot page mini map", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("zooming out and dragging north leaves no rows outside the band", async ({ page }) => {
    await installMapProbe(page);
    await stubTiles(page);
    await page.goto("/spots/pipeline");
    // The forecast timeline above the mini map replaces a shorter placeholder when it loads:
    // wait for it, so the map does not move down under the mouse after it was measured.
    await expect(page.getByTestId("timeline-scroller")).toBeVisible();
    const map = page.getByRole("region", { name: "Map of monitored surf spots" });
    await map.scrollIntoViewIfNeeded();
    await expect(map.locator("path.spot-pipeline")).toHaveCount(1);
    await expect.poll(() => page.evaluate(() => (window as unknown as ProbeWindow).__maps?.length ?? 0)).toBeGreaterThan(0);
    const box = (await map.boundingBox())!;
    const v0 = await liveView(page);
    expect(v0.options.worldCopyJump).toBe(false);
    expect(v0.options.maxBounds).toBe(true);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    for (let i = 0; i < 10; i++) {
      await page.mouse.wheel(0, 500);
      await mapSettled(page, 300);
    }
    const out = await liveView(page);
    expect(out.zoom).toBeCloseTo(out.minZoom, 6);
    expectBounded(out, "mini map zoomed out");
    for (let i = 0; i < 3; i++) {
      await mouseDrag(page, { x: box.x + box.width / 2, y: box.y + box.height * 0.2 }, 0, box.height * 0.7);
      expectBounded(await liveView(page), `mini map after drag north ${i + 1}`);
    }
    if (process.env.MAP_SCREENSHOT_DIR) await map.screenshot({ path: `${process.env.MAP_SCREENSHOT_DIR}/nav-spot-mini-map.png` });
  });
});
