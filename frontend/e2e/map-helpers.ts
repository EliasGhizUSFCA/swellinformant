import { expect, type Page } from "@playwright/test";

import { apiGet } from "./helpers";
import { mockFields, type FieldMock } from "./map-fields.mock";

/**
 * Shared helpers of the map specs (map.spec.ts, map-layers.spec.ts): opening the map with
 * stubbed tiles, with or without route-mocked SYNTHETIC ocean fields, and reading the map's
 * canvases and geometry. Tile servers are not reachable from CI sandboxes, so by default tile
 * requests are answered with a local PNG; E2E_REAL_TILES=1 loads the real OpenStreetMap tiles
 * (requires network). MAP_SCREENSHOT_DIR saves screenshots of the main states.
 */
export const REAL_TILES = process.env.E2E_REAL_TILES === "1";
const SHOTS = process.env.MAP_SCREENSHOT_DIR;
const TILE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

export interface Snapshot {
  valid_time: string | null;
  step_hours: number;
  spots: Array<{
    slug: string;
    event_at_time: boolean;
    conditions: null | { score: number; is_daylight: boolean; breaking_height_min_ft: number; breaking_height_max_ft: number };
  }>;
}
export interface Spot {
  slug: string;
  name: string;
  country: string;
  region_group: string;
  break_type: string;
}

export async function openMap(page: Page) {
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

/** Opens the map with synthetic ocean fields; `now` is the valid time the map starts on. */
export async function openMapWithFields(page: Page, opts?: Parameters<typeof mockFields>[1]) {
  const mock = await mockFields(page, opts);
  const opened = await openMap(page);
  const snap = (await apiGet(page.request, "/api/map/conditions")) as Snapshot;
  const nowIndex = nearestIndex(mock.validTimes, snap.valid_time!);
  await expect.poll(() => painted(page), { message: "ocean pixels painted" }).toBeGreaterThan(10_000);
  return { ...opened, mock, nowIndex, now: mock.validTimes[nowIndex]! };
}

export const nearestIndex = (times: string[], iso: string) => {
  const t = Date.parse(iso);
  let best = 0;
  times.forEach((v, i) => {
    if (Math.abs(Date.parse(v) - t) < Math.abs(Date.parse(times[best]!) - t)) best = i;
  });
  return best;
};

export const field = (page: Page) => page.locator("canvas.map-field-canvas");
export const painted = async (page: Page) => Number((await field(page).getAttribute("data-painted")) ?? 0);
export const requested = (mock: FieldMock, variable: string, at: string) => mock.requests.includes(`${variable}@${at}`);

/** Screen position of (lat, lon) in the map's initial view (centre 15°N 10°E, zoom 2). */
export async function screenPoint(page: Page, lat: number, lon: number) {
  const box = (await page.locator(".leaflet-container").boundingBox())!;
  const mx = (x: number) => (x + 180) / 360;
  const my = (y: number) => 0.5 - Math.log(Math.tan(Math.PI / 4 + (y * Math.PI) / 360)) / (2 * Math.PI);
  return { x: box.x + box.width / 2 + (mx(lon) - mx(10)) * 1024, y: box.y + box.height / 2 + (my(lat) - my(15)) * 1024 };
}

/** Screenshot for review (MAP_SCREENSHOT_DIR only), after `settleMs` for a map flight to end. */
export async function shot(page: Page, name: string, settleMs = 0) {
  if (!SHOTS) return;
  if (settleMs) await page.waitForTimeout(settleMs);
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
}
