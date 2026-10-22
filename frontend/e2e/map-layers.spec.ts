import { expect, test, type Page } from "@playwright/test";

import { apiGet } from "./helpers";
import { mockFields, mockFieldsUnavailable, PARTITION_VARIABLES, syntheticValue, VARIABLES, type SyntheticFields } from "./map-fields.mock";
import { chooseWaveSystems, field, LAND_RGB, mapSettled, openMap, openMapWithFields, painted, requested, screenPoint, shot, type ProbeWindow, type Spot } from "./map-helpers";

/**
 * The map's ocean layers against the production build: the colour field, the wave crest
 * dashes (wave layers), the wind particles (Wind layer and the wind overlay), isobars, the hover
 * readout, the legend and the display settings. They are tested with route-mocked SYNTHETIC
 * fields from ./map-fields.mock.ts (made-up patterns, not a forecast); the backend in this
 * suite has no live NOAA data, so its own index reports "unavailable". Navigation, markers,
 * the spot panel and the timeline are in map.spec.ts.
 */

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
 * Direction the particle trails point: the alpha difference over ±3 px, summed over the
 * particle canvas. A trail is brightest at its head, so along one trail the sum is about
 * 3 × (head − tail) alpha in the direction of travel (x east, y down); across a thin
 * antialiased line (at most 3 px wide) it is zero, so line thickness adds no noise.
 */
