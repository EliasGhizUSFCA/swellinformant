import { describe, expect, it } from "vitest";

import { buildLut, colorAt, cssGradient, hexToRgb, LAYER_SCALES, LUT_SIZE, RAMP, scalePosition } from "@/lib/map-colors";
import type { FieldIndex } from "@/lib/map-fields";
import { formatLayerValue, layerScale, legendTicks, readout, toDisplay, type DisplayUnits } from "@/lib/map-layers";

const M: DisplayUnits = { height: "m", wind: "m/s" };
const FT: DisplayUnits = { height: "ft", wind: "km/h" };

describe("colour scales", () => {
  it("uses the documented display ranges", () => {
    expect([LAYER_SCALES.hs.min, LAYER_SCALES.hs.max]).toEqual([0, 10]);
    expect([LAYER_SCALES.tp.min, LAYER_SCALES.tp.max]).toEqual([0, 20]);
    expect([LAYER_SCALES.wind.min, LAYER_SCALES.wind.max]).toEqual([0, 30]);
    expect([LAYER_SCALES.energy.min, LAYER_SCALES.energy.max]).toEqual([0, 150]);
  });

  it("saturates beyond the range and keeps NaN", () => {
    expect(scalePosition(LAYER_SCALES.hs, -3)).toBe(0);
    expect(scalePosition(LAYER_SCALES.hs, 25)).toBe(1);
    expect(scalePosition(LAYER_SCALES.hs, 5)).toBeCloseTo(0.5);
    expect(scalePosition(LAYER_SCALES.hs, NaN)).toBeNaN();
    // wave power uses a square-root scale so typical values are not all deep blue
    expect(scalePosition(LAYER_SCALES.energy, 37.5)).toBeCloseTo(0.5);
  });

  it("runs from deep blue (calm) to magenta (largest)", () => {
    const [r0, , b0] = colorAt(RAMP, 0);
    const [r1, g1, b1] = colorAt(RAMP, 1);
    expect(b0).toBeGreaterThan(r0);
    expect(r1).toBeGreaterThan(g1);
    expect(b1).toBeGreaterThan(g1);
    expect(colorAt(RAMP, 2)).toEqual(colorAt(RAMP, 1));
    expect(colorAt(RAMP, -1)).toEqual(hexToRgb(RAMP[0]!.color));
  });

  it("builds an opaque RGBA lookup table with the ramp's end colours", () => {
    const lut = buildLut(RAMP);
    expect(lut.length).toBe(LUT_SIZE * 4);
    expect(Array.from(lut.slice(0, 3))).toEqual(hexToRgb(RAMP[0]!.color));
    expect(Array.from(lut.slice(-4, -1))).toEqual(hexToRgb(RAMP[RAMP.length - 1]!.color));
    expect(lut[3]).toBe(255);
    expect(cssGradient(RAMP)).toMatch(/^linear-gradient\(to right, #1c2a6e 0%/);
    expect(() => hexToRgb("red")).toThrow();
  });

  it("takes the hs / tp display range from the field index when given", () => {
    const index = {
      available: true,
      enabled: true,
      valid_times: [],
      variables: [{ name: "hs", label: "Hs", unit: "m", display_min: 0, display_max: 12, group: "core" }],
      stale_after_hours: 12,
      stale: false,
    } as FieldIndex;
    expect(layerScale("hs", index).max).toBe(12);
    // The wave-height colours stay anchored in metres: 10 m keeps its colour on a 0-12 m scale.
    const wide = layerScale("hs", index);
    expect(colorAt(wide.stops, 10 / 12)).toEqual(hexToRgb("#f3c4dc"));
    expect(colorAt(wide.stops, 5 / 12)).toEqual(hexToRgb("#b856a8"));
    expect(layerScale("tp", index).max).toBe(20);
    expect(layerScale("wind", index).max).toBe(30);
  });
});

describe("units, ticks and readout", () => {
  it("converts to the user's units", () => {
    expect(toDisplay("hs", 1, FT)).toBeCloseTo(3.28084);
    expect(toDisplay("wind", 10, FT)).toBeCloseTo(36);
    expect(toDisplay("wind", 10, { height: "m", wind: "kn" })).toBeCloseTo(19.43844);
    expect(formatLayerValue("hs", 2.44, M)).toBe("2.4 m");
    expect(formatLayerValue("hs", 2.44, FT)).toBe("8.0 ft");
    expect(formatLayerValue("hs", 4, FT)).toBe("13 ft");
    expect(formatLayerValue("wind", 10, FT)).toBe("36 km/h");
    expect(formatLayerValue("energy", 45.4, M)).toBe("45 kW/m");
    expect(formatLayerValue("tp", NaN, M)).toBe("—");
    // Periods are stored up to byte 254 = 25.4 s: that value is a lower bound, not 25 s.
    expect(formatLayerValue("tp", 25.3, M)).toBe("25 s");
    expect(formatLayerValue("tp", 25.4, M)).toBe("≥25.4 s");
  });

  it("puts legend ticks in the user's units at matching positions", () => {
    // Wave height: the metre ticks sit on the colour ramp's anchors (D11), feet every 5 ft.
    const m = legendTicks("hs", LAYER_SCALES.hs, M);
    expect(m.map((t) => t.label)).toEqual(["0", "1", "2", "3", "4", "6", "8", "10"]);
    expect(m[1]!.pos).toBeCloseTo(0.1);
    expect(m[5]!.pos).toBeCloseTo(0.6);
    const ft = legendTicks("hs", LAYER_SCALES.hs, FT);
    expect(ft.map((t) => t.label)).toEqual(["0", "5", "10", "15", "20", "25", "30"]);
    expect(ft[1]!.pos).toBeCloseTo(5 / 3.28084 / 10);
    expect(ft[2]!.pos).toBeCloseTo(10 / 3.28084 / 10);
    expect(legendTicks("wind", LAYER_SCALES.wind, FT).map((t) => t.label)).toEqual(["0", "20", "40", "60", "80", "100"]);
    expect(legendTicks("wind", LAYER_SCALES.wind, { height: "m", wind: "kn" }).map((t) => t.label)).toEqual(["0", "10", "20", "30", "40", "50"]);
    const e = legendTicks("energy", LAYER_SCALES.energy, M);
    expect(e.at(-1)).toEqual({ label: "150", pos: 1 });
    expect(e.find((t) => t.label === "50")!.pos).toBeCloseTo(Math.sqrt(1 / 3));
  });

  it("describes the point under the cursor, labelled as offshore data", () => {
    const s = { hs: 2.4, tp: 12.2, waveDir: 247, energy: 30, windSpeed: 10, windDir: 315, pressure: 1012.4 };
    expect(readout("hs", s, M)).toEqual({ title: "Offshore waves (Hs)", text: "2.4 m · 12 s · from WSW · 1012 hPa" });
    expect(readout("tp", s, M)!.text).toBe("12 s · Hs 2.4 m · from WSW · 1012 hPa");
    expect(readout("energy", s, M)!.title).toBe("Offshore wave power");
    expect(readout("wind", { windSpeed: 10, windDir: 315 }, FT)).toEqual({ title: "Wind", text: "36 km/h · from NW" });
    expect(readout("hs", { hs: NaN, tp: 10 }, M)).toBeNull();
    expect(readout("wind", {}, M)).toBeNull();
    expect(readout("hs", { hs: 1, waveDir: NaN }, M)!.text).toBe("1.0 m");
  });
});
