import { expect, test, type Page } from "@playwright/test";

import { apiGet, apiPost, verifyEmailFromOutbox } from "./helpers";
import { mockFields, mockFieldsUnavailable, syntheticValidTimes, syntheticValue, type FieldMock, type SyntheticFields } from "./map-fields.mock";

/**
 * Interactive map against the production build. Tile servers are not reachable from CI
 * sandboxes, so by default tile requests are answered with a local PNG. Set
 * E2E_REAL_TILES=1 to load the real OpenStreetMap tiles instead (requires network).
 *
 * The ocean layers (colour field, particles, isobars) are tested with route-mocked
 * SYNTHETIC fields from ./map-fields.mock.ts (made-up patterns, not a forecast); the
 * backend in this suite has no live NOAA data, so its own index reports "unavailable".
 * Set MAP_SCREENSHOT_DIR to save screenshots of the main states (synthetic fields).
 */
const REAL_TILES = process.env.E2E_REAL_TILES === "1";
const SHOTS = process.env.MAP_SCREENSHOT_DIR;
const TILE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

interface Snapshot {
  valid_time: string | null;
  step_hours: number;
  spots: Array<{
    slug: string;
    event_at_time: boolean;
    conditions: null | { score: number; is_daylight: boolean; breaking_height_min_ft: number; breaking_height_max_ft: number };
  }>;
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

/** Opens the map with synthetic ocean fields; `now` is the valid time the map starts on. */
async function openMapWithFields(page: Page, opts?: Parameters<typeof mockFields>[1]) {
  const mock = await mockFields(page, opts);
  const opened = await openMap(page);
  const snap = (await apiGet(page.request, "/api/map/conditions")) as Snapshot;
  const nowIndex = nearestIndex(mock.validTimes, snap.valid_time!);
  await expect.poll(() => painted(page), { message: "ocean pixels painted" }).toBeGreaterThan(10_000);
  return { ...opened, mock, nowIndex, now: mock.validTimes[nowIndex]! };
}

const nearestIndex = (times: string[], iso: string) => {
  const t = Date.parse(iso);
  let best = 0;
  times.forEach((v, i) => {
    if (Math.abs(Date.parse(v) - t) < Math.abs(Date.parse(times[best]!) - t)) best = i;
  });
  return best;
};

const visibleCount = async (page: Page) => Number(/Showing (\d+) of/.exec(await page.getByTestId("visible-count").innerText())![1]);
const field = (page: Page) => page.locator("canvas.map-field-canvas");
const painted = async (page: Page) => Number((await field(page).getAttribute("data-painted")) ?? 0);
const requested = (mock: FieldMock, variable: string, at: string) => mock.requests.includes(`${variable}@${at}`);

/** Screen position of (lat, lon) in the map's initial view (centre 15°N 10°E, zoom 2). */
async function screenPoint(page: Page, lat: number, lon: number) {
  const box = (await page.locator(".leaflet-container").boundingBox())!;
  const mx = (x: number) => (x + 180) / 360;
  const my = (y: number) => 0.5 - Math.log(Math.tan(Math.PI / 4 + (y * Math.PI) / 360)) / (2 * Math.PI);
  return { x: box.x + box.width / 2 + (mx(lon) - mx(10)) * 1024, y: box.y + box.height / 2 + (my(lat) - my(15)) * 1024 };
}

/** RGB of every page pixel in a (2r+1)² square around a screen point. */
async function pixels(page: Page, x: number, y: number, r = 4): Promise<Array<[number, number, number]>> {
  const png = await page.screenshot({ clip: { x: Math.round(x) - r, y: Math.round(y) - r, width: 2 * r + 1, height: 2 * r + 1 } });
  return page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const ctx = c.getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    const out: Array<[number, number, number]> = [];
    for (let i = 0; i < d.length; i += 4) out.push([d[i]!, d[i + 1]!, d[i + 2]!]);
    return out;
  }, png.toString("base64"));
}

const median = (xs: number[]) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)]!;
const saturation = ([r, g, b]: [number, number, number]) => Math.max(r, g, b) - Math.min(r, g, b);

/** "8am Sun PDT", the way the timeline bubble writes a time in `tz`. */
function bubble(iso: string, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", hour: "numeric", minute: "2-digit", hour12: true, timeZoneName: "short" }).formatToParts(new Date(iso));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const minute = get("minute") !== "00" ? `:${get("minute")}` : "";
  return `${get("hour")}${minute}${get("dayPeriod").toLowerCase()} ${get("weekday")} ${get("timeZoneName")}`;
}

/** Screenshot for review (MAP_SCREENSHOT_DIR only), after `settleMs` for a map flight to end. */
async function shot(page: Page, name: string, settleMs = 0) {
  if (!SHOTS) return;
  if (settleMs) await page.waitForTimeout(settleMs);
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
}

const lonGap = (a: number, b: number) => {
  const d = Math.abs(a - b) % 360;
  return Math.min(d, 360 - d);
};

/** RGBA of the colour-field canvas itself (below the land and particles) at a page point. */
async function fieldPixel(page: Page, x: number, y: number): Promise<number[]> {
  return field(page).evaluate(
    (c: HTMLCanvasElement, [px, py]) => {
      const r = c.getBoundingClientRect();
      const i = Math.floor(((px - r.left) * c.width) / r.width);
      const j = Math.floor(((py - r.top) * c.height) / r.height);
      return Array.from(c.getContext("2d")!.getImageData(i, j, 1, 1).data);
    },
    [x, y] as const,
  );
}

/**
 * Page-coordinate extent of the "hot" field pixels (red > 150) within ±r CSS px of a point.
 * (An extent, not a centroid: Mercator stretches the northern half of a square more than
 * the southern half, so a pixel centroid sits a few pixels poleward of its centre.)
 */
async function hotBox(page: Page, x: number, y: number, r: number) {
  return field(page).evaluate(
    (c: HTMLCanvasElement, [px, py, rr]) => {
      const b = c.getBoundingClientRect();
      const sx = c.width / b.width;
      const sy = c.height / b.height;
      const i0 = Math.max(0, Math.floor((px - rr - b.left) * sx));
      const j0 = Math.max(0, Math.floor((py - rr - b.top) * sy));
      const w = Math.min(c.width, Math.ceil((px + rr - b.left) * sx)) - i0;
      const h = Math.min(c.height, Math.ceil((py + rr - b.top) * sy)) - j0;
      const d = c.getContext("2d")!.getImageData(i0, j0, w, h).data;
      let n = 0;
      let minI = Infinity;
      let maxI = -Infinity;
      let minJ = Infinity;
      let maxJ = -Infinity;
      for (let j = 0; j < h; j++) {
        for (let i = 0; i < w; i++) {
          const k = (j * w + i) * 4;
          if (d[k + 3]! > 0 && d[k]! > 150) {
            n++;
            minI = Math.min(minI, i);
            maxI = Math.max(maxI, i);
            minJ = Math.min(minJ, j);
            maxJ = Math.max(maxJ, j);
          }
        }
      }
      if (!n) return null;
      return {
        n,
        left: b.left + (i0 + minI) / sx,
        right: b.left + (i0 + maxI + 1) / sx,
        top: b.top + (j0 + minJ) / sy,
        bottom: b.top + (j0 + maxJ + 1) / sy,
      };
    },
    [x, y, r] as const,
  );
}