async function trailGradient(page: Page) {
  return page.locator("canvas.map-particle-canvas").evaluate((c: HTMLCanvasElement) => {
    const { width: w, height: h } = c;
    const d = c.getContext("2d")!.getImageData(0, 0, w, h).data;
    const a = (x: number, y: number) => d[(y * w + x) * 4 + 3]!;
    let gx = 0;
    let gy = 0;
    for (let y = 3; y < h - 3; y++) {
      for (let x = 3; x < w - 3; x++) {
        if (!a(x, y)) continue;
        gx += a(x + 3, y) - a(x - 3, y);
        gy += a(x, y + 3) - a(x, y - 3);
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

const wave = (page: Page) => page.locator("canvas.map-wave-canvas");

/**
 * What the wave canvas shows: "ink" = alpha x brightness (the crest dashes), the dominant
 * line orientation from the structure tensor of
 * the ink (degrees in screen coordinates, x east, y down: 0 = horizontal crests, 90 =
 * vertical, 45 = running from top left to bottom right), its coherence (0..1), an
 * orientation histogram (36 bins of 5 degrees, gradient energy), and a hash of the pixels.
 */
async function waveImage(page: Page) {
  return wave(page).evaluate((c: HTMLCanvasElement) => {
    const { width: w, height: h } = c;
    const d = c.getContext("2d")!.getImageData(0, 0, w, h).data;
    const b = new Float32Array(w * h);
    let ink = 0;
    let hash = 0;
    for (let k = 0; k < w * h; k++) {
      const a = d[k * 4 + 3]!;
      if (!a) continue;
      b[k] = ((a / 255) * (d[k * 4]! + d[k * 4 + 1]! + d[k * 4 + 2]!)) / 765;
      ink += b[k]!;
      hash = (hash * 31 + a * ((k % w) + 1) + Math.floor(k / w)) % 1_000_000_007;
    }
    let jxx = 0;
    let jyy = 0;
    let jxy = 0;
    const hist = new Array<number>(36).fill(0);
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const k = y * w + x;
        const gx = (b[k + 1]! - b[k - 1]!) / 2;
        const gy = (b[k + w]! - b[k - w]!) / 2;
        const e = gx * gx + gy * gy;
        if (!e) continue;
        jxx += gx * gx;
        jyy += gy * gy;
        jxy += gx * gy;
        // line orientation = gradient orientation + 90 degrees, folded to 0..180
        const line = ((((Math.atan2(gy, gx) * 180) / Math.PI + 90) % 180) + 180) % 180;
        hist[Math.floor(line / 5) % 36]! += e;
      }
    }
    const grad = (0.5 * Math.atan2(2 * jxy, jxx - jyy) * 180) / Math.PI;
    const orientation = (((grad + 90) % 180) + 180) % 180;
    const coherence = Math.hypot(jxx - jyy, 2 * jxy) / Math.max(1e-9, jxx + jyy);
    return { ink, orientation, coherence, hist, hash, width: w, height: h };
  });
}

/** Orientation peaks of a waveImage histogram: bins (5 degrees) holding > `share` of the energy after smoothing. */
function orientationPeaks(hist: number[], share = 0.15): number[] {
  const total = hist.reduce((a, v) => a + v, 0);
  const smooth = hist.map((_, i) => hist[(i + 35) % 36]! + hist[i]! + hist[(i + 1) % 36]!);
  const peaks: number[] = [];
  smooth.forEach((v, i) => {
    if (v > share * total && v >= smooth[(i + 35) % 36]! && v >= smooth[(i + 1) % 36]!) peaks.push(i * 5 + 2.5);
  });
  return peaks;
}

/** The smoothed energy share of each local maximum of a waveImage histogram, for the log ("2.5°: 0.21"). */
function peakShares(hist: number[]): string {
  const total = hist.reduce((a, v) => a + v, 0);
  const smooth = hist.map((_, i) => hist[(i + 35) % 36]! + hist[i]! + hist[(i + 1) % 36]!);
  return smooth
    .map((v, i) => ({ v, i }))
    .filter(({ v, i }) => v >= smooth[(i + 35) % 36]! && v >= smooth[(i + 1) % 36]! && v > 0.05 * total)
    .map(({ v, i }) => `${i * 5 + 2.5}°: ${(v / total).toFixed(2)}`)
    .join(", ");
}

/**
 * Bright ink of the wave canvas projected on a direction (dx, dy): a 1 px binned profile.
 * Rows of crest dashes across that direction show as peaks spaced by the crest spacing.
 */
async function inkProfile(page: Page, dx: number, dy: number) {
  return wave(page).evaluate(
    (c: HTMLCanvasElement, [ux, uy]) => {
      const { width: w, height: h } = c;
      const d = c.getContext("2d")!.getImageData(0, 0, w, h).data;
      const n = Math.ceil(Math.abs(ux) * w + Math.abs(uy) * h) + 2;
      const off = Math.min(0, ux * w) + Math.min(0, uy * h);
      const prof = new Array<number>(n).fill(0);
      for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
          const k = (y * w + x) * 4;
          const a = d[k + 3]!;
          if (!a) continue;
          prof[Math.floor(x * ux + y * uy - off)]! += ((a / 255) * (d[k]! + d[k + 1]! + d[k + 2]!)) / 765;
        }
      }
      return { prof, scale: w / c.getBoundingClientRect().width };
    },
    [dx, dy] as const,
  );
}

/** First autocorrelation peak of a profile (lag in profile bins) between `min` and `max`. */
function period(prof: number[], min: number, max: number): number {
  const mean = prof.reduce((a, v) => a + v, 0) / prof.length;
  const x = prof.map((v) => v - mean);
  const ac = (lag: number) => x.slice(lag).reduce((a, v, i) => a + v * x[i]!, 0);
  const vals = Array.from({ length: max + 2 }, (_, lag) => (lag >= min - 1 ? ac(lag) : -Infinity));
  for (let lag = min; lag <= max; lag++) if (vals[lag]! > 0 && vals[lag]! >= vals[lag - 1]! && vals[lag]! >= vals[lag + 1]!) return lag;
  return NaN;
}

/** Shift (bins) of profile `b` relative to `a` with the highest correlation, within ±maxShift. */
function shiftOf(a: number[], b: number[], maxShift: number): number {
  let best = 0;
  let bestV = -Infinity;
  for (let s = -maxShift; s <= maxShift; s++) {
    let v = 0;
    for (let i = maxShift; i < a.length - maxShift; i++) v += a[i]! * b[i + s]!;
    if (v > bestV) {
      bestV = v;
      best = s;
    }
  }
  return best;
}

/** Largest wave-canvas alpha within `r` canvas px of (lat, lon), from the view the canvas was drawn for. */
async function waveAlphaNear(page: Page, lat: number, lon: number, r = 3) {
  return wave(page).evaluate(
    (c: HTMLCanvasElement, [la, lo, rr]) => {
      const [ox, oy, vw, vh, world] = c.dataset.view!.split(",").map(Number) as [number, number, number, number, number];
      const mx = ((lo + 180) / 360) * world;
      const my = (0.5 - Math.log(Math.tan(Math.PI / 4 + (la * Math.PI) / 360)) / (2 * Math.PI)) * world;
      let x = mx - ox;
      x += Math.round((vw / 2 - x) / world) * world; // the world copy nearest the view centre
      const cx = Math.round((x * c.width) / vw);
      const cy = Math.round(((my - oy) * c.height) / vh);
      if (cx < rr || cy < rr || cx >= c.width - rr || cy >= c.height - rr) return -1;
      const d = c.getContext("2d")!.getImageData(cx - rr, cy - rr, 2 * rr + 1, 2 * rr + 1).data;
      let max = 0;
      for (let i = 3; i < d.length; i += 4) max = Math.max(max, d[i]!);
      return max;
    },
    [lat, lon, r] as const,
  );
}

/** SYNTHETIC uniform wave systems (height m, period s, direction FROM deg); a missing one has no data. */
function swells(spec: Partial<Record<"primary" | "secondary" | "windsea", [number, number, number]>>, combined: [number, number, number] = [3, 12, 250]): { fields: SyntheticFields; variables: string[] } {
  const prefix = { primary: "swell1", secondary: "swell2", windsea: "windsea" } as const;
  const values: Record<string, number> = { hs: combined[0], tp: combined[1], wave_dir: combined[2], wind_u: 0, wind_v: 8, pressure: 1013 };
  for (const [system, v] of Object.entries(spec) as Array<[keyof typeof prefix, [number, number, number]]>) {
    values[`${prefix[system]}_hs`] = v[0];
    values[`${prefix[system]}_tp`] = v[1];
    values[`${prefix[system]}_dir`] = v[2];
  }
  return {
    fields: { name: `swells-${JSON.stringify(values)}`, value: (variable) => values[variable] ?? null },
    variables: [...VARIABLES, ...PARTITION_VARIABLES.filter((v) => values[v] != null)],
  };
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
    // The South Pacific shows the field; inland Western Australia shows the grey land (#737376)
    // drawn over it (the synthetic field has values everywhere, the land masks them).
    const lp = await screenPoint(page, -27, 121);
    const land = await pixels(page, lp.x, lp.y);
    for (const ch of [0, 1, 2] as const) expect(Math.abs(median(land.map((p) => p[ch])) - LAND_RGB[ch])).toBeLessThan(10);
    const sp = await screenPoint(page, -50, -120);
    expect(median((await pixels(page, sp.x, sp.y)).map(saturation))).toBeGreaterThan(40); // a saturated colour, not grey
    // Wave crest dashes run over the ocean; wave layers never use the wind particle canvas.
    await expect(wave(page)).toHaveAttribute("data-mode", "animated");
    await expect(wave(page)).toHaveAttribute("data-style", "dashes");
    await expect(page.locator("canvas.map-particle-canvas")).toHaveCount(0);
    await shot(page, "desktop-1440x900-synthetic-wave-height");

    // Redraw time of the colour field at 1440 × 900 (half-resolution canvas), over 6 pans.
    const times: number[] = [];
    for (let i = 0; i < 6; i++) {
      const draws = await field(page).getAttribute("data-draws");
      await page.mouse.move(800, 300);
      await page.mouse.down();
      // Left-down, then right-up: the six pans end about where they started. (Six pans all
      // 20 px down carried Jeffreys Bay ~130 px down, onto the top edge of the timeline.)
      await page.mouse.move(800 + (i % 2 ? -90 : 90), 300 + (i % 2 ? -20 : 20), { steps: 4 });
      await page.mouse.up();
      await expect(field(page)).not.toHaveAttribute("data-draws", draws!);
      times.push(Number(await field(page).getAttribute("data-draw-ms")));
    }
    const mid = median(times);
    console.log(`colour-field redraw at 1440x900: ${times.map((t) => t.toFixed(1)).join(", ")} ms (median ${mid.toFixed(1)} ms)`);
    test.info().annotations.push({ type: "field redraw ms (1440x900)", description: `${times.join(", ")}; median ${mid}` });
    expect(mid).toBeLessThan(250); // regression guard; the target is < 60 ms (see the log)

    // Markers stay clickable above every layer. Wait for the last pan's inertia to end first:
    // a click while the map still glides can land beside the moving marker.
    await mapSettled(page);
    const jbay = page.locator("path.spot-jeffreys-bay");
    // Nothing is drawn over the marker's centre: no map layer, and none of the page's controls.
    const onTop = await jbay.evaluate((el) => {
      const b = el.getBoundingClientRect();
      return document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2) === el;
    });
    expect(onTop, "Jeffreys Bay's marker is the topmost element at its centre").toBe(true);
    await jbay.click({ force: true });
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
  test("settings switch units, particles and basemap, persist, and filters still filter", async ({ page, request }) => {
    await openMapWithFields(page);
    await page.getByRole("button", { name: "Settings" }).click();
    const settings = page.getByTestId("map-settings");
    // animation off / on (the wave crests on this wave layer)
    const flow = settings.getByRole("switch", { name: /Animated flow/ });
    await expect(flow).toHaveAttribute("aria-checked", "true");
    await flow.click();
    await expect(wave(page)).toHaveAttribute("data-mode", "off");
    await flow.click();
    await expect(wave(page)).toHaveAttribute("data-mode", "animated");
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
  test("a map moved under a still mouse reads the value at the place now under the cursor", async ({ page }) => {
    // Live 2026-10-16: after a programmatic move (the spot list's fly-to, the recentre button,
    // keys) the readout kept the old place's values at the cursor's screen position.
    const { nowIndex } = await openMapWithFields(page);
    const p = await screenPoint(page, -50, -120);
    await page.mouse.move(p.x, p.y);
    const readout = page.getByTestId("map-readout");
    await expect(readout).toContainText("Offshore waves (Hs)");
    const before = Number(await readout.getAttribute("data-lon"));
    expect(before).toBeCloseTo(-120, 0);
    await page.evaluate(() => {
      const m = (window as unknown as ProbeWindow).__maps![0]!;
      const c = m.getCenter();
      m.setView([c.lat, c.lng + 25], m.getZoom(), { animate: false });
    });
    await expect.poll(async () => Number(await readout.getAttribute("data-lon"))).toBeGreaterThan(before + 20);
    const lat = Number(await readout.getAttribute("data-lat"));
    const lon = Number(await readout.getAttribute("data-lon"));
    // The place the readout names is the one under the cursor ...
    const at = await page.evaluate(([la, lo]) => {
      const m = (window as unknown as ProbeWindow).__maps![0]!;
      const pt = m.latLngToContainerPoint([la, lo]);
      const r = m.getContainer().getBoundingClientRect();
      return { x: r.left + pt.x, y: r.top + pt.y };
    }, [lat, lon] as const);
    expect(Math.abs(at.x - p.x)).toBeLessThan(2);
    expect(Math.abs(at.y - p.y)).toBeLessThan(2);
    // ... and the value is the synthetic field there (± interpolation and rounding).
    const expected = syntheticValue("hs", lat, lon, nowIndex)! * 3.28084;
    const shown = Number(/([\d.]+) ft/.exec(await readout.innerText())![1]);
    expect(Math.abs(shown - expected)).toBeLessThan(1);
  });

  test("without ocean layers the map shows a notice and everything else still works", async ({ page }) => {
    await mockFieldsUnavailable(page, "Ocean layers need live NOAA data");
    const fieldRequests: string[] = [];
    page.on("request", (r) => {
      if (/\/api\/map\/fields\/[a-z0-9_]+\?/.test(r.url())) fieldRequests.push(r.url());
    });
    const { errors } = await openMap(page);
    // A short line on the map; the backend's own message is in the legend (checked below).
    await expect(page.getByTestId("fields-notice")).toHaveText("Ocean layers are unavailable right now.");
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

  test("wind particles travel with (u, v) on the Wind layer; wave layers draw crests instead", async ({ page }) => {
    const { mock, now } = await openMapWithFields(page, { fields: UNIFORM_FLOW });
    await expect(wave(page)).toHaveAttribute("data-mode", "animated");
    await expect(page.locator("canvas.map-particle-canvas")).toHaveCount(0);
    // Wind towards the north (v > 0): trails head up the screen (y decreases).
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect(field(page)).toHaveAttribute("data-layer", "wind");
    await expect.poll(() => requested(mock, "wind_u", now) && requested(mock, "wind_v", now)).toBe(true);
    await expect.poll(async () => Number(await field(page).getAttribute("data-painted"))).toBeGreaterThan(10_000);
    const particles = page.locator("canvas.map-particle-canvas");
    await expect(particles).toHaveAttribute("data-mode", "animated");
    await expect(wave(page)).toHaveCount(0);
    await page.waitForTimeout(1500);
    const wind = await trailGradient(page);
    expect(wind.gy, JSON.stringify(wind)).toBeLessThan(0);
    expect(Math.abs(wind.gy)).toBeGreaterThan(3 * Math.abs(wind.gx));
    console.log(`wind trail gradient: ${JSON.stringify(wind)}`);
  });

  for (const renderer of ["native", "8-bit"] as const) {
    test(`wind trails fade out completely, without an alpha-6 veil (${renderer} canvas)`, { tag: "@cross-browser" }, async ({ page, browserName }) => {
      if (renderer === "8-bit") {
        // Chromium has half-float canvases; take that away to exercise the explicit trail
        // renderer that Firefox, Safari and iOS use.
        await page.addInitScript(() => {
          const get = HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: string, opts?: Record<string, unknown>) {
            if (opts && "colorType" in opts) {
              const { colorType: _drop, ...rest } = opts;
              void _drop;
              return get.call(this, type, rest);
            }
            return get.call(this, type, opts);
          } as typeof HTMLCanvasElement.prototype.getContext;
        });
      }
      const { mock, now } = await openMapWithFields(page, { fields: UNIFORM_FLOW });
      await page.getByRole("radio", { name: "Wind Speed" }).click();
      await expect.poll(() => requested(mock, "wind_u", now) && requested(mock, "wind_v", now)).toBe(true);
      const particles = page.locator("canvas.map-particle-canvas");
      await expect(particles).toHaveAttribute("data-mode", "animated");
      const colorType = await particles.getAttribute("data-color-type");
      const trails = await particles.getAttribute("data-trails");
      if (renderer === "8-bit" || browserName !== "chromium") {
        expect(trails).toBe("explicit");
      } else {
        expect(colorType).toBe("float16");
        expect(trails).toBe("fade");
      }
      // On an 8-bit canvas, dimming each frame by 0.92 stops at alpha 6/255, a veil that piles
      // up wherever particles have been (a spike at 6 in the alpha histogram).
      await page.waitForTimeout(5000);
      const alpha = await particles.evaluate((c: HTMLCanvasElement) => {
        const d = c.getContext("2d")!.getImageData(0, 0, c.width, c.height).data;
        const h = new Array<number>(256).fill(0);
        for (let i = 3; i < d.length; i += 4) h[d[i]!]!++;
        return { at5: h[5]!, at6: h[6]!, at7: h[7]!, inked: (d.length / 4 - h[0]!) / (d.length / 4) };
      });
      test.info().annotations.push({ type: `particle canvas alpha (${renderer}, ${colorType}, ${trails})`, description: JSON.stringify(alpha) });
      console.log(`particle canvas alpha (${renderer}, ${colorType}, ${trails}): ${JSON.stringify(alpha)}`);
      expect(alpha.inked).toBeGreaterThan(0.002); // the particles do draw
      expect(alpha.at6, JSON.stringify(alpha)).toBeLessThan(3 * Math.max(1, (alpha.at5 + alpha.at7) / 2));
      expect(alpha.inked, "no veil over the ocean").toBeLessThan(0.35);
    });
  }

  test("prefers-reduced-motion: still crests on wave layers, still arrows for wind, nothing animates", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openMapWithFields(page, { fields: UNIFORM_FLOW });
    // Waves from the west: still, vertical crests with arrowheads pointing east.
    await expect(wave(page)).toHaveAttribute("data-mode", "static");
    await expect.poll(async () => Number(await wave(page).getAttribute("data-crests"))).toBeGreaterThan(20);
    expect(Number(await wave(page).getAttribute("data-arrows"))).toBeGreaterThan(3);
    const still = await waveImage(page);
    expect(still.ink).toBeGreaterThan(0);
    expect(Math.abs(still.orientation - 90), JSON.stringify(still)).toBeLessThan(8);
    await page.waitForTimeout(1000);
    expect((await waveImage(page)).hash, "no animation").toBe(still.hash);
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect(field(page)).toHaveAttribute("data-layer", "wind");
    const particles = page.locator("canvas.map-particle-canvas");
    await expect(particles).toHaveAttribute("data-mode", "static");
    expect(Number(await particles.getAttribute("data-arrows"))).toBeGreaterThan(100);
    await expect.poll(async () => (await arrowLean(page)).dy).toBeLessThan(-0.5); // wind towards the north: arrows point up
    const w = await arrowLean(page);
    expect(Math.abs(w.dy)).toBeGreaterThan(3 * Math.abs(w.dx));
    await page.waitForTimeout(800);
    expect((await arrowLean(page)).hash, "no animation").toBe(w.hash);
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-drawer")).toContainText("still arrows");
  });

  test("a hidden tab stops the animation and holds playback until it is shown again", async ({ page }) => {
    const { mock } = await openMapWithFields(page);
    const particles = wave(page); // the wave crests of the default Wave Height layer
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
});

/** Page position of (lat, lon) from the view the colour-field canvas was drawn for (any zoom; nearest world copy). */
async function pagePoint(page: Page, lat: number, lon: number) {
  return field(page).evaluate(
    (c: HTMLCanvasElement, [la, lo]) => {
      const [ox, oy, vw, vh, world] = c.dataset.view!.split(",").map(Number) as [number, number, number, number, number];
      const r = c.getBoundingClientRect();
      const mx = ((lo + 180) / 360) * world;
      const my = (0.5 - Math.log(Math.tan(Math.PI / 4 + (la * Math.PI) / 360)) / (2 * Math.PI)) * world;
      let x = mx - ox;
      x += Math.round((vw / 2 - x) / world) * world;
      return { x: r.left + (x * r.width) / vw, y: r.top + ((my - oy) * r.height) / vh };
    },
    [lat, lon] as const,
  );
}

/**
 * Opens the map with SYNTHETIC uniform wave systems, the systems in `families` chosen (the
 * combined sea, the default, needs no choice), and waits for drawn dashes of `families`.
 */
async function openWithSwells(page: Page, spec: Parameters<typeof swells>[0], families: string) {
  if (families !== "combined") await chooseWaveSystems(page, families.split(","));
  const s = swells(spec);
  const opened = await openMapWithFields(page, { fields: s.fields, variables: s.variables });
  await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "1"); // the coastline mask is ready
  await expect(wave(page)).toHaveAttribute("data-families", families);
  await expect.poll(async () => Number(await wave(page).getAttribute("data-crests"))).toBeGreaterThan(10);
  return opened;
}

test.describe("wave crest dashes (synthetic partitions)", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("crest spacing follows the period: rows of 16 s dashes are 4 times as far apart as 8 s rows", async ({ browser, baseURL }) => {
    const spacing = async (tp: number) => {
      const ctx = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 } });
      try {
        const page = await ctx.newPage();
        await openWithSwells(page, { primary: [2.5, tp, 270] }, "primary");
        const { prof, scale } = await inkProfile(page, 1, 0); // waves from the west: vertical crest dashes in vertical rows
        return period(prof, Math.round(6 * scale), Math.round(90 * scale)) / scale;
      } finally {
        await ctx.close();
      }
    };
    const s8 = await spacing(8);
    const s16 = await spacing(16);
    console.log(`crest spacing: 8 s ${s8} px, 16 s ${s16} px (ratio ${(s16 / s8).toFixed(2)})`);
    test.info().annotations.push({ type: "crest spacing 8 s / 16 s", description: `${s8} / ${s16} px` });
    expect(Math.abs(s8 - 12)).toBeLessThanOrEqual(1); // 0.12 px/m x 99.9 m
    expect(Math.abs(s16 / s8 - 4)).toBeLessThanOrEqual(0.2); // D11: 4.0 ± 0.2
  });

  test("higher waves draw more ink: 4 m more than twice 1 m", async ({ browser, baseURL }) => {
    const ink = async (hs: number) => {
      const ctx = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 } });
      try {
        const page = await ctx.newPage();
        await openWithSwells(page, { primary: [hs, 12, 250] }, "primary");
        let sum = 0;
        for (let i = 0; i < 4; i++) {
          sum += (await waveImage(page)).ink;
          await page.waitForTimeout(250);
        }
        return sum / 4;
      } finally {
        await ctx.close();
      }
    };
    const [low, high] = [await ink(1), await ink(4)];
    console.log(`crest ink: 1 m ${low.toFixed(0)}, 4 m ${high.toFixed(0)}`);
    expect(high).toBeGreaterThan(2 * low); // D11 (the traced crests were tested at 1.5 x)
  });

  test("a primary swell from the W and a secondary from the S give two crest orientations; without the secondary, one", async ({ page, browser, baseURL }) => {
    const { mock } = await openWithSwells(page, { primary: [2.5, 13, 270], secondary: [1.8, 10, 180] }, "secondary,primary");
    const hist = (await waveImage(page)).hist;
    const both = orientationPeaks(hist);
    console.log(`orientation peaks (W + S): ${both.join(", ")} (smoothed shares: ${peakShares(hist)})`);
    expect(both.some((o) => Math.abs(o - 90) <= 10), both.join(",")).toBe(true); // vertical: from the west
    expect(both.some((o) => o <= 10 || o >= 170), both.join(",")).toBe(true); // horizontal: from the south
    expect(mock.requests.some((r) => r.startsWith("swell2_dir@"))).toBe(true);
    const ctx = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 } });
    try {
      const p2 = await ctx.newPage();
      const { mock: m2 } = await openWithSwells(p2, { primary: [2.5, 13, 270] }, "primary");
      const one = orientationPeaks((await waveImage(p2)).hist);
      console.log(`orientation peaks (W only): ${one.join(", ")}`);
      expect(one.every((o) => Math.abs(o - 90) <= 10), one.join(",")).toBe(true);
      expect(one.length).toBeGreaterThan(0);
      // The secondary swell is not in the index, so it is never requested (never made up).
      expect(m2.requests.some((r) => r.startsWith("swell2"))).toBe(false);
    } finally {
      await ctx.close();
    }
  });

  test("the wave-system chooser: the combined sea by default, partitions, systems the run lacks disabled", async ({ page }) => {
    // The default (D11, lead decision 3): the combined sea, even when the run has partitions,
    // whose fields are then not downloaded.
    const { mock } = await openWithSwells(page, { primary: [2.5, 13, 250], windsea: [1.4, 6, 300] }, "combined");
    await expect(wave(page)).toHaveAttribute("data-families", "combined");
    expect(mock.requests.some((r) => r.startsWith("swell1") || r.startsWith("windsea"))).toBe(false);
    await page.getByRole("button", { name: "Settings" }).click();
    const chooser = page.getByTestId("wave-systems");
    await expect(chooser).toBeVisible();
    await expect(chooser.getByRole("button", { name: /^Combined sea/ })).toHaveAttribute("aria-pressed", "true");
    await chooser.getByRole("button", { name: /^Primary swell/ }).click();
    await expect(wave(page)).toHaveAttribute("data-families", "primary");
    await expect(chooser.getByRole("button", { name: /^Combined sea/ })).toHaveAttribute("aria-pressed", "false");
    const secondary = chooser.getByRole("button", { name: /Secondary swell/ });
    await expect(secondary).toBeDisabled();
    await expect(secondary).toHaveAttribute("title", "Not available for this model run");
    await expect(chooser).toContainText("Not available for this model run: Secondary swell.");
    await chooser.getByRole("button", { name: /^Wind sea/ }).click();
    await expect(wave(page)).toHaveAttribute("data-families", "windsea,primary");
    await chooser.getByRole("button", { name: /^Primary swell/ }).click();
    await expect(wave(page)).toHaveAttribute("data-families", "windsea");
    await expect.poll(() => mock.requests.some((r) => r.startsWith("windsea_dir@"))).toBe(true);
    await expect(chooser.getByRole("button", { name: /^Wind sea/ })).toHaveAttribute("aria-pressed", "true");
    await shot(page, "desktop-1440x900-chooser-windsea");
    await chooser.getByRole("button", { name: /^Combined sea/ }).click();
    await expect(wave(page)).toHaveAttribute("data-families", "combined");
    await page.keyboard.press("Escape");
    expect(mock.requests.some((r) => r.startsWith("swell2"))).toBe(false);
  });

  test("without partitions the combined sea is drawn and the chooser is hidden", async ({ page }) => {
    await openMapWithFields(page, { fields: UNIFORM_FLOW });
    await expect(wave(page)).toHaveAttribute("data-families", "combined");
    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByTestId("map-settings")).toBeVisible();
    await expect(page.getByTestId("wave-systems")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-crests")).toContainText("Combined sea (peak)");
    await expect(page.getByTestId("legend-crests")).toContainText("One combined sea");
  });

  test("crests stop at the coast: nothing 30 km inland of Perth at zoom 7, on both basemaps", async ({ browser, baseURL }) => {
    // 31.99°S 116.07°E is 30 km east of the coast at Cottesloe (115.75°E).
    const ctx = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, geolocation: { latitude: -31.99, longitude: 116.07 }, permissions: ["geolocation"] });
    try {
      const page = await ctx.newPage();
      await openWithSwells(page, { primary: [3, 12, 250] }, "primary");
      await page.getByRole("button", { name: "Show my location" }).click();
      await expect(field(page)).toHaveAttribute("data-zoom", "7");
      await expect(wave(page)).toHaveAttribute("data-view", /,32768$/);
      for (const basemap of ["Dark", "Streets"]) {
        if (basemap === "Streets") {
          await page.getByRole("button", { name: "Settings" }).click();
          await page.getByTestId("map-settings").getByRole("radio", { name: "Streets" }).click();
          await page.keyboard.press("Escape");
        }
        await page.waitForTimeout(1500);
        let offshore = 0;
        for (let i = 0; i < 10; i++) {
          expect(await waveAlphaNear(page, -31.99, 116.07, 4), `${basemap}: 30 km inland`).toBe(0);
          offshore = Math.max(offshore, await waveAlphaNear(page, -31.99, 115.2, 40));
          await page.waitForTimeout(250);
        }
        expect(offshore, `${basemap}: 50 km offshore`).toBeGreaterThan(0);
        await shot(page, `perth-zoom7-${basemap.toLowerCase()}`);
      }
    } finally {
      await ctx.close();
    }
  });

  test("the wind shows over land on the Wind layer; wave layers keep opaque land", async ({ page }) => {
    await openMapWithFields(page, { fields: UNIFORM_FLOW });
    const p = await pagePoint(page, 23, 10); // the Sahara
    const grey = (px: Array<[number, number, number]>) => Math.max(...([0, 1, 2] as const).map((ch) => Math.abs(median(px.map((q) => q[ch])) - LAND_RGB[ch])));
    expect(grey(await pixels(page, p.x, p.y)), "opaque land on Wave Height").toBeLessThan(10);
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land-alpha", "translucent");
    await expect.poll(async () => grey(await pixels(page, p.x, p.y)), { message: "wind colour through the land" }).toBeGreaterThan(12);
    await shot(page, "desktop-1440x900-wind-over-land");
  });

  test("the wind overlay on a wave layer downloads the wind and draws particles over the crests", async ({ page }) => {
    const { mock, now } = await openWithSwells(page, { primary: [2.5, 13, 250], secondary: [1.5, 10, 190] }, "secondary,primary");
    expect(requested(mock, "wind_u", now)).toBe(false);
    await expect(page.locator("canvas.map-particle-canvas")).toHaveCount(0);
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByTestId("map-settings").getByRole("switch", { name: /Wind on wave layers/ }).click();
    await page.keyboard.press("Escape");
    await expect.poll(() => requested(mock, "wind_u", now) && requested(mock, "wind_v", now)).toBe(true);
    await expect(page.locator("canvas.map-particle-canvas")).toHaveAttribute("data-mode", "animated");
    await expect(wave(page)).toHaveAttribute("data-mode", "animated");
    await expect(field(page)).toHaveAttribute("data-layer", "hs");
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-wind")).toContainText("White streaks drift with the wind");
    await expect(page.getByTestId("legend-crests")).toContainText("Primary swell");
    await shot(page, "desktop-1440x900-wind-overlay");
  });

  test("the legend states units, source, model cycle, valid time, scales and the arrow convention", async ({ page }) => {
    await openWithSwells(page, { primary: [2.5, 13, 250], secondary: [1.5, 10, 190] }, "secondary,primary");
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    const legend = page.getByTestId("legend-drawer");
    await expect(legend.getByTestId("legend-title")).toHaveText("Offshore wave height, Hs (ft)");
    await expect(legend.getByTestId("legend-crests")).toContainText("Primary swell (light tinted blobs)");
    await expect(legend.getByTestId("legend-crests")).toContainText("Secondary swell (split amber bars)");
    await expect(legend.getByTestId("legend-crests")).toContainText("Short dashes are wave crests moving with the waves");
    await expect(legend.getByTestId("legend-scales")).toHaveText(/^Crest spacing ×[\d,]+ at the map centre; time runs 4× faster than real\.$/);
    await expect(legend.getByTestId("legend-crests")).toContainText("Stylised crests from the forecast model, not individual waves.");
    await expect(legend.getByTestId("legend-convention")).toContainText("Arrows point where the wind or waves are going.");
    await expect(legend.getByTestId("legend-source")).toContainText("Model cycle");
    await expect(legend.getByTestId("legend-valid")).toContainText(/Valid .+ · [+-]?\d+ h \(forecast, not observed\)|analysis/);
    // Units: mph is offered and shared with the spot charts through swell.windUnit.
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    await expect(legend.getByTestId("legend-title")).toHaveText("Wind speed (km/h)");
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByTestId("map-settings").getByRole("radio", { name: "mph", exact: true }).click();
    await page.keyboard.press("Escape");
    await expect(legend.getByTestId("legend-title")).toHaveText("Wind speed (mph)");
    expect(await page.evaluate(() => window.localStorage.getItem("swell.windUnit"))).toBe("mph");
    await shot(page, "desktop-1440x900-legend-wind-mph");
  });

  test("the readout lists the wave systems under the cursor", async ({ page }) => {
    await openWithSwells(page, { primary: [2.5, 13, 250], windsea: [1.2, 6, 300] }, "primary");
    // The readout lists the systems on screen: add the wind sea.
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByTestId("wave-systems").getByRole("button", { name: /^Wind sea/ }).click();
    await page.keyboard.press("Escape");
    await expect(wave(page)).toHaveAttribute("data-families", "windsea,primary");
    const p = await pagePoint(page, -50, -120);
    await page.mouse.move(p.x, p.y);
    const readout = page.getByTestId("map-readout");
    // (directions are sent in 1.5° steps, so 250° arrives as 250.5°)
    await expect(readout).toContainText(/Primary 8\.2 ft 13 s from WSW \(25[01]°\)/);
    await expect(readout).toContainText(/Wind sea 3\.9 ft 6 s from WNW \(300°\)/);
  });

  test("a field served for another valid time is never shown: the map re-reads the index", async ({ page }) => {
    // The SYNTHETIC server answers every request for hs with the next step's field.
    let shifted = false;
    const s = swells({ primary: [2.5, 13, 250] });
    const mock = await mockFields(page, {
      fields: s.fields,
      variables: s.variables,
      servedMeta: (variable, at) => (shifted && variable === "hs" ? { valid_time: new Date(Date.parse(at) + 3 * 3_600_000).toISOString() } : null),
    });
    await openMap(page);
    await expect.poll(() => painted(page)).toBeGreaterThan(10_000);
    const reads = mock.indexReads;
    shifted = true;
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await slider.focus();
    await page.keyboard.press("End"); // a step not downloaded yet
    const last = mock.validTimes[mock.validTimes.length - 1]!;
    await expect.poll(() => requested(mock, "hs", last)).toBe(true);
    await expect.poll(() => mock.indexReads, { message: "index re-read after the mismatch" }).toBeGreaterThan(reads);
    await expect(field(page)).not.toHaveAttribute("data-at", last);
  });
});

