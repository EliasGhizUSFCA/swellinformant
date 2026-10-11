import { describe, expect, it } from "vitest";

import { buildLut, LAYER_SCALES, RAMP } from "@/lib/map-colors";
import type { FieldGrid } from "@/lib/map-fields";
import { mercLat, mercLon } from "@/lib/map-geo";
import { renderField } from "@/lib/map-render";
import { parseSettings, DEFAULT_SETTINGS, readSettings, writeSettings, SETTINGS_KEY } from "@/lib/map-settings";

/** SYNTHETIC global 1° field: value = |lat| / 9 m, no data over a box (a fake "continent"). */
function synthetic(): FieldGrid {
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

describe("map settings", () => {
  it("keeps only valid stored values", () => {
    expect(parseSettings(null)).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings("not json")).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings("[1,2]").layer).toBe(DEFAULT_SETTINGS.layer);
    const s = parseSettings(JSON.stringify({ layer: "wind", pressure: true, heightUnits: "m", windUnits: "kn", particles: false, basemap: "streets" }));
    expect(s).toEqual({ layer: "wind", pressure: true, heightUnits: "m", windUnits: "kn", particles: false, basemap: "streets" });
    const bad = parseSettings(JSON.stringify({ layer: "rain", pressure: "yes", heightUnits: "yards", windUnits: "mph", particles: 1, basemap: "satellite" }));
    expect(bad).toEqual(DEFAULT_SETTINGS);
  });

  it("round-trips through localStorage and survives storage errors", () => {
    window.localStorage.clear();
    writeSettings({ ...DEFAULT_SETTINGS, basemap: "streets" });
    expect(JSON.parse(window.localStorage.getItem(SETTINGS_KEY)!).basemap).toBe("streets");
    expect(readSettings().basemap).toBe("streets");
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
