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
    conditions: null | { score: number; label: string; valid_time: string; is_daylight: boolean; breaking_height_min_ft: number; breaking_height_max_ft: number };
  }>;
}
export interface Spot {
  slug: string;
  name: string;
  country: string;
  region_group: string;
  break_type: string;
}

/** Answers tile requests with a local PNG (unless E2E_REAL_TILES=1); returns the tile URLs loaded. */
export async function stubTiles(page: Page): Promise<string[]> {
  const tiles: string[] = [];
  page.on("requestfinished", (r) => {
    if (/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/.test(r.url())) tiles.push(r.url());
  });
  if (!REAL_TILES) {
    await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (route) =>
      route.fulfill({ status: 200, contentType: "image/png", body: TILE_PNG }),
    );
  }
  return tiles;
}

export async function openMap(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await installMapProbe(page);
  const tiles = await stubTiles(page);
  await page.goto("/map");
  await expect(page.getByLabel("Map of monitored surf spots")).toBeVisible();
  await expect(page.locator("path.spot-marker")).toHaveCount(50);
  await expect.poll(() => page.evaluate(() => (window as unknown as ProbeWindow).__maps?.length ?? 0), { message: "Leaflet map created" }).toBeGreaterThan(0);
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

/**
 * Page position of (lat, lon) in the map's current view (live zoom and centre), in the world
 * copy nearest the view centre, where the map draws markers.
 */
export async function screenPoint(page: Page, lat: number, lon: number) {
  return page.evaluate(
    ([la, lo]) => {
      const maps = (window as unknown as ProbeWindow).__maps!;
      const m = maps[maps.length - 1]!;
      const c = m.getCenter().lng;
      const p = m.latLngToContainerPoint([la, lo + 360 * Math.round((c - lo) / 360)]);
      const r = m.getContainer().getBoundingClientRect();
      return { x: r.left + p.x, y: r.top + p.y };
    },
    [lat, lon] as const,
  );
}

// --- the live Leaflet map ----------------------------------------------------------------

/** The slice of Leaflet's API the probe reads (Leaflet itself is bundled into the page). */
export interface ProbeMap {
  getZoom(): number;
  getMinZoom(): number;
  getMaxZoom(): number;
  getCenter(): { lat: number; lng: number };
  getSize(): { x: number; y: number };
  getContainer(): HTMLElement;
  latLngToContainerPoint(ll: [number, number]): { x: number; y: number };
  setView(c: [number, number], z: number, o?: { animate?: boolean }): ProbeMap;
  on(type: string, fn: () => void): ProbeMap;
  options: { worldCopyJump?: boolean; maxBoundsViscosity?: number; bounceAtZoomLimits?: boolean; maxBounds?: unknown };
}
export interface MarkerCalls {
  setLatLng: number;
  setStyle: number;
  setRadius: number;
  on: number;
  off: number;
}
export interface ProbeWindow {
  __maps?: ProbeMap[];
  __markerCalls?: MarkerCalls;
  __moves?: Array<{ lng: number; lat: number; zoom: number }>;
}

/**
 * Before the page loads: keep every Leaflet map the page creates (window.__maps) and count the
 * calls react-leaflet makes on circle markers (window.__markerCalls). Leaflet assigns
 * window.L when its module runs, which the setter below intercepts.
 */
export async function installMapProbe(page: Page) {
  await page.addInitScript(() => {
    const w = window as unknown as ProbeWindow & { L?: unknown };
    w.__maps = [];
    w.__markerCalls = { setLatLng: 0, setStyle: 0, setRadius: 0, on: 0, off: 0 };
    w.__moves = [];
    let L: unknown;
    Object.defineProperty(window, "L", {
      configurable: true,
      get: () => L,
      set(v: {
        Map: { addInitHook(fn: (this: ProbeMap) => void): void };
        CircleMarker: { prototype: Record<string, (...a: unknown[]) => unknown> };
        Evented: { prototype: Record<string, (...a: unknown[]) => unknown> };
      }) {
        L = v;
        v.Map.addInitHook(function (this: ProbeMap) {
          w.__maps!.push(this);
          this.on("move", () => {
            const c = this.getCenter();
            w.__moves!.push({ lng: c.lng, lat: c.lat, zoom: this.getZoom() });
          });
        });
        const marker = v.CircleMarker.prototype;
        for (const k of ["setLatLng", "setStyle", "setRadius"] as const) {
          const orig = marker[k]!;
          marker[k] = function (this: unknown, ...a: unknown[]) {
            w.__markerCalls![k] += 1;
            return orig.apply(this, a);
          };
        }
        const evented = v.Evented.prototype;
        for (const k of ["on", "off"] as const) {
          const orig = evented[k]!;
          evented[k] = function (this: unknown, ...a: unknown[]) {
            if (this instanceof (v.CircleMarker as unknown as new () => unknown)) w.__markerCalls![k] += 1;
            return orig.apply(this, a);
          };
        }
      },
    });
  });
}

/** Zeroes the marker-call counters and the recorded map moves. */
export const resetProbe = (page: Page) =>
  page.evaluate(() => {
    const w = window as unknown as ProbeWindow;
    w.__markerCalls = { setLatLng: 0, setStyle: 0, setRadius: 0, on: 0, off: 0 };
    w.__moves = [];
  });
export const markerCalls = (page: Page) => page.evaluate(() => ({ ...(window as unknown as ProbeWindow).__markerCalls! }));
export const mapMoves = (page: Page) => page.evaluate(() => [...((window as unknown as ProbeWindow).__moves ?? [])]);

export interface LiveView {
  zoom: number;
  minZoom: number;
  maxZoom: number;
  centre: { lat: number; lng: number };
  size: { x: number; y: number };
  /** Width of one world copy in CSS pixels at the current zoom. */
  worldPx: number;
  /** Map rows past the wave-data band (84°N to 78.5°S), above and below. */
  rowsOutsideBand: number;
  /** Map rows past the edge of the Mercator world (±85.05°), where nothing can be drawn. */
  blankRows: number;
  options: { worldCopyJump: boolean | undefined; maxBoundsViscosity: number | undefined; bounceAtZoomLimits: boolean | undefined; maxBounds: boolean };
}

/** The live view of the page's `which`-th Leaflet map (default: the last one created). */
export async function liveView(page: Page, which = -1): Promise<LiveView> {
  return page.evaluate((i) => {
    const maps = (window as unknown as ProbeWindow).__maps!;
    const m = maps.at(i)!;
    const size = m.getSize();
    const zoom = m.getZoom();
    const c = m.getCenter();
    const outside = (north: number, south: number) => {
      const top = m.latLngToContainerPoint([north, c.lng]).y;
      const bottom = m.latLngToContainerPoint([south, c.lng]).y;
      return Math.max(0, Math.min(size.y, top)) + Math.max(0, size.y - Math.max(0, bottom));
    };
    return {
      zoom,
      minZoom: m.getMinZoom(),
      maxZoom: m.getMaxZoom(),
      centre: { lat: c.lat, lng: c.lng },
      size: { x: size.x, y: size.y },
      worldPx: 256 * 2 ** zoom,
      rowsOutsideBand: outside(84, -78.5),
      blankRows: outside(85.0511287798, -85.0511287798),
      options: {
        worldCopyJump: m.options.worldCopyJump,
        maxBoundsViscosity: m.options.maxBoundsViscosity,
        bounceAtZoomLimits: m.options.bounceAtZoomLimits,
        maxBounds: !!m.options.maxBounds,
      },
    };
  }, which);
}

/** Moves the live map without animation (test set-up, not a user gesture). */
export const setMapView = (page: Page, lat: number, lng: number, zoom: number) =>
  page.evaluate(
    ([la, lo, z]) => {
      const maps = (window as unknown as ProbeWindow).__maps!;
      maps[maps.length - 1]!.setView([la, lo], z, { animate: false });
    },
    [lat, lng, zoom] as const,
  );

/** Resolves once the map has not moved for `quietMs` (flights, inertia and zoom animations end). */
export async function mapSettled(page: Page, quietMs = 400, timeoutMs = 10_000) {
  const start = Date.now();
  let last = -1;
  let since = Date.now();
  while (Date.now() - start < timeoutMs) {
    const n = (await mapMoves(page)).length;
    if (n !== last) {
      last = n;
      since = Date.now();
    } else if (Date.now() - since >= quietMs) return;
    await page.waitForTimeout(50);
  }
  throw new Error("the map kept moving");
}

/**
 * Markers on screen against the spots that should have one there: a spot is in view when its
 * world copy nearest the view centre is inside the map (with the view at most one world wide,
 * no other copy can be).
 */
export async function markerCoverage(page: Page, spots: ReadonlyArray<{ slug: string; latitude: number; longitude: number }>) {
  return page.evaluate((list) => {
    const maps = (window as unknown as ProbeWindow).__maps!;
    const m = maps[maps.length - 1]!;
    const box = m.getContainer().getBoundingClientRect();
    const size = m.getSize();
    const c = m.getCenter().lng;
    const inside = (x: number, y: number) => x >= 0 && x <= size.x && y >= 0 && y <= size.y;
    const missing: string[] = [];
    let inView = 0;
    let onScreen = 0;
    for (const s of list) {
      const p = m.latLngToContainerPoint([s.latitude, s.longitude + 360 * Math.round((c - s.longitude) / 360)]);
      const r = document.querySelector(`path.spot-${s.slug}`)?.getBoundingClientRect();
      const shown = !!r && r.width > 0 && inside(r.x + r.width / 2 - box.left, r.y + r.height / 2 - box.top);
      const visible = inside(p.x, p.y);
      if (visible) inView += 1;
      if (shown) onScreen += 1;
      if (visible && !shown) missing.push(s.slug);
    }
    return { inView, onScreen, missing };
  }, spots);
}

/** Page position of a marker's centre. */
export async function markerCentre(page: Page, slug: string) {
  const b = (await page.locator(`path.spot-${slug}`).boundingBox())!;
  return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
}

// --- gestures ----------------------------------------------------------------------------

/** A mouse drag by (dx, dy) from `from`, then a wait for inertia to end. */
export async function mouseDrag(page: Page, from: { x: number; y: number }, dx: number, dy: number, steps = 12) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(from.x + dx, from.y + dy, { steps });
  await page.mouse.up();
  await mapSettled(page);
}

