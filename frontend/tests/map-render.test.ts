import { describe, expect, it } from "vitest";

import { buildLut, LAYER_SCALES, RAMP } from "@/lib/map-colors";
import type { Grid } from "@/lib/map-fields";
import { mercLat, mercLon } from "@/lib/map-geo";
import { renderField } from "@/lib/map-render";
import { DEFAULT_SETTINGS, parseSettings, parseWaveSystems, readSettings, SETTINGS_KEY, WIND_UNIT_EVENT, WIND_UNIT_KEY, writeSettings } from "@/lib/map-settings";

/** SYNTHETIC global 1° field: value = |lat| / 9 m, no data over a box (a fake "continent"). */
function synthetic(): Grid {
  const nx = 360;
  const ny = 181;
  const values = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const lat = 90 - j;
      const lon = -180 + i;
      values[j * nx + i] = lon >= 0 && lon <= 40 && lat >= 0 && lat <= 30 ? NaN : Math.abs(lat) / 9;
    }
  }
  return { nx, ny, lat0: 90, lon0: -180, dlat: 1, dlon: 1, values };
}

const lut = buildLut(RAMP);
const buffer = (w: number, h: number) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) });

describe("field rendering", () => {
  it("paints ocean pixels with the scale colours and leaves no-data transparent", () => {
    const view = { originX: 0, originY: 0, width: 512, height: 512, worldPx: 512 }; // zoom 1, whole world
    const out = buffer(256, 256); // 2 CSS px per pixel
    const painted = renderField(out, view, 2, { grid: synthetic(), scale: LAYER_SCALES.hs, lut, minWeight: 0.5, alpha: 255 });
    expect(painted).toBeGreaterThan(0.9 * 256 * 256);
    const px = (x: number, y: number) => Array.from(out.data.slice((y * 256 + x) * 4, (y * 256 + x) * 4 + 4));
    // near the equator: the LUT colour of the bilinear value |lat| / 9 at that row
    const row = 128;
    const lat = mercLat(((row + 0.5) * 2) / 512);
    expect(Math.abs(lat)).toBeLessThan(1);
    const li = Math.round((Math.abs(lat) / 9 / 10) * 255) * 4;
    expect(px(10, row)).toEqual([lut[li], lut[li + 1], lut[li + 2], 255]);
    // inside the no-data box (lon 10..30, lat 10..20): transparent
    const x = Math.round(((20 + 180) / 360) * 256);
    const y = Math.round(((1 - Math.log(Math.tan(Math.PI / 4 + (15 * Math.PI) / 360)) / Math.PI) / 2) * 256);
    expect(mercLon(((x + 0.5) * 2) / 512)).toBeCloseTo(20, 0);
    expect(px(x, y)[3]).toBe(0);
    // high latitude (>= 81° → ≥ 9 m) saturates towards the top of the scale
    const top = px(10, 2);
    expect(top[3]).toBe(255);
    expect(top.slice(0, 3)).not.toEqual([lut[0], lut[1], lut[2]]);
  });

  it("wraps longitude for world copies and honours alpha / minWeight", () => {
    const g = synthetic();
    const a = buffer(64, 8);
    const b = buffer(64, 8);
    const base = { originX: 100, originY: 200, width: 128, height: 16, worldPx: 512 };
    renderField(a, base, 2, { grid: g, scale: LAYER_SCALES.hs, lut, minWeight: 0.5, alpha: 150 });
    renderField(b, { ...base, originX: base.originX + 512 }, 2, { grid: g, scale: LAYER_SCALES.hs, lut, minWeight: 0.5, alpha: 150 });
    expect(Array.from(a.data)).toEqual(Array.from(b.data));
    const alphas = new Set(Array.from(a.data.filter((_, i) => i % 4 === 3)));
    expect([...alphas].every((v) => v === 0 || v === 150)).toBe(true);
    // a low minWeight reaches further into the no-data box than a strict one
    const view = { originX: 256, originY: 160, width: 64, height: 64, worldPx: 512 };
    const strict = renderField(buffer(32, 32), view, 2, { grid: g, scale: LAYER_SCALES.hs, lut, minWeight: 0.5, alpha: 255 });
    const loose = renderField(buffer(32, 32), view, 2, { grid: g, scale: LAYER_SCALES.hs, lut, minWeight: 0.01, alpha: 255 });
    expect(loose).toBeGreaterThan(strict);
  });
});