/** Latitude at page row `y`, given that row `y0` is at `lat0`, at Leaflet zoom `zoom`. */
function latAtRow(y: number, y0: number, lat0: number, zoom: number): number {
  const my0 = 0.5 - Math.log(Math.tan(Math.PI / 4 + (lat0 * Math.PI) / 360)) / (2 * Math.PI);
  const my = my0 + (y - y0) / (256 * 2 ** zoom);
  return (Math.atan(Math.sinh(Math.PI * (1 - 2 * my))) * 180) / Math.PI;
}

async function centreOf(page: Page, selector: string) {
  const b = (await page.locator(selector).boundingBox())!;
  return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
}

/**
 * Direction the particle trails point: the alpha gradient summed over the particle canvas.
 * A trail is brightest at its head, so along one trail the sum is (head − tail) alpha in the
 * direction of travel (x east, y down).
 */
async function trailGradient(page: Page) {
  return page.locator("canvas.map-particle-canvas").evaluate((c: HTMLCanvasElement) => {
    const { width: w, height: h } = c;
    const d = c.getContext("2d")!.getImageData(0, 0, w, h).data;
    const a = (x: number, y: number) => d[(y * w + x) * 4 + 3]!;
    let gx = 0;
    let gy = 0;
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        if (!a(x, y)) continue;
        gx += a(x + 1, y) - a(x - 1, y);
        gy += a(x, y + 1) - a(x, y - 1);
      }
    }
    return { gx, gy };
  });
}

/**
 * Static arrows (reduced motion) sit on a 46 px grid with the head barbs at the downstream
 * end, so their ink leans from each cell's centre towards where the flow goes. Also a hash of
 * the pixels, to tell whether the canvas changes.
 */
async function arrowLean(page: Page) {
  return page.locator("canvas.map-particle-canvas").evaluate((c: HTMLCanvasElement) => {
    const { width: w, height: h } = c;
    const d = c.getContext("2d")!.getImageData(0, 0, w, h).data;
    let sx = 0;
    let sy = 0;
    let ink = 0;
    let hash = 0;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const a = d[(y * w + x) * 4 + 3]!;
        if (!a) continue;
        sx += a * ((x % 46) - 22.5);
        sy += a * ((y % 46) - 22.5);
        ink += a;
        hash = (hash * 31 + a * (x + 1) + y) % 1_000_000_007;
      }
    }
    return { dx: ink ? sx / ink : 0, dy: ink ? sy / ink : 0, ink, hash };
  });
}

// SYNTHETIC uniform flow: waves FROM the west (270°, so they travel east) at 10 s, and wind
// blowing TOWARDS the north (u 0, v 8 m/s, i.e. a southerly).
const UNIFORM_FLOW: SyntheticFields = {
  name: "uniform-flow",
  value: (variable) => (({ hs: 2, tp: 10, wave_dir: 270, wind_u: 0, wind_v: 8, pressure: 1013 }) as Record<string, number>)[variable] ?? null,
};

// SYNTHETIC hot spots: Hs 9 m within ±1° of 60°N 30°W and of 20°S 150°E, 0.5 m elsewhere.
const HOT_SPOTS = [
  { lat: 60, lon: -30 },
  { lat: -20, lon: 150 },
];
const HOT_FIELD: SyntheticFields = {
  name: "hot-spots",
  value: (variable, lat, lon) => {
    if (variable === "hs") return HOT_SPOTS.some((h) => Math.abs(lat - h.lat) <= 1 && lonGap(lon, h.lon) <= 1) ? 9 : 0.5;
    return ({ tp: 10, wave_dir: 270, wind_u: 0, wind_v: 0, pressure: 1013 } as Record<string, number>)[variable] ?? null;
  },
};

