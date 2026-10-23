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
  it("has its stated anchors, exactly", () => {
    expect(WAVE_HEIGHT_RAMP.map((a) => [a.value, a.color])).toEqual([
      [0, "#151d42"],
      [0.5, "#212f68"],
      [1, "#344790"],
      [1.5, "#4a4ea6"],
      [2, "#614fb0"],
      [2.5, "#7a4eb2"],
      [3, "#8f4db0"],
      [4, "#a64fab"],
      [5, "#b856a8"],
      [6, "#c862a9"],
      [7, "#d572ae"],
      [8, "#df86b8"],
      [9, "#e9a3c7"],
      [10, "#f3c4dc"],
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
    expect(labs[0]![0]).toBeCloseTo(12.2, 1);
    expect(labs.at(-1)![0]).toBeCloseTo(83.8, 1);
    // Every step of the scale is a visible step in lightness too (L* up by 2 or more).
    for (let i = 1; i < labs.length; i++) expect(labs[i]![0] - labs[i - 1]![0], `ΔL* at ${WAVE_HEIGHT_RAMP[i]!.value} m`).toBeGreaterThanOrEqual(2);
    // Also between anchors, sampled every 0.1 m: never darker for a larger sea.
    let prev = -1;
    for (let m = 0; m <= 10.001; m += 0.1) {
      const L = lab(colorAtValue(WAVE_HEIGHT_RAMP, m))[0];
      expect(L).toBeGreaterThan(prev - 0.35); // rounding to whole RGB values only
      prev = L;
    }
  });

  it("turns from blue through violet and magenta to pink with height, softer than full saturation", () => {
    // CIE hue angle h (degrees, -180..180): about -68 (blue) at 0-1 m, rising steadily through
    // violet (2 m) and magenta-purple (3 m) to pink (about -15) from 8 m.
    const hue = (hex: string) => {
      const [, a, b] = lab(hexToRgb(hex));
      return (Math.atan2(b, a) * 180) / Math.PI;
    };
    const hues = WAVE_HEIGHT_RAMP.map((a) => hue(a.color));
    for (let i = 3; i < hues.length; i++) expect(hues[i]!, `hue at ${WAVE_HEIGHT_RAMP[i]!.value} m`).toBeGreaterThan(hues[i - 1]! - 1.5);
    const at = (m: number) => hues[WAVE_HEIGHT_RAMP.findIndex((a) => a.value === m)]!;
    expect(at(1)).toBeLessThan(-60); // slate blue
    expect(at(2)).toBeGreaterThan(at(1) + 8); // violet
    expect(at(3)).toBeGreaterThan(-50); // magenta-purple
    expect(at(8)).toBeGreaterThan(-25); // pink
    // Muted, not electric: CIE chroma at most 62 anywhere on the scale.
    for (let m = 0; m <= 10.001; m += 0.1) {
      const [, a, b] = lab(colorAtValue(WAVE_HEIGHT_RAMP, m));
      expect(Math.hypot(a, b), `C* at ${m.toFixed(1)} m`).toBeLessThanOrEqual(62);
    }
  });

  it("interpolates linearly in metres and saturates beyond the ends", () => {
    const mid = colorAtValue(WAVE_HEIGHT_RAMP, 3.5); // half way between 3 m and 4 m
    const [a, b] = [hexToRgb("#8f4db0"), hexToRgb("#a64fab")];
    for (let c = 0; c < 3; c++) expect(Math.abs(mid[c]! - (a[c]! + b[c]!) / 2)).toBeLessThanOrEqual(0.5);
    expect(colorAtValue(WAVE_HEIGHT_RAMP, 14)).toEqual(hexToRgb("#f3c4dc"));
    expect(colorAtValue(WAVE_HEIGHT_RAMP, -1)).toEqual(hexToRgb("#151d42"));
    expect(colorAtValue(WAVE_HEIGHT_RAMP, NaN)).toEqual(hexToRgb("#151d42"));
    // A 0-12 m scale: the anchors keep their metres, the last 2 m saturate.
    const wide = metresToStops(WAVE_HEIGHT_RAMP, 0, 12);
    expect(wide[0]).toEqual({ at: 0, color: "#151d42" });
    expect(wide.at(-1)).toEqual({ at: 1, color: "#f3c4dc" });
    expect(colorAt(wide, 4 / 12)).toEqual(hexToRgb("#a64fab"));
    // A 1-9 m scale starts and ends on interpolated colours.
    const narrow = metresToStops(WAVE_HEIGHT_RAMP, 1.25, 9);
    expect(narrow[0]!.color).toBe("#3f4b9b");
    expect(narrow.at(-1)!.color).toBe("#e9a3c7");
  });

  it("builds a lookup table with the ramp's end colours and paints a field above 10 m in the top colour", () => {
    const lut = buildLut(LAYER_SCALES.hs.stops);
    expect(lut.length).toBe(LUT_SIZE * 4);
    expect(Array.from(lut.slice(0, 3))).toEqual(hexToRgb("#151d42"));
    expect(Array.from(lut.slice(-4, -1))).toEqual(hexToRgb("#f3c4dc"));
    // SYNTHETIC 1° grid: 14 m everywhere (beyond the scale) paints the saturated colour.
    const nx = 360;
    const ny = 181;
    const grid = { nx, ny, lat0: 90, lon0: -180, dlat: 1, dlon: 1, values: new Float32Array(nx * ny).fill(14) };
    const out = { width: 4, height: 4, data: new Uint8ClampedArray(64) };
    renderField(out, { originX: 100, originY: 100, width: 8, height: 8, worldPx: 512 }, 2, { grid, scale: LAYER_SCALES.hs, lut, minWeight: 0.5, alpha: 255 });
    expect(Array.from(out.data.slice(0, 3))).toEqual(hexToRgb("#f3c4dc"));
  });
});
