import { describe, expect, it } from "vitest";

import {
  compass,
  formatCalendarDate,
  formatDuration,
  formatMoney,
  formatRange,
  fromUnits,
  heightRange,
  leadLabel,
  toUnits,
} from "@/lib/format";

describe("units", () => {
  it("converts feet and metres both ways", () => {
    expect(toUnits(10, "ft")).toBe(10);
    expect(toUnits(3.28084, "m")).toBeCloseTo(1, 5);
    expect(fromUnits(2, "m")).toBeCloseTo(6.56168, 4);
  });

  it("formats breaking height ranges in the user's units", () => {
    expect(heightRange(6, 9)).toBe("6–9 ft");
    expect(heightRange(6.2, 8.8)).toBe("6–9 ft");
    expect(heightRange(6.56, 9.84, "m")).toBe("2–3 m");
  });
});

describe("dates and times", () => {
  it("formats ranges in the spot's local calendar, end-exclusive", () => {
    // 2026-07-12 06:00 SAST to 2026-07-14 17:00 SAST
    expect(formatRange("2026-07-12T04:00:00Z", "2026-07-14T15:00:00Z", "Africa/Johannesburg")).toBe("Jul 12–14");
    expect(formatRange("2026-07-30T04:00:00Z", "2026-08-02T15:00:00Z", "Africa/Johannesburg")).toBe("Jul 30 – Aug 2");
  });

  it("handles the international date line", () => {
    // 2026-08-09T20:00Z is already Aug 10 in Fiji (UTC+12) but still Aug 9 in Honolulu.
    expect(formatRange("2026-08-09T20:00:00Z", "2026-08-09T22:00:00Z", "Pacific/Fiji")).toBe("Aug 10");
    expect(formatRange("2026-08-09T20:00:00Z", "2026-08-09T22:00:00Z", "Pacific/Honolulu")).toBe("Aug 9");
  });

  it("renders calendar dates without timezone shifting", () => {
    expect(formatCalendarDate("2026-07-10")).toBe("Jul 10");
    expect(formatCalendarDate(null)).toBe("—");
  });

  it("formats durations and lead time", () => {
    expect(formatDuration(1585)).toBe("26h 25m");
    expect(formatDuration(120)).toBe("2h");
    expect(leadLabel(7.2)).toBe("in 7 days");
    expect(leadLabel(-0.5)).toBe("in progress");
  });
});

describe("misc", () => {
  it("maps compass points", () => {
    expect(compass(0)).toBe("N");
    expect(compass(225)).toBe("SW");
    expect(compass(359)).toBe("N");
    expect(compass(null)).toBe("—");
  });

  it("formats money with the currency", () => {
    expect(formatMoney("780.00", "USD")).toBe("$780");
    expect(formatMoney(1234.5, "EUR")).toBe("€1,235");
    expect(formatMoney(null, "USD")).toBe("—");
  });
});