test.describe("interactive map", () => {
  test("loads with 50 markers, working tiles and no uncaught errors", async ({ page }) => {
    const { errors, tiles } = await openMap(page);
    // Default: the dark Natural Earth basemap drawn on a canvas (no tile requests).
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "1");
    await expect(page.locator(".leaflet-control-attribution")).toContainText("Natural Earth");
    expect(tiles).toEqual([]);
    // Without live NOAA data the backend's index says so; the map still works.
    await expect(page.getByTestId("fields-notice")).toBeVisible();
    // "Streets" in Settings switches to the OpenStreetMap tiles.
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("radio", { name: "Streets" }).click();
    await page.keyboard.press("Escape");
    await expect.poll(async () => page.locator("img.leaflet-tile-loaded").count()).toBeGreaterThan(0);
    expect(tiles.length).toBeGreaterThan(0);
    expect(tiles.every((u) => u.startsWith("https://tile.openstreetmap.org/"))).toBe(true);
    await expect(page.locator(".leaflet-control-attribution")).toContainText("OpenStreetMap");
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "0");
    await page.getByRole("button", { name: "Legend", exact: true }).click();
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
    await expect(panel.getByTestId("forecast-confidence-slot")).toBeAttached();
    await panel.getByRole("button", { name: "Close spot details" }).click();
    await expect(panel).toHaveCount(0);
  });

  test("the forecast time control requests cached predictions for the chosen hour", async ({ page, request }) => {
    await openMap(page);
    const before = await page.getByTestId("time-label").innerText();
    // Eight steps after the server's "now" hour. The request is debounced (250 ms), so clicks
    // slower than that may also ask for earlier hours: wait for the final hour's request.
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const final = Date.parse(snap.valid_time!) + 8 * snap.step_hours * 3600_000;
    const req = page.waitForRequest((r) => {
      const at = new URL(r.url()).searchParams.get("at");
      return r.url().includes("/api/map/conditions?at=") && at !== null && Date.parse(at) === final;
    });
    for (let i = 0; i < 8; i++) await page.getByRole("button", { name: "Later" }).click();
    const url = new URL((await req).url());
    const at = new Date(url.searchParams.get("at")!);
    expect(at.getTime() - Date.now()).toBeGreaterThan(20 * 3600_000); // ≥ ~1 day ahead
    await expect(page.getByTestId("time-label")).not.toHaveText(before);
    await page.locator("path.spot-nazare").click({ force: true });
    const shown = await page.getByTestId("forecast-breakdown").innerText();
    expect(shown).toMatch(/local/);
    await page.getByRole("button", { name: "Now", exact: true }).click();
    await expect(page.getByTestId("time-label")).toHaveText(before);
  });

  test("filters narrow the visible spots consistently with the data", async ({ page, request }) => {
    await openMap(page);
    const spots = (await apiGet(request, "/api/spots")) as Spot[];
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const bySlug = new Map(snap.spots.map((s) => [s.slug, s]));

    // The filters live in the Settings popover, with the matching count.
    await page.getByRole("button", { name: "Settings" }).click();
    const settings = page.getByTestId("map-settings");
    await expect(settings.getByTestId("filter-count")).toHaveText("50 of 50 spots match");

    await settings.getByRole("button", { name: "Hawaii", exact: true }).click();
    const hawaii = spots.filter((s) => s.region_group === "Hawaii").length;
    await expect.poll(() => visibleCount(page)).toBe(hawaii);
    await expect(page.locator("path.spot-marker")).toHaveCount(hawaii);
    await expect(settings.getByTestId("filter-count")).toHaveText(`${hawaii} of 50 spots match`);

    await settings.getByRole("button", { name: "All", exact: true }).click();
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

  test("the best-spots list ranks daylight hours above night ones and tags the night", async ({ page, request }) => {
    await openMap(page);
    const spots = (await apiGet(request, "/api/spots")) as Spot[];
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const cond = new Map(snap.spots.map((s) => [s.slug, s.conditions]));
    const rank = (slug: string) => {
      const c = cond.get(slug);
      return c ? (c.is_daylight ? 1000 : 0) + c.score : -1;
    };
    // The same ordering as before the redesign: daylight first, then score; 15 rows.
    const expected = [...spots].sort((a, b) => rank(b.slug) - rank(a.slug)).slice(0, 15);
    const rows = page.getByTestId("top-spots").locator("li");
    await expect(rows).toHaveCount(15);
    for (const [i, s] of expected.entries()) {
      await expect(rows.nth(i)).toContainText(`${s.name} · ${s.country}`);
      const c = cond.get(s.slug);
      await expect(rows.nth(i).getByTestId("night-tag")).toHaveCount(c && !c.is_daylight ? 1 : 0);
    }
    // A row opens that spot's panel.
    await rows.first().getByRole("button").click();
    await expect(page.getByTestId("panel-spot-name")).toHaveText(expected[0]!.name);
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

test.describe("ocean layers (synthetic fields)", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("paints the ocean colour field under the land and reports the redraw time", async ({ page }) => {
    const { mock, now, errors } = await openMapWithFields(page);
    for (const v of ["hs", "tp", "wave_dir"]) expect(requested(mock, v, now), `${v}@${now}`).toBe(true);
    await expect(field(page)).toHaveAttribute("data-layer", "hs");
    await expect(field(page)).toHaveAttribute("data-at", now);
    await expect(field(page)).toHaveAttribute("aria-label", /Offshore significant wave height/);
    // The canvas really holds coloured ocean pixels: mostly opaque, many distinct colours.
    const stats = await field(page).evaluate((c: HTMLCanvasElement) => {
      const d = c.getContext("2d")!.getImageData(0, 0, c.width, c.height).data;
      const colours = new Set<number>();
      let opaque = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i + 3] === 255) {
          opaque++;
          colours.add((d[i]! << 16) | (d[i + 1]! << 8) | d[i + 2]!);
        }
      }
      return { opaque: opaque / (d.length / 4), colours: colours.size };
    });
    expect(stats.opaque).toBeGreaterThan(0.5);
    expect(stats.colours).toBeGreaterThan(40);
    // The South Pacific shows the field; inland Western Australia shows the dark land (#2b2b2b)
    // drawn over it (the synthetic field has values everywhere, the land masks them).
    const lp = await screenPoint(page, -27, 121);
    const land = await pixels(page, lp.x, lp.y);
    for (const ch of [0, 1, 2] as const) expect(Math.abs(median(land.map((p) => p[ch])) - 0x2b)).toBeLessThan(10);
    const sp = await screenPoint(page, -50, -120);
    expect(median((await pixels(page, sp.x, sp.y)).map(saturation))).toBeGreaterThan(40); // a saturated colour, not grey
    // Particles run over the ocean.
    await expect(page.locator("canvas.map-particle-canvas")).toHaveAttribute("data-mode", "animated");
    await shot(page, "desktop-1440x900-synthetic-wave-height");

    // Redraw time of the colour field at 1440 × 900 (half-resolution canvas), over 6 pans.
    const times: number[] = [];
    for (let i = 0; i < 6; i++) {
      const draws = await field(page).getAttribute("data-draws");
      await page.mouse.move(800, 300);
      await page.mouse.down();
      await page.mouse.move(800 + (i % 2 ? -90 : 90), 320, { steps: 4 });
      await page.mouse.up();
      await expect(field(page)).not.toHaveAttribute("data-draws", draws!);
      times.push(Number(await field(page).getAttribute("data-draw-ms")));
    }
    const mid = median(times);
    console.log(`colour-field redraw at 1440x900: ${times.map((t) => t.toFixed(1)).join(", ")} ms (median ${mid.toFixed(1)} ms)`);
    test.info().annotations.push({ type: "field redraw ms (1440x900)", description: `${times.join(", ")}; median ${mid}` });
    expect(mid).toBeLessThan(250); // regression guard; the target is < 60 ms (see the log)

    // Markers stay clickable above every layer.
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    await expect(page.getByTestId("panel-spot-name")).toHaveText("Jeffreys Bay (Supertubes)");
    await shot(page, "desktop-1440x900-synthetic-spot-panel", 1500);
    // The open panel leaves Leaflet's zoom buttons uncovered (Playwright refuses covered targets).
    await page.locator(".leaflet-control-zoom-out").click({ timeout: 5_000 });
    expect(errors).toEqual([]);
  });

  test("each layer tab switches the colour layer, the legend and the units", async ({ page }) => {
    const { mock, now } = await openMapWithFields(page);
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    const legend = page.getByTestId("legend-drawer");
    await expect(page.getByRole("radio", { name: "Wave Height" })).toHaveAttribute("aria-checked", "true");
    await expect(legend.getByTestId("legend-title")).toHaveText("Offshore wave height, Hs (ft)");
    await expect(legend.getByTestId("legend-about")).toContainText("Surf height at a beach is different");

    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect(field(page)).toHaveAttribute("data-layer", "wind");
    await expect.poll(() => requested(mock, "wind_u", now) && requested(mock, "wind_v", now)).toBe(true);
    await expect(legend.getByTestId("legend-title")).toHaveText("Wind speed (km/h)");
    await expect(legend.getByTestId("legend-ticks")).toContainText("100"); // 0–30 m/s ≈ 0–108 km/h

    await page.getByRole("radio", { name: "Wave Energy" }).click();
    await expect(field(page)).toHaveAttribute("data-layer", "energy");
    await expect(legend.getByTestId("legend-title")).toHaveText("Offshore wave power (kW/m)");
    await expect(legend.getByTestId("legend-about")).toContainText("0.49 × Hs² × Te");
    await expect(legend.getByTestId("legend-ticks")).toContainText("150");

    // Arrow keys move between the tabs (a radio group). Held briefly like a real key press:
    // Radix checks the radio that receives focus while an arrow key is down.
    await page.getByRole("radio", { name: "Wave Energy" }).focus();
    await page.keyboard.down("ArrowRight");
    await page.waitForTimeout(60);
    await page.keyboard.up("ArrowRight");
    await expect(page.getByRole("radio", { name: "Wave Period" })).toHaveAttribute("aria-checked", "true");
    await expect(field(page)).toHaveAttribute("data-layer", "tp");
    await expect(legend.getByTestId("legend-title")).toHaveText("Offshore peak period (s)");
    await expect(field(page)).toHaveAttribute("aria-label", /Offshore peak wave period/);

    // Units from Settings apply to the legend.
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByTestId("map-settings").getByRole("radio", { name: "kn", exact: true }).click();
    await page.getByTestId("map-settings").getByRole("radio", { name: "m", exact: true }).click();
    await page.keyboard.press("Escape");
    await expect(legend.getByTestId("legend-title")).toHaveText("Wind speed (kn)");
    await page.getByRole("radio", { name: "Wave Height" }).click();
    await expect(legend.getByTestId("legend-title")).toHaveText("Offshore wave height, Hs (m)");
    await expect(legend.getByTestId("legend-ticks")).toContainText("10");
  });

  test("the pressure toggle draws labelled isobars from the pressure field", async ({ page }) => {
    const { mock, now } = await openMapWithFields(page);
    const base = page.locator("canvas.map-basemap-canvas");
    await expect(base).toHaveAttribute("data-isobars", "0");
    const toggle = page.getByRole("button", { name: "Pressure" });
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => requested(mock, "pressure", now)).toBe(true);
    await expect.poll(async () => Number(await base.getAttribute("data-isobars"))).toBeGreaterThan(5);
    await shot(page, "desktop-1440x900-synthetic-pressure");
    await toggle.click();
    await expect(base).toHaveAttribute("data-isobars", "0");
  });

  test.describe("in Los Angeles time", () => {
    test.use({ timezoneId: "America/Los_Angeles" });

    test("the timeline drives the ocean layers and the spot markers", async ({ page }) => {
      const { mock, nowIndex, now } = await openMapWithFields(page);
      const tz = "America/Los_Angeles";
      const slider = page.getByRole("slider", { name: "Forecast time" });
      const label = page.getByTestId("time-label");
      await expect(label).toHaveText(bubble(now, tz));
      await expect(slider).toHaveAttribute("aria-valuenow", String(nowIndex));
      await expect(slider).toHaveAttribute("aria-valuemax", String(mock.validTimes.length - 1));
      // day labels in local time under the ticks
      const weekday = (iso: string) => new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short" }).format(new Date(iso));
      await expect(page.getByTestId("time-control")).toContainText(weekday(mock.validTimes.at(-1)!));
      await shot(page, "desktop-1440x900-synthetic-timeline-los-angeles");

      // Arrow key: one step later → new ?at= for the layer fields and the spot snapshot.
      const next = mock.validTimes[nowIndex + 1]!;
      const snapReq = page.waitForRequest((r) => r.url().includes(`/api/map/conditions?at=${encodeURIComponent(next)}`));
      await slider.focus();
      await page.keyboard.press("ArrowRight");
      await expect(label).toHaveText(bubble(next, tz));
      await snapReq;
      await expect.poll(() => requested(mock, "hs", next)).toBe(true);
      await expect(field(page)).toHaveAttribute("data-at", next);
      // neighbouring steps are prefetched
      await expect.poll(() => requested(mock, "hs", mock.validTimes[nowIndex + 2]!)).toBe(true);

      await page.keyboard.press("End");
      await expect(slider).toHaveAttribute("aria-valuenow", String(mock.validTimes.length - 1));
      await expect(label).toHaveText(bubble(mock.validTimes.at(-1)!, tz));
      await page.keyboard.press("Home");
      await expect(slider).toHaveAttribute("aria-valuenow", "0");

      // Click on the track at ~75 %.
      const box = (await slider.boundingBox())!;
      await page.mouse.click(box.x + box.width * 0.75, box.y + box.height / 2);
      const clicked = Number(await slider.getAttribute("aria-valuenow"));
      expect(Math.abs(clicked - Math.round(0.75 * (mock.validTimes.length - 1)))).toBeLessThanOrEqual(1);
      await expect(label).toHaveText(bubble(mock.validTimes[clicked]!, tz));
      await expect.poll(() => requested(mock, "hs", mock.validTimes[clicked]!)).toBe(true);

      // Drag the thumb back towards the start.
      await page.mouse.move(box.x + box.width * 0.75, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width * 0.25, box.y + box.height / 2, { steps: 6 });
      await page.mouse.up();
      const dragged = Number(await slider.getAttribute("aria-valuenow"));
      expect(Math.abs(dragged - Math.round(0.25 * (mock.validTimes.length - 1)))).toBeLessThanOrEqual(1);

      // "Now" snaps back to the server's current step.
      await page.getByRole("button", { name: "Now", exact: true }).click();
      await expect(slider).toHaveAttribute("aria-valuenow", String(nowIndex));
      await expect(label).toHaveText(bubble(now, tz));
    });
  });

  test("play advances through the forecast and stops at the end", async ({ page }) => {
    // five steps starting at the server's current 3-hour step
    const times = syntheticValidTimes(Date.now(), 7).slice(2);
    const { nowIndex } = await openMapWithFields(page, { validTimes: times });
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await expect(slider).toHaveAttribute("aria-valuenow", String(nowIndex));
    await page.getByRole("button", { name: "Play forecast" }).click();
    await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
    await expect.poll(async () => Number(await slider.getAttribute("aria-valuenow"))).toBeGreaterThan(nowIndex);
    await expect(slider).toHaveAttribute("aria-valuenow", String(times.length - 1), { timeout: 20_000 });
    // stopped at the last step
    await expect(page.getByRole("button", { name: "Play forecast" })).toBeDisabled();
    // pause works mid-way
    await slider.focus();
    await page.keyboard.press("Home");
    await page.getByRole("button", { name: "Play forecast" }).click();
    await expect(slider).toHaveAttribute("aria-valuenow", "1", { timeout: 10_000 });
    await page.getByRole("button", { name: "Pause" }).click();
    await page.waitForTimeout(1500);
    await expect(slider).toHaveAttribute("aria-valuenow", "1");
  });

  test("settings switch units, particles and basemap, persist, and filters still filter", async ({ page, request }) => {
    await openMapWithFields(page);
    await page.getByRole("button", { name: "Settings" }).click();
    const settings = page.getByTestId("map-settings");
    // particles off / on
    const particles = settings.getByRole("switch", { name: /Animated particles/ });
    await expect(particles).toHaveAttribute("aria-checked", "true");
    await particles.click();
    await expect(page.locator("canvas.map-particle-canvas")).toHaveAttribute("data-mode", "off");
    await particles.click();
    await expect(page.locator("canvas.map-particle-canvas")).toHaveAttribute("data-mode", "animated");
    // basemap
    await settings.getByRole("radio", { name: "Streets" }).click();
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "0");
    await expect.poll(async () => page.locator("img.leaflet-tile-loaded").count()).toBeGreaterThan(0);
    await settings.getByRole("radio", { name: "Dark" }).click();
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "1");
    // units
    await settings.getByRole("radio", { name: "m", exact: true }).click();
    await expect(settings.getByText("Min. breaking height (m)")).toBeVisible();
    await settings.getByRole("radio", { name: "m/s", exact: true }).click();
    // filters still filter the markers
    const spots = (await apiGet(request, "/api/spots")) as Spot[];
    const europe = spots.filter((s) => s.region_group === "Europe").length;
    await settings.getByRole("button", { name: "Europe", exact: true }).click();
    await expect(page.locator("path.spot-marker")).toHaveCount(europe);
    await expect(settings.getByTestId("filter-count")).toHaveText(`${europe} of 50 spots match`);
    await shot(page, "desktop-1440x900-synthetic-settings");
    await page.keyboard.press("Escape");

    // Display settings persist in this browser; filters are per visit.
    await page.reload();
    await expect(page.locator("path.spot-marker")).toHaveCount(50);
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-title")).toHaveText("Offshore wave height, Hs (m)");
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect(page.getByTestId("legend-title")).toHaveText("Wind speed (m/s)");
  });

  test("the legend drawer opens and closes and names the data source and model cycle", async ({ page }) => {
    await openMapWithFields(page);
    const tab = page.getByRole("button", { name: "Legend", exact: true });
    await expect(tab).toHaveAttribute("aria-expanded", "false");
    await tab.click();
    await expect(tab).toHaveAttribute("aria-expanded", "true");
    const legend = page.getByTestId("legend-drawer");
    await expect(legend.getByTestId("legend-scale")).toBeVisible();
    await expect(legend.getByTestId("legend-source")).toContainText("Synthetic test fields");
    await expect(legend.getByTestId("legend-source")).toContainText("Synthetic test data, not a forecast");
    await expect(legend.getByTestId("legend-source")).toContainText("Model cycle");
    await expect(legend.getByTestId("map-legend")).toContainText("No usable forecast");
    await expect(legend.getByTestId("map-legend")).toContainText("Exceptional");
    await shot(page, "desktop-1440x900-synthetic-legend");
    await legend.getByRole("button", { name: "Close legend" }).click();
    await expect(legend).toHaveCount(0);
    await tab.click();
    await expect(page.getByTestId("legend-drawer")).toBeVisible();
    await tab.click();
    await expect(page.getByTestId("legend-drawer")).toHaveCount(0);
  });

  test("hovering the ocean shows the value under the cursor, and the map centre for keyboard users", async ({ page }) => {
    const { mock, nowIndex } = await openMapWithFields(page);
    const p = await screenPoint(page, -50, -120);
    await page.mouse.move(p.x, p.y);
    const readout = page.getByTestId("map-readout");
    await expect(readout).toContainText("Offshore waves (Hs)");
    await expect(readout).toContainText(/\d+(\.\d)? ft · \d+ s · from (N|NNE|NE|ENE|E|ESE|SE|SSE|S|SSW|SW|WSW|W|WNW|NW|NNW)\b/);
    // the value is the synthetic field at that point (± interpolation and rounding)
    const expected = syntheticValue("hs", -50, -120, nowIndex)! * 3.28084;
    const shown = Number(/([\d.]+) ft/.exec(await readout.innerText())![1]);
    expect(Math.abs(shown - expected)).toBeLessThan(1); // whole feet from 10 ft up
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect.poll(() => requested(mock, "wind_u", mock.validTimes[nowIndex]!)).toBe(true);
    await page.mouse.move(p.x + 3, p.y + 3);
    await expect(readout).toContainText(/\d+ km\/h · from [NESW]{1,3}\b/);
    await shot(page, "desktop-1440x900-synthetic-wind-readout");
    // leaving the map (onto a control) hides it
    await page.getByRole("button", { name: "Settings" }).hover();
    await expect(readout).toHaveCount(0);

    // Keyboard: with keyboard focus on the map itself → readout for the map centre. (The map is
    // the first stop of its region; after a key press, focusing it counts as keyboard focus.)
    await page.getByRole("radio", { name: "Wind Speed" }).focus();
    await page.keyboard.press("Shift+Tab");
    await page.locator(".leaflet-container").focus();
    expect(await page.evaluate(() => document.activeElement?.matches(".leaflet-container:focus-visible"))).toBe(true);
    await expect(page.getByTestId("map-readout-centre")).toBeVisible();
    await expect(readout).toContainText(/km\/h · from/);
    await page.keyboard.press("Tab");
    await expect(readout).toHaveCount(0);
  });

  test("the locate button flies to the user's position", async ({ page, context, baseURL }) => {
    await context.grantPermissions(["geolocation"], { origin: baseURL });
    await context.setGeolocation({ latitude: -33.92, longitude: 18.42 });
    await openMapWithFields(page);
    await page.getByRole("button", { name: "Show my location" }).click();
    const me = page.locator("path.user-location-marker");
    await expect(me).toBeVisible();
    const map = (await page.locator(".leaflet-container").boundingBox())!;
    await expect
      .poll(async () => {
        const b = (await me.boundingBox())!;
        return Math.hypot(b.x + b.width / 2 - (map.x + map.width / 2), b.y + b.height / 2 - (map.y + map.height / 2));
      }, { message: "map centred on the user", timeout: 10_000 })
      .toBeLessThan(30);
  });

  test("a denied location shows an error and leaves the map as it was", async ({ page }) => {
    await page.addInitScript(() => {
      const denied = { code: 1, PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3, message: "denied" };
      Object.defineProperty(navigator, "geolocation", {
        value: { getCurrentPosition: (_ok: unknown, fail: (e: unknown) => void) => setTimeout(() => fail(denied), 50), watchPosition: () => 0, clearWatch: () => undefined },
      });
    });
    await openMap(page);
    await page.getByRole("button", { name: "Show my location" }).click();
    await expect(page.getByText(/Location access was denied/)).toBeVisible();
    await expect(page.locator("path.user-location-marker")).toHaveCount(0);
  });

  test("without ocean layers the map shows a notice and everything else still works", async ({ page }) => {
    await mockFieldsUnavailable(page, "Ocean layers need live NOAA data");
    const fieldRequests: string[] = [];
    page.on("request", (r) => {
      if (/\/api\/map\/fields\/[a-z_]+\?/.test(r.url())) fieldRequests.push(r.url());
    });
    const { errors } = await openMap(page);
    await expect(page.getByTestId("fields-notice")).toHaveText("Ocean layers need live NOAA data");
    await expect(field(page)).toHaveAttribute("data-painted", "0");
    await expect(field(page)).toHaveAttribute("aria-label", /Ocean layers unavailable/);
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "1");
    // the timeline falls back to the spot snapshot range
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await expect(slider).toHaveAttribute("aria-valuenow", "0");
    expect(Number(await slider.getAttribute("aria-valuemax"))).toBeGreaterThan(40);
    await shot(page, "desktop-1440x900-unavailable");
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-source")).toContainText("Ocean layers need live NOAA data");
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    await expect(page.getByTestId("panel-spot-name")).toHaveText("Jeffreys Bay (Supertubes)");
    expect(fieldRequests).toEqual([]);
    expect(errors).toEqual([]);
  });
});

