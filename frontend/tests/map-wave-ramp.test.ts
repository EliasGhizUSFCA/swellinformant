import { describe, expect, it } from "vitest";

import { buildLut, colorAt, colorAtValue, hexToRgb, LAYER_SCALES, LUT_SIZE, metresToStops, WAVE_HEIGHT_RAMP } from "@/lib/map-colors";
import { renderField } from "@/lib/map-render";

/** CIE L*a*b* of an sRGB colour (D65 white, 2° observer). */
function lab([r, g, b]: [number, number, number]): [number, number, number] {
  const lin = (c: number) => {
    const v = c / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  const [R, G, B] = [lin(r), lin(g), lin(b)];
  const X = (0.4124564 * R + 0.3575761 * G + 0.1804375 * B) / 0.95047;
  const Y = 0.2126729 * R + 0.7151522 * G + 0.072175 * B;
  const Z = (0.0193339 * R + 0.119192 * G + 0.9503041 * B) / 1.08883;
  const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : ((24389 / 27) * t + 16) / 116);
  return [116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))];
}
const deltaE = (a: [number, number, number], b: [number, number, number]) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

describe("wave-height colour ramp (metres)", () => {
  it("has the D11 anchors, exactly", () => {
    expect(WAVE_HEIGHT_RAMP.map((a) => [a.value, a.color])).toEqual([
      [0, "#0c1846"],
      [0.5, "#132e78"],
      [1, "#1b48a6"],
      [1.5, "#2a5cc2"],
      [2, "#4363cf"],
      [2.5, "#5a62d6"],
      [3, "#7360d6"],
      [4, "#925ad0"],
      [5, "#ad55c4"],
      [6, "#c556b4"],
      [7, "#d860a6"],
      [8, "#e6729f"],
      [9, "#f19ebd"],
      [10, "#f9d0e0"],
    ]);
    // The Wave Height layer uses it, at its metre positions on the 0-10 m scale; the other layers keep the shared ramp.
    expect(LAYER_SCALES.hs.stops.map((s) => [Math.round(s.at * 1000) / 1000, s.color])).toEqual(WAVE_HEIGHT_RAMP.map((a) => [a.value / 10, a.color]));
    expect(LAYER_SCALES.tp.stops).not.toBe(LAYER_SCALES.hs.stops);
  });

  it("gets lighter with every anchor (CIE L* strictly increasing) and neighbours differ by ΔE ≥ 5", () => {
    const labs = WAVE_HEIGHT_RAMP.map((a) => lab(hexToRgb(a.color)));
    for (let i = 1; i < labs.length; i++) {
      expect(labs[i]![0], `L* at ${WAVE_HEIGHT_RAMP[i]!.value} m`).toBeGreaterThan(labs[i - 1]![0]);
      expect(deltaE(labs[i]!, labs[i - 1]!), `ΔE ${WAVE_HEIGHT_RAMP[i - 1]!.value}-${WAVE_HEIGHT_RAMP[i]!.value} m`).toBeGreaterThanOrEqual(5);
    }
    expect(labs[0]![0]).toBeCloseTo(10.4, 1);
    expect(labs.at(-1)![0]).toBeCloseTo(87.3, 1);
    // Also between anchors, sampled every 0.1 m: never darker for a larger sea.
    let prev = -1;
    for (let m = 0; m <= 10.001; m += 0.1) {
      const L = lab(colorAtValue(WAVE_HEIGHT_RAMP, m))[0];
      expect(L).toBeGreaterThan(prev - 0.35); // rounding to whole RGB values only
      prev = L;
    }
  });

  it("interpolates linearly in metres and saturates beyond the ends", () => {
    const mid = colorAtValue(WAVE_HEIGHT_RAMP, 3.5); // half way between 3 m and 4 m
    const [a, b] = [hexToRgb("#7360d6"), hexToRgb("#925ad0")];
    for (let c = 0; c < 3; c++) expect(Math.abs(mid[c]! - (a[c]! + b[c]!) / 2)).toBeLessThanOrEqual(0.5);
    expect(colorAtValue(WAVE_HEIGHT_RAMP, 14)).toEqual(hexToRgb("#f9d0e0"));
    expect(colorAtValue(WAVE_HEIGHT_RAMP, -1)).toEqual(hexToRgb("#0c1846"));
    expect(colorAtValue(WAVE_HEIGHT_RAMP, NaN)).toEqual(hexToRgb("#0c1846"));
    // A 0-12 m scale: the anchors keep their metres, the last 2 m saturate.
    const wide = metresToStops(WAVE_HEIGHT_RAMP, 0, 12);
    expect(wide[0]).toEqual({ at: 0, color: "#0c1846" });
    expect(wide.at(-1)).toEqual({ at: 1, color: "#f9d0e0" });
    expect(colorAt(wide, 4 / 12)).toEqual(hexToRgb("#925ad0"));
    // A 1-9 m scale starts and ends on interpolated colours.
    const narrow = metresToStops(WAVE_HEIGHT_RAMP, 1.25, 9);
    expect(narrow[0]!.color).toBe("#2352b4");
    expect(narrow.at(-1)!.color).toBe("#f19ebd");
  });

  it("builds a lookup table with the ramp's end colours and paints a field above 10 m in the top colour", () => {
    const lut = buildLut(LAYER_SCALES.hs.stops);
    expect(lut.length).toBe(LUT_SIZE * 4);
    expect(Array.from(lut.slice(0, 3))).toEqual(hexToRgb("#0c1846"));
    expect(Array.from(lut.slice(-4, -1))).toEqual(hexToRgb("#f9d0e0"));
    // SYNTHETIC 1° grid: 14 m everywhere (beyond the scale) paints the saturated colour.
    const nx = 360;
    const ny = 181;
    const grid = { nx, ny, lat0: 90, lon0: -180, dlat: 1, dlon: 1, values: new Float32Array(nx * ny).fill(14) };
    const out = { width: 4, height: 4, data: new Uint8ClampedArray(64) };
    renderField(out, { originX: 100, originY: 100, width: 8, height: 8, worldPx: 512 }, 2, { grid, scale: LAYER_SCALES.hs, lut, minWeight: 0.5, alpha: 255 });
    expect(Array.from(out.data.slice(0, 3))).toEqual(hexToRgb("#f9d0e0"));
  });
});
