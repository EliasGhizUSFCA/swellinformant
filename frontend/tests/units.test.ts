import { describe, expect, it } from "vitest";

import { heightRange } from "@/lib/format";
import { convertHeightFt, convertWind, formatHeightRange, formatWind } from "@/lib/units";

describe("wind units", () => {
  it("converts km/h with the exact factors", () => {
    expect(convertWind(36, "km/h")).toBe(36);
    expect(convertWind(36, "m/s")).toBeCloseTo(10, 12);
    expect(convertWind(1.852, "kn")).toBeCloseTo(1, 12);
    expect(convertWind(1.609344, "mph")).toBeCloseTo(1, 12);
    expect(convertWind(100, "mph")).toBeCloseTo(62.137, 3);
  });

  it("formats with the unit, and a dash without a value", () => {
    expect(formatWind(14.4, "km/h")).toBe("14 km/h");
    expect(formatWind(14.4, "m/s")).toBe("4.0 m/s");
    expect(formatWind(14.4, "mph")).toBe("9 mph");
    expect(formatWind(14.4, "kn", 1)).toBe("7.8 kn");
    expect(formatWind(null, "kn")).toBe("—");
    expect(formatWind(undefined, "mph")).toBe("—");
    expect(formatWind(Number.NaN, "km/h")).toBe("—");
  });
});

describe("height units", () => {
  it("converts feet with 1 ft = 0.3048 m", () => {
    expect(convertHeightFt(10, "ft")).toBe(10);
    expect(convertHeightFt(10, "m")).toBeCloseTo(3.048, 12);
  });

  it("formats ranges like heightRange", () => {
    expect(formatHeightRange(5, 7, "ft")).toBe("5–7 ft");
    expect(formatHeightRange(5.2, 6.8, "ft")).toBe("5–7 ft");
    expect(formatHeightRange(5, 7, "m")).toBe("1.5–2.1 m");
    expect(formatHeightRange(1.4, 1.6, "ft")).toBe("1.5 ft");
    for (const [a, b] of [
      [3, 5],
      [6.2, 8.8],
      [2.4, 3.1],
      [10, 15],
    ] as const) {
      expect(formatHeightRange(a, b, "ft")).toBe(heightRange(a, b, "ft"));
      expect(formatHeightRange(a, b, "m")).toBe(heightRange(a, b, "m"));
    }
  });
});