test.describe("ocean layers: geography, motion and bandwidth (synthetic fields)", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the colour field lands where Leaflet puts each position (asymmetric hot spots)", async ({ page, browser, baseURL }) => {
    await openMapWithFields(page, { fields: HOT_FIELD });
    // Whole world (zoom 2): hot at both spots, cold at their mirror images, which a flipped
    // row order, flipped columns or a 180° longitude shift would paint instead.
    for (const h of HOT_SPOTS) {
      const p = await screenPoint(page, h.lat, h.lon);
      expect((await fieldPixel(page, p.x, p.y))[0], `hot at ${h.lat}, ${h.lon}`).toBeGreaterThan(150);
      const shifted = h.lon > 0 ? h.lon - 180 : h.lon + 180;
      for (const [lat, lon] of [
        [-h.lat, h.lon],
        [h.lat, -h.lon],
        [h.lat, shifted],
        [-h.lat, -h.lon],
      ] as const) {
        const q = await screenPoint(page, lat, lon);
        expect((await fieldPixel(page, q.x, q.y))[0], `cold at the mirror image ${lat}, ${lon}`).toBeLessThan(90);
      }
    }
    // Zoom 7, where a 0.5° cell is 45 px wide (91 px tall at 60°N): the hot square (±1.2° at
    // the colour threshold) is centred on Leaflet's own projection of the spot, which the
    // user-location marker shows: within 3 px east-west and 0.04° north-south. A half-cell
    // shift would be 22 px / 0.25°. Each spot gets a fresh browser context, because the
    // locate button accepts a cached position up to 5 minutes old.
    for (const [n, h] of HOT_SPOTS.entries()) {
      const ctx = await browser.newContext({
        baseURL,
        viewport: { width: 1440, height: 900 },
        geolocation: { latitude: h.lat, longitude: h.lon },
        permissions: ["geolocation"],
      });
      try {
        const p = await ctx.newPage();
        await openMapWithFields(p, { fields: HOT_FIELD });
        await p.getByRole("button", { name: "Show my location" }).click();
        await expect(p.locator("path.user-location-marker")).toBeVisible();
        await expect(field(p)).toHaveAttribute("data-zoom", "7");
        const offset = async () => {
          const m = await centreOf(p, "path.user-location-marker");
          const c = await hotBox(p, m.x, m.y, 300);
          if (!c || c.n < 500) return null;
          const north = latAtRow(c.top, m.y, h.lat, 7);
          const south = latAtRow(c.bottom, m.y, h.lat, 7);
          return { dxPx: (c.left + c.right) / 2 - m.x, dLat: (north + south) / 2 - h.lat, north, south };
        };
        await expect
          .poll(async () => {
            const o = await offset();
            return o ? Math.max(Math.abs(o.dxPx) / 3, Math.abs(o.dLat) / 0.04) : Infinity;
          }, { message: `hot square centred on ${h.lat}, ${h.lon}`, timeout: 10_000 })
          .toBeLessThan(1);
        const o = (await offset())!;
        expect(o.north - h.lat).toBeGreaterThan(1.1); // the square is the expected size (±1.2°)
        expect(h.lat - o.south).toBeGreaterThan(1.1);
        // The hover readout samples the same place (the bilinear sampler, not the renderer).
        const m = await centreOf(p, "path.user-location-marker");
        await p.mouse.move(m.x + 1, m.y + 1);
        await expect(p.getByTestId("map-readout")).toContainText(/29\.5 ft|30 ft/);
        await shot(p, `hot-spot-${n + 1}-zoom7`);
      } finally {
        await ctx.close();
      }
    }
  });

  test("particles travel towards wave_dir + 180° on wave tabs and with (u, v) on the wind tab", async ({ page }) => {
    const { mock, now } = await openMapWithFields(page, { fields: UNIFORM_FLOW });
    const particles = page.locator("canvas.map-particle-canvas");
    await expect(particles).toHaveAttribute("data-mode", "animated");
    // Waves from 270° (west): trails head east.
    await page.waitForTimeout(1500); // let trails build up
    const wave = await trailGradient(page);
    expect(wave.gx, JSON.stringify(wave)).toBeGreaterThan(0);
    expect(Math.abs(wave.gx)).toBeGreaterThan(3 * Math.abs(wave.gy));
    // Wind towards the north (v > 0): trails head up the screen (y decreases).
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect(field(page)).toHaveAttribute("data-layer", "wind");
    await expect.poll(() => requested(mock, "wind_u", now) && requested(mock, "wind_v", now)).toBe(true);
    await expect.poll(async () => Number(await field(page).getAttribute("data-painted"))).toBeGreaterThan(10_000);
    await page.waitForTimeout(1500);
    const wind = await trailGradient(page);
    expect(wind.gy, JSON.stringify(wind)).toBeLessThan(0);
    expect(Math.abs(wind.gy)).toBeGreaterThan(3 * Math.abs(wind.gx));
    // Trails fade out completely. On an 8-bit canvas, dimming each frame by 0.92 stops at
    // alpha 6/255, a veil that piles up wherever particles have been (a spike at 6 in the
    // alpha histogram); the half-float particle canvas has none.
    await expect(particles).toHaveAttribute("data-color-type", "float16");
    await page.waitForTimeout(5000);
    const alpha = await particles.evaluate((c: HTMLCanvasElement) => {
      const d = c.getContext("2d")!.getImageData(0, 0, c.width, c.height).data;
      const h = new Array<number>(256).fill(0);
      for (let i = 3; i < d.length; i += 4) h[d[i]!]!++;
      return { at5: h[5]!, at6: h[6]!, at7: h[7]!, inked: (d.length / 4 - h[0]!) / (d.length / 4) };
    });
    test.info().annotations.push({ type: "particle canvas alpha", description: JSON.stringify(alpha) });
    expect(alpha.at6, JSON.stringify(alpha)).toBeLessThan(3 * Math.max(1, (alpha.at5 + alpha.at7) / 2));
  });

  test("prefers-reduced-motion: still arrows pointing downstream, nothing animates", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openMapWithFields(page, { fields: UNIFORM_FLOW });
    const particles = page.locator("canvas.map-particle-canvas");
    await expect(particles).toHaveAttribute("data-mode", "static");
    expect(Number(await particles.getAttribute("data-arrows"))).toBeGreaterThan(100);
    const a = await arrowLean(page);
    expect(a.dx, JSON.stringify(a)).toBeGreaterThan(0.5); // waves from the west: arrows point east
    expect(Math.abs(a.dx)).toBeGreaterThan(3 * Math.abs(a.dy));
    await page.waitForTimeout(800);
    expect((await arrowLean(page)).hash, "no animation").toBe(a.hash);
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect(field(page)).toHaveAttribute("data-layer", "wind");
    await expect.poll(async () => (await arrowLean(page)).dy).toBeLessThan(-0.5); // wind towards the north: arrows point up
    const w = await arrowLean(page);
    expect(Math.abs(w.dy)).toBeGreaterThan(3 * Math.abs(w.dx));
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-drawer")).toContainText("still arrows");
  });

  test("a hidden tab stops the particles and holds playback until it is shown again", async ({ page }) => {
    const { mock } = await openMapWithFields(page);
    const particles = page.locator("canvas.map-particle-canvas");
    await expect(particles).toHaveAttribute("data-mode", "animated");
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await slider.focus();
    await page.keyboard.press("Home");
    await page.getByRole("button", { name: "Play forecast" }).click();
    await expect(slider).toHaveAttribute("aria-valuenow", "1", { timeout: 5_000 });
    const setHidden = (hidden: boolean) =>
      page.evaluate((h) => {
        Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (h ? "hidden" : "visible") });
        Object.defineProperty(document, "hidden", { configurable: true, get: () => h });
        document.dispatchEvent(new Event("visibilitychange"));
      }, hidden);
    await setHidden(true);
    await expect(particles).toHaveAttribute("data-mode", "off");
    await page.waitForTimeout(600); // a debounced request already scheduled may still go out
    const held = await slider.getAttribute("aria-valuenow");
    const requests = mock.requests.length;
    // Asserting that nothing happens needs a fixed wait: more than two playback steps (1.1 s each).
    await page.waitForTimeout(2_600);
    await expect(slider).toHaveAttribute("aria-valuenow", held!);
    expect(mock.requests.length, "no downloads while hidden").toBe(requests);
    await setHidden(false);
    await expect(particles).toHaveAttribute("data-mode", "animated");
    await expect.poll(async () => Number(await slider.getAttribute("aria-valuenow"))).toBeGreaterThan(Number(held));
    await page.getByRole("button", { name: "Pause" }).click();
  });

  test("dragging the timeline downloads only the step it settles on and its neighbours", async ({ page }) => {
    const { mock, nowIndex } = await openMapWithFields(page);
    const t = mock.validTimes;
    // The start-up prefetch of the neighbouring steps is done.
    await expect.poll(() => requested(mock, "hs", t[nowIndex + 1]!) && requested(mock, "hs", t[nowIndex - 1]!)).toBe(true);
    await page.waitForTimeout(500);
    const before = mock.requests.length;
    const slider = page.getByRole("slider", { name: "Forecast time" });
    const box = (await slider.boundingBox())!;
    await page.mouse.move(box.x + box.width * 0.02, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.98, box.y + box.height / 2, { steps: 20 });
    await page.mouse.up();
    const end = Number(await slider.getAttribute("aria-valuenow"));
    expect(end).toBeGreaterThan(t.length - 3);
    await expect.poll(() => requested(mock, "hs", t[end]!) && requested(mock, "hs", t[end - 1]!)).toBe(true);
    await page.waitForTimeout(800);
    const times = new Set(mock.requests.slice(before).map((r) => r.split("@")[1]));
    // The settled step and its neighbours (one more if the machine was slow enough for the
    // 250 ms debounce to fire during the drag), not every step the thumb passed.
    expect([...times].length, [...times].join(", ")).toBeLessThanOrEqual(4);
    expect(times.has(t[end]!)).toBe(true);
    // Each request names the model run (a cache key only: a new run never reuses old bytes).
    expect(mock.urls.every((u) => new URL(u).searchParams.get("run") === "synthetic-run")).toBe(true);
  });

  test("performance: colour-field redraw, particle frame cost and field-cache memory", async ({ page }) => {
    const { mock } = await openMapWithFields(page);
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Performance.enable");
    const metrics = async () => {
      const { metrics: m } = (await cdp.send("Performance.getMetrics")) as { metrics: Array<{ name: string; value: number }> };
      return Object.fromEntries(m.map((x) => [x.name, x.value]));
    };
    /** Main-thread script and task time per animation frame over `ms`. */
    const perFrame = async (ms: number) => {
      const m0 = await metrics();
      const frames = await page.evaluate(
        (dur) =>
          new Promise<number>((resolve) => {
            let n = 0;
            const t0 = performance.now();
            const tick = () => {
              n++;
              if (performance.now() - t0 < dur) requestAnimationFrame(tick);
              else resolve(n);
            };
            requestAnimationFrame(tick);
          }),
        ms,
      );
      const m1 = await metrics();
      return { frames, scriptMs: ((m1.ScriptDuration! - m0.ScriptDuration!) * 1000) / frames, taskMs: ((m1.TaskDuration! - m0.TaskDuration!) * 1000) / frames };
    };
    const particles = page.locator("canvas.map-particle-canvas");
    await expect(particles).toHaveAttribute("data-mode", "animated");
    const count = await particles.evaluate((c: HTMLCanvasElement) => `${c.width}x${c.height}`);
    const animated = await perFrame(3000);
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByTestId("map-settings").getByRole("switch", { name: /Animated particles/ }).click();
    await page.keyboard.press("Escape");
    await expect(particles).toHaveAttribute("data-mode", "off");
    const still = await perFrame(3000);
    const note = `particles on: ${animated.scriptMs.toFixed(2)} ms script / ${animated.taskMs.toFixed(2)} ms task per frame (${animated.frames} frames, canvas ${count}); off: ${still.scriptMs.toFixed(2)} / ${still.taskMs.toFixed(2)} ms`;
    console.log(note);
    test.info().annotations.push({ type: "particle frame cost (1440x900)", description: note });
    expect(animated.scriptMs).toBeLessThan(12); // regression guard; one frame at 60 Hz is 16.7 ms

    // Field cache: step through 12 times (36 fields with the prefetch); the LRU keeps 24.
    const heap = async () => (await cdp.send("Runtime.getHeapUsage")) as { usedSize: number; totalSize: number; backingStorageSize?: number };
    await cdp.send("HeapProfiler.collectGarbage");
    const h0 = await heap();
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await slider.focus();
    await page.keyboard.press("Home");
    for (let i = 1; i <= 12; i++) {
      await page.keyboard.press("ArrowRight");
      await expect.poll(() => requested(mock, "hs", mock.validTimes[i]!)).toBe(true);
      await expect(field(page)).toHaveAttribute("data-at", mock.validTimes[i]!);
    }
    await page.waitForTimeout(500);
    await cdp.send("HeapProfiler.collectGarbage");
    const h1 = await heap();
    const mb = (b: number) => (b / 1e6).toFixed(1);
    const grew = h1.usedSize + (h1.backingStorageSize ?? 0) - h0.usedSize - (h0.backingStorageSize ?? 0);
    const memNote = `JS heap ${mb(h0.usedSize)} → ${mb(h1.usedSize)} MB, array buffers ${mb(h0.backingStorageSize ?? 0)} → ${mb(h1.backingStorageSize ?? 0)} MB after ${new Set(mock.requests).size} field downloads (expected decoded cache: 24 × 720 × 361 × 4 B = 25.0 MB)`;
    console.log(memNote);
    test.info().annotations.push({ type: "field cache memory", description: memNote });
    expect(grew / 1e6).toBeLessThan(80);
  });
});