describe("wind colour = readout value (WINDVIZ-10)", () => {
  it("paints the length of the interpolated (u, v), as the readout shows, not interpolated node speeds", async () => {
    const { sampleGrid } = await import("@/lib/map-fields");
    // SYNTHETIC: u alternates +10 / -10 m/s between 1° columns, v = 4 m/s everywhere.
    const nx = 360;
    const ny = 181;
    const u = new Float32Array(nx * ny);
    for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) u[j * nx + i] = i % 2 ? -10 : 10;
    const ug: Grid = { nx, ny, lat0: 90, lon0: -180, dlat: 1, dlon: 1, values: u };
    const vg: Grid = { ...ug, values: new Float32Array(nx * ny).fill(4) };
    const scale = LAYER_SCALES.wind;
    const windLut = buildLut(scale.stops);
    const view = { originX: 0, originY: 1536, width: 64, height: 64, worldPx: 4096 }; // zoom 4, near the equator
    const out = buffer(64, 64);
    renderField(out, view, 1, { grid: ug, v: vg, scale, lut: windLut, minWeight: 0.5, alpha: 255 });
    for (const [x, y] of [
      [5, 5],
      [20, 40],
      [33, 12],
    ] as const) {
      const lat = mercLat((view.originY + y + 0.5) / view.worldPx);
      const lon = mercLon((view.originX + x + 0.5) / view.worldPx);
      const speed = Math.hypot(sampleGrid(ug, lat, lon), sampleGrid(vg, lat, lon)); // the readout's value
      const t = (speed - scale.min) / (scale.max - scale.min);
      const pos = scale.transform === "sqrt" ? Math.sqrt(Math.max(0, t)) : t;
      const li = Math.round(Math.min(1, Math.max(0, pos)) * 255) * 4;
      const k = (y * 64 + x) * 4;
      for (let c = 0; c < 3; c++) expect(Math.abs(out.data[k + c]! - windLut[li + c]!)).toBeLessThanOrEqual(4);
    }
  });
});

describe("map settings", () => {
  it("keeps only valid stored values", () => {
    expect(parseSettings(null)).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings("not json")).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings("[1,2]").layer).toBe(DEFAULT_SETTINGS.layer);
    // A settings object saved before the wind overlay and wave systems existed (v1) still loads.
    const s = parseSettings(JSON.stringify({ layer: "wind", pressure: true, heightUnits: "m", windUnits: "kn", particles: false, basemap: "streets" }));
    expect(s).toEqual({
      layer: "wind",
      pressure: true,
      heightUnits: "m",
      windUnits: "kn",
      particles: false,
      basemap: "streets",
      windOverlay: false,
      waveSystems: ["primary", "secondary"],
    });
    // mph is a wind unit now (shared with the spot charts); "furlongs/h" is not.
    const bad = parseSettings(JSON.stringify({ layer: "rain", pressure: "yes", heightUnits: "yards", windUnits: "furlongs/h", particles: 1, basemap: "satellite", windOverlay: "on", waveSystems: "all" }));
    expect(bad).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings(JSON.stringify({ windUnits: "mph" })).windUnits).toBe("mph");
  });

  it("parses the wave systems tolerantly", () => {
    expect(parseWaveSystems(["windsea", "bogus", "windsea", "primary"])).toEqual(["windsea", "primary"]);
    expect(parseWaveSystems(["combined"])).toEqual(["combined"]);
    expect(parseWaveSystems(["combined", "secondary"])).toEqual(["secondary"]); // combined is an alternative, not an addition
    expect(parseWaveSystems([])).toEqual(["primary", "secondary"]);
    expect(parseWaveSystems(null)).toEqual(["primary", "secondary"]);
    expect(parseSettings(JSON.stringify({ windOverlay: true, waveSystems: ["windsea"] }))).toMatchObject({ windOverlay: true, waveSystems: ["windsea"] });
  });

  it("shares the wind unit with the spot charts through swell.windUnit", () => {
    window.localStorage.clear();
    // The shared key wins over the map's own copy.
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify({ windUnits: "kn" }));
    window.localStorage.setItem(WIND_UNIT_KEY, "mph");
    expect(readSettings().windUnits).toBe("mph");
    window.localStorage.setItem(WIND_UNIT_KEY, "parsecs");
    expect(readSettings().windUnits).toBe("kn");
    // Writing the map settings writes the shared key and tells the page.
    const events: string[] = [];
    const listen = () => events.push("changed");
    window.addEventListener(WIND_UNIT_EVENT, listen);
    try {
      writeSettings({ ...DEFAULT_SETTINGS, windUnits: "m/s" });
      expect(window.localStorage.getItem(WIND_UNIT_KEY)).toBe("m/s");
      expect(events).toEqual(["changed"]);
      writeSettings({ ...DEFAULT_SETTINGS, windUnits: "m/s", basemap: "streets" });
      expect(events).toEqual(["changed"]); // unchanged unit: no event
    } finally {
      window.removeEventListener(WIND_UNIT_EVENT, listen);
    }
  });

  it("round-trips through localStorage and survives storage errors", () => {
    window.localStorage.clear();
    writeSettings({ ...DEFAULT_SETTINGS, basemap: "streets", windOverlay: true, waveSystems: ["windsea"] });
    expect(JSON.parse(window.localStorage.getItem(SETTINGS_KEY)!).basemap).toBe("streets");
    expect(readSettings()).toMatchObject({ basemap: "streets", windOverlay: true, waveSystems: ["windsea"] });
    const getItem = Storage.prototype.getItem;
    const setItem = Storage.prototype.setItem;
    Storage.prototype.getItem = () => {
      throw new Error("SecurityError");
    };
    Storage.prototype.setItem = () => {
      throw new Error("QuotaExceededError");
    };
    try {
      expect(readSettings()).toEqual(DEFAULT_SETTINGS);
      expect(() => writeSettings(DEFAULT_SETTINGS)).not.toThrow();
    } finally {
      Storage.prototype.getItem = getItem;
      Storage.prototype.setItem = setItem;
    }
  });
});