/** A one-finger touch drag by (dx, dy) through CDP (needs a touch-enabled context). */
export async function touchDrag(page: Page, from: { x: number; y: number }, dx: number, dy: number, steps = 12) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: from.x, y: from.y }] });
  for (let i = 1; i <= steps; i++) {
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: from.x + (dx * i) / steps, y: from.y + (dy * i) / steps }] });
    await page.waitForTimeout(16);
  }
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await cdp.detach();
  await mapSettled(page);
}

/** A two-finger pinch around (cx, cy), the fingers moving from `from` to `to` px off centre. */
export async function pinch(page: Page, cx: number, cy: number, from: number, to: number, steps = 12) {
  const cdp = await page.context().newCDPSession(page);
  const points = (d: number) => [
    { x: cx - d, y: cy, id: 1 },
    { x: cx + d, y: cy, id: 2 },
  ];
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: points(from) });
  for (let i = 1; i <= steps; i++) {
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: points(from + ((to - from) * i) / steps) });
    await page.waitForTimeout(16);
  }
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await cdp.detach();
  await mapSettled(page);
}

/** Screenshot for review (MAP_SCREENSHOT_DIR only), after `settleMs` for a map flight to end. */
export async function shot(page: Page, name: string, settleMs = 0) {
  if (!SHOTS) return;
  if (settleMs) await page.waitForTimeout(settleMs);
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
}
