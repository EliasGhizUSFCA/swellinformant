import { describe, expect, it } from "vitest";

import {
  compass,
  formatCalendarDate,
  formatDuration,
  formatHourTick,
  formatMoney,
  formatRange,
  formatSpotDateTime,
  formatSpotTime,
  fromUnits,
  heightRange,
  leadLabel,
  toUnits,
  zoneAbbrev,
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
    expect(heightRange(1.4, 1.6)).toBe("1.5 ft"); // a collapsed range reads as one value
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

describe("spot-local times", () => {
  it("formats a forecast time in the spot's zone, optionally with the zone name", () => {
    expect(formatSpotDateTime("2026-10-15T05:00:00Z", "Africa/Johannesburg")).toBe("Thu, Oct 15, 7:00 AM");
    expect(formatSpotDateTime("2026-10-15T05:00:00Z", "Africa/Johannesburg", { zone: true })).toBe("Thu, Oct 15, 7:00 AM GMT+2");
    expect(formatSpotDateTime("2026-10-15T05:00:00Z", "Africa/Johannesburg", { hour12: false })).toBe("Thu, Oct 15, 07:00");
    expect(formatSpotDateTime("2026-10-15T05:00:00Z", "Africa/Johannesburg", { weekday: "long" })).toBe("Thursday, Oct 15, 7:00 AM");
    expect(formatSpotDateTime("2026-10-15T00:00:00Z", "Pacific/Honolulu")).toBe("Wed, Oct 14, 2:00 PM");
  });

  it("tells the two 1:30 AMs of a DST fall-back night apart by zone name", () => {
    // America/New_York returns to EST at 06:00Z on 2026-11-01: 1:00-2:00 AM happens twice.
    const first = formatSpotDateTime("2026-11-01T05:30:00Z", "America/New_York", { zone: true });
    const second = formatSpotDateTime("2026-11-01T06:30:00Z", "America/New_York", { zone: true });
    expect(first).toBe("Sun, Nov 1, 1:30 AM EDT");
    expect(second).toBe("Sun, Nov 1, 1:30 AM EST");
    expect(zoneAbbrev("2026-11-01T05:30:00Z", "America/New_York")).toBe("EDT");
    expect(zoneAbbrev("2026-11-01T06:30:00Z", "America/New_York")).toBe("EST");
  });

  it("names the zone on both sides of the US Pacific fall-back (08:00Z PDT, 09:00Z PST)", () => {
    // America/Los_Angeles returns to PST at 09:00Z on 2026-11-01: both instants read 1:00 AM.
    const before = formatSpotDateTime("2026-11-01T08:00:00Z", "America/Los_Angeles", { zone: true });
    const after = formatSpotDateTime("2026-11-01T09:00:00Z", "America/Los_Angeles", { zone: true });
    expect(before).toBe("Sun, Nov 1, 1:00 AM PDT");
    expect(after).toBe("Sun, Nov 1, 1:00 AM PST");
    expect(before).not.toBe(after);
    expect(formatSpotTime("2026-11-01T08:00:00Z", "America/Los_Angeles")).toBe("Sun 1:00 AM PDT");
    expect(formatSpotTime("2026-11-01T09:00:00Z", "America/Los_Angeles")).toBe("Sun 1:00 AM PST");
  });

  it("names zones without a common abbreviation by their offset (Asia/Makassar: GMT+8)", () => {
    expect(formatSpotDateTime("2026-10-15T00:00:00Z", "Asia/Makassar", { zone: true })).toBe("Thu, Oct 15, 8:00 AM GMT+8");
    expect(formatSpotTime("2026-10-15T00:00:00Z", "Asia/Makassar")).toBe("Thu 8:00 AM GMT+8");
    expect(formatSpotTime("2026-10-15T00:00:00Z", "Asia/Makassar", { zone: false })).toBe("Thu 8:00 AM");
    expect(zoneAbbrev("2026-10-15T00:00:00Z", "Asia/Makassar")).toBe("GMT+8");
  });

  it("names zones whose offset is not a whole hour", () => {
    expect(zoneAbbrev("2026-10-14T00:00:00Z", "Asia/Kathmandu")).toBe("GMT+5:45");
    expect(zoneAbbrev("2026-07-01T00:00:00Z", "America/Los_Angeles")).toBe("PDT");
    expect(zoneAbbrev("2026-10-14T00:00:00Z", "UTC")).toBe("UTC");
  });

  it("labels hour ticks 12 AM, 6 AM, 12 PM, 6 PM in the spot's zone", () => {
    // Africa/Johannesburg is UTC+2: local midnight is 22:00Z the day before.
    const at = (iso: string) => formatHourTick(Date.parse(iso), "Africa/Johannesburg");
    expect(at("2026-10-12T22:00:00Z")).toBe("12 AM");
    expect(at("2026-10-13T04:00:00Z")).toBe("6 AM");
    expect(at("2026-10-13T10:00:00Z")).toBe("12 PM");
    expect(at("2026-10-13T16:00:00Z")).toBe("6 PM");
    // Kathmandu (UTC+5:45): local midnight falls at 18:15Z; a whole UTC hour shows its minutes.
    expect(formatHourTick(Date.parse("2026-10-12T18:15:00Z"), "Asia/Kathmandu")).toBe("12 AM");
    expect(formatHourTick(Date.parse("2026-10-12T18:00:00Z"), "Asia/Kathmandu")).toBe("11:45 PM");
  });
});