test.describe("map on a phone (390 × 844)", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("tabs scroll, the spot panel is a bottom sheet and the timeline works", async ({ page }) => {
    const { mock, nowIndex } = await openMapWithFields(page);
    // Best spots start collapsed on small screens.
    const listButton = page.getByRole("button", { name: /Best spots now/ });
    await expect(listButton).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("top-spots")).toHaveCount(0);
    await shot(page, "mobile-390x844-synthetic-wave-height");

    // The layer row scrolls sideways; every tab and the pressure toggle stay reachable.
    const tabs = page.getByTestId("layer-tabs");
    expect(await tabs.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    await page.getByRole("radio", { name: "Wave Period" }).click();
    await expect(field(page)).toHaveAttribute("data-layer", "tp");
    await page.getByRole("button", { name: "Pressure" }).click();
    await expect(page.getByRole("button", { name: "Pressure" })).toHaveAttribute("aria-pressed", "true");

    // A tap on the ocean shows the readout for a few seconds.
    const sea = await screenPoint(page, -30, -20);
    await page.touchscreen.tap(sea.x, sea.y);
    await expect(page.getByTestId("map-readout")).toContainText("Offshore peak period");
    await expect(page.getByTestId("map-readout")).toHaveCount(0, { timeout: 8_000 });

    // Timeline: tap on the track.
    const slider = page.getByRole("slider", { name: "Forecast time" });
    const box = (await slider.boundingBox())!;
    await page.touchscreen.tap(box.x + box.width * 0.8, box.y + box.height / 2);
    const i = Number(await slider.getAttribute("aria-valuenow"));
    expect(i).toBeGreaterThan(nowIndex);
    await expect.poll(() => requested(mock, "tp", mock.validTimes[i]!)).toBe(true);

    // The list opens on demand.
    await listButton.click();
    await expect(page.getByTestId("top-spots")).toBeVisible();
    await shot(page, "mobile-390x844-synthetic-best-spots");
    await listButton.click();

    // Spot panel: a bottom sheet over the timeline.
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    const panel = page.getByTestId("map-spot-panel");
    await expect(panel.getByTestId("panel-spot-name")).toHaveText("Jeffreys Bay (Supertubes)");
    const sheet = (await panel.boundingBox())!;
    expect(sheet.y + sheet.height).toBeGreaterThan(844 - 12);
    expect(sheet.width).toBeGreaterThan(370);
    await expect(panel.getByTestId("our-prediction").or(panel.getByTestId("no-forecast"))).toBeVisible();
    await shot(page, "mobile-390x844-synthetic-spot-sheet", 1500);
    await panel.getByRole("button", { name: "Close spot details" }).click();
    await expect(panel).toHaveCount(0);
    await expect(slider).toBeVisible();

    // Settings and legend fit the screen.
    await page.getByRole("button", { name: "Settings" }).click();
    const settings = (await page.getByTestId("map-settings").boundingBox())!;
    expect(settings.x).toBeGreaterThanOrEqual(0);
    expect(settings.x + settings.width).toBeLessThanOrEqual(390);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-drawer")).toBeVisible();
    // The open drawer leaves the zoom buttons at the right edge uncovered and usable.
    const drawer = (await page.getByTestId("legend-drawer").boundingBox())!;
    const zoom = (await page.locator(".leaflet-control-zoom").boundingBox())!;
    expect(drawer.x + drawer.width).toBeLessThan(zoom.x);
    await page.locator(".leaflet-control-zoom-in").click({ timeout: 5_000 });
    await shot(page, "mobile-390x844-synthetic-legend");
  });

  test("a long source attribution goes to the legend, not over the markers", async ({ page }) => {
    // The real NOAA attribution is a 140-character sentence; on a phone it wrapped to three
    // lines over the map and caught taps meant for markers (seen with live data).
    const origin = {
      code: "synthetic",
      name: "Synthetic test fields",
      attribution:
        "Synthetic test data, not a forecast: a sentence as long as a real model attribution, naming an agency, two models and the programme that disseminates them.",
      is_demo: true,
    };
    await openMapWithFields(page, { origin });
    const strip = page.locator(".leaflet-control-attribution");
    await expect(strip).toContainText("Synthetic test fields");
    await expect(strip).not.toContainText("programme that disseminates");
    expect((await strip.boundingBox())!.height).toBeLessThan(34); // at most two short lines
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    await expect(page.getByTestId("panel-spot-name")).toHaveText("Jeffreys Bay (Supertubes)");
    await page.getByRole("button", { name: "Close spot details" }).click();
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-source")).toContainText(origin.attribution);
  });
});
