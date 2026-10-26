import { describe, expect, it } from "vitest";

import {
  bubbleLabel,
  cycleLabel,
  leadLabel,
  longTimeLabel,
  modelName,
  nearestStepIndex,
  stepHours,
  tickIndices,
  timelineDays,
  timelineSteps,
  timeParam,
} from "@/lib/map-time";

const every3h = (startIso: string, n: number) => Array.from({ length: n }, (_, i) => new Date(Date.parse(startIso) + i * 3 * 3_600_000).toISOString());

describe("timeline steps", () => {
  it("uses the index valid times when present (sorted, de-duplicated, strings kept)", () => {
    const vt = ["2026-10-11T06:00:00Z", "2026-10-11T00:00:00Z", "2026-10-11T03:00:00Z", "2026-10-11T06:00:00+00:00"];
    expect(timelineSteps(vt, { start: "2026-10-11T00:00:00Z", end: "2026-10-12T00:00:00Z", stepHours: 3 })).toEqual([
      "2026-10-11T00:00:00Z",
      "2026-10-11T03:00:00Z",
      "2026-10-11T06:00:00Z",
    ]);
  });

  it("falls back to the snapshot range: step 0 = the server's now hour", () => {
    const s = timelineSteps([], { start: "2026-10-11T16:00:00Z", end: "2026-10-12T04:30:00Z", stepHours: 3 });
    expect(s[0]).toBe("2026-10-11T16:00:00.000Z");
    expect(s.at(-1)).toBe("2026-10-12T04:00:00.000Z");
    expect(s).toHaveLength(5);
    expect(timelineSteps(null, null)).toEqual([]);
    expect(timelineSteps(undefined, { start: "x", end: "y", stepHours: 3 })).toEqual([]);
  });

  it("finds the step nearest to a time", () => {
    const s = every3h("2026-10-11T00:00:00Z", 8);
    expect(nearestStepIndex(s, "2026-10-11T07:59:00Z")).toBe(3);
    expect(nearestStepIndex(s, "2026-10-11T07:00:00Z")).toBe(2);
    expect(nearestStepIndex(s, "2030-01-01T00:00:00Z")).toBe(7);
    expect(nearestStepIndex(s, null)).toBe(0);
    expect(stepHours(s)).toBe(3);
    expect(tickIndices(s)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    const hourly = Array.from({ length: 7 }, (_, i) => new Date(Date.parse("2026-10-11T00:00:00Z") + i * 3_600_000).toISOString());
    expect(tickIndices(hourly)).toEqual([0, 3, 6]);
  });
});

describe("timeline labels", () => {
  it("formats the bubble like '8am Sun PDT' in the given zone", () => {
    expect(bubbleLabel("2026-10-25T15:00:00Z", "America/Los_Angeles")).toBe("8am Sun PDT");
    expect(bubbleLabel("2026-10-25T21:00:00Z", "America/Los_Angeles")).toBe("2pm Sun PDT");
    expect(bubbleLabel("2026-10-25T03:00:00Z", "Asia/Kolkata")).toBe("8:30am Sun GMT+5:30");
    expect(bubbleLabel("2026-10-25T12:00:00Z", "UTC")).toBe("12pm Sun UTC");
    expect(longTimeLabel("2026-10-25T15:00:00Z", "America/Los_Angeles")).toBe("Sunday, October 25 at 8:00 AM PDT");
  });

  it("is DST-safe: the zone abbreviation and day grouping follow local time", () => {
    // US daylight time ends on Sun Nov 1 2026 at 2am local (09:00 UTC).
    expect(bubbleLabel("2026-11-01T06:00:00Z", "America/Los_Angeles")).toBe("11pm Sat PDT");
    expect(bubbleLabel("2026-11-01T12:00:00Z", "America/Los_Angeles")).toBe("4am Sun PST");
    const steps = every3h("2026-10-31T07:00:00Z", 24); // 3 days of 3-hourly steps
    const days = timelineDays(steps, "America/Los_Angeles");
    expect(days.map((d) => d.label)).toEqual(["Sat Oct 31", "Sun Nov 1", "Mon Nov 2"]);
    expect(days.map((d) => d.weekday)).toEqual(["Sat", "Sun", "Mon"]);
    // Sunday has 25 hours: its steps run from 00:00 PDT to 23:00 PST
    const sunday = days[1]!;
    expect(bubbleLabel(steps[sunday.first]!, "America/Los_Angeles")).toBe("12am Sun PDT");
    expect(bubbleLabel(steps[sunday.last]!, "America/Los_Angeles")).toBe("11pm Sun PST");
    expect(sunday.last - sunday.first + 1).toBe(9);
    // contiguous and covering every step
    expect(days[0]!.first).toBe(0);
    expect(days.at(-1)!.last).toBe(steps.length - 1);
    days.slice(1).forEach((d, i) => expect(d.first).toBe(days[i]!.last + 1));
  });

  it("groups by the local date, not the UTC date", () => {
    const steps = ["2026-10-25T22:00:00Z", "2026-10-26T01:00:00Z", "2026-10-26T06:00:00Z"];
    expect(timelineDays(steps, "America/Los_Angeles").map((d) => d.key)).toEqual(["2026-10-25"]);
    expect(timelineDays(steps, "Australia/Sydney").map((d) => d.key)).toEqual(["2026-10-26"]);
    expect(timelineDays(steps, "UTC").map((d) => d.key)).toEqual(["2026-10-25", "2026-10-26"]);
  });
});

describe("model run and lead time", () => {
  it("names the cycle in UTC like forecasters do", () => {
    expect(cycleLabel("2026-10-11T18:00:00Z")).toBe("18Z Oct 11");
    expect(cycleLabel("2026-10-12T00:00:00+00:00")).toBe("00Z Oct 12");
    expect(cycleLabel("2026-10-11T23:30:00-02:00")).toBe("01:30Z Oct 12");
  });

  it("counts the lead time from the cycle; the cycle time itself is the analysis", () => {
    const cycle = "2026-10-11T18:00:00Z";
    expect(leadLabel("2026-10-11T18:00:00Z", cycle)).toBe("analysis");
    expect(leadLabel("2026-10-12T00:00:00Z", cycle)).toBe("+6 h");
    expect(leadLabel("2026-10-21T18:00:00Z", cycle)).toBe("+240 h");
    expect(leadLabel("2026-10-11T15:00:00Z", cycle)).toBe("−3 h");
    expect(leadLabel(null, cycle)).toBeNull();
    expect(leadLabel("2026-10-12T00:00:00Z", undefined)).toBeNull();
    expect(leadLabel("not a time", cycle)).toBeNull();
  });

  it("shortens source names to the model", () => {
    expect(modelName({ code: "noaa_gfs_direct", name: "NOAA GFS-Wave + GFS (direct from NOAA open data)" })).toBe("GFS");
    expect(modelName({ code: "ecmwf_ifs_open", name: "ECMWF IFS HRES + ecWAM (open data, 0.25°)" })).toBe("ECMWF");
    expect(modelName({ code: "synthetic", name: "Synthetic test fields" })).toBe("Synthetic test fields");
    expect(modelName({ code: "x", name: "Some model (detail)" })).toBe("Some model");
  });

  it("writes ?t= in UTC whatever the input offset", () => {
    expect(timeParam("2026-10-12T00:00:00Z")).toBe("2026-10-12T00:00:00Z");
    expect(timeParam("2026-10-11T20:00:00-04:00")).toBe("2026-10-12T00:00:00Z");
    expect(timeParam("2026-10-12T00:00:00.000Z")).toBe("2026-10-12T00:00:00Z");
  });
});