/** Wave crest dash checks that also run on the Pixel 7 project (no fixed viewport). */
test.describe("wave crest dashes on any screen (synthetic partitions)", () => {
  test("dashes of a swell from the SW run NW-SE and travel up-right (to the NE)", { tag: "@mobile" }, async ({ page }) => {
    await openWithSwells(page, { primary: [2.5, 12, 225] }, "primary");
    const img = await waveImage(page);
    console.log(`orientation for a swell from 225°: ${img.orientation.toFixed(1)}° (coherence ${img.coherence.toFixed(2)})`);
    test.info().annotations.push({ type: "crest orientation, swell from 225°", description: `${img.orientation.toFixed(1)}°, coherence ${img.coherence.toFixed(2)}` });
    expect(Math.abs(img.orientation - 45), JSON.stringify({ o: img.orientation, c: img.coherence })).toBeLessThan(8);
    expect(img.coherence).toBeGreaterThan(0.5);
    // Motion: the ink profile along the travel direction (x east, y down: (0.707, -0.707))
    // shifts forward between two frames; a crest of a 12 s swell moves 27 px x 4 / 12 s = 9 px/s.
    const a = await inkProfile(page, Math.SQRT1_2, -Math.SQRT1_2);
    const t0 = Date.now();
    await page.waitForTimeout(500);
    const b = await inkProfile(page, Math.SQRT1_2, -Math.SQRT1_2);
    const dt = (Date.now() - t0) / 1000;
    const shift = shiftOf(a.prof, b.prof, Math.round(10 * a.scale)) / a.scale;
    console.log(`crest motion along the travel direction: ${shift.toFixed(1)} CSS px in ${dt.toFixed(2)} s (expected about ${(9 * dt).toFixed(1)})`);
    expect(shift).toBeGreaterThan(1.5);
    expect(shift).toBeLessThan(9 * dt * 2.5);
    await shot(page, "desktop-1440x900-crests-from-sw");
  });

  test("dashes stay off land on the dark and the street basemap (land probes)", { tag: "@mobile" }, async ({ page }) => {
    // Uniform SYNTHETIC swell everywhere, land included: only the coastline clip keeps it off land.
    await openWithSwells(page, { primary: [3, 12, 250] }, "primary");
    const LAND = [
      [-25, 135, "central Australia"],
      [40, -100, "central United States"],
      [23, 10, "Sahara"],
      [5, 22, "central Africa"],
      [50, 60, "Kazakhstan"],
    ] as const;
    const OCEAN = [
      [-45, -120],
      [-40, 60],
      [-40, -5],
    ] as const;
    const probe = async (label: string) => {
      await page.waitForTimeout(2000);
      let ocean = 0;
      let landInView = 0;
      for (let i = 0; i < 8; i++) {
        for (const [lat, lon, name] of LAND) {
          const a = await waveAlphaNear(page, lat, lon);
          if (a < 0) continue; // not in this view (a phone shows less of the world)
          landInView++;
          expect(a, `${label}: ${name}`).toBe(0);
        }
        for (const [lat, lon] of OCEAN) ocean = Math.max(ocean, await waveAlphaNear(page, lat, lon, 30));
        await page.waitForTimeout(250);
      }
      expect(landInView, `${label}: land probes in view`).toBeGreaterThanOrEqual(8 * 2);
      expect(ocean, `${label}: open ocean`).toBeGreaterThan(0);
    };
    // Dark: the opaque land above the crests clips them; the crest canvas skips the composite.
    await expect(wave(page)).toHaveAttribute("data-ocean-mask", "0");
    await probe("dark");
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByTestId("map-settings").getByRole("radio", { name: "Streets" }).click();
    await page.keyboard.press("Escape");
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "0");
    await expect(wave(page)).toHaveAttribute("data-ocean-mask", "1");
    await probe("streets");
  });

  test("reduced motion: still dashes with arrowheads, unchanged over a second", { tag: "@mobile" }, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openWithSwells(page, { primary: [2.5, 12, 225] }, "primary");
    await expect(wave(page)).toHaveAttribute("data-mode", "static");
    expect(Number(await wave(page).getAttribute("data-arrows"))).toBeGreaterThan(3); // D11 (was > 0)
    const a = await waveImage(page);
    expect(a.ink).toBeGreaterThan(0);
    expect(Math.abs(a.orientation - 45), JSON.stringify({ o: a.orientation })).toBeLessThan(8);
    await page.waitForTimeout(1000);
    expect((await waveImage(page)).hash).toBe(a.hash);
  });
});

test.describe("map on a phone (390 × 844)", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });
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
