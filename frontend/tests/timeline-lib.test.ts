import { afterEach, describe, expect, it } from "vitest";

import {
  dayInfos,
  defaultStepIndex,
  hourTicks,
  localDate,
  localMidnights,
  makeScale,
  nearestStepIndex,
  nextDay,
  nightBands,
  openingStepIndex,
  partitionKinds,
  pxPerHourFor,
  qualityRuns,
  relationRuns,
  segmentsText,
  selectedTimeTitle,
  smoothPath,
  stepExtents,
  stepLengthText,
  tideHeightText,
  timelineModel,
  zoneLabel,
  zoneName,
} from "@/lib/timeline";

import { jbay, lisbonFallBack, mixed3h6h, withSyntheticTide } from "./timeline-helpers";

const H = 3_600_000;
const T = (iso: string) => Date.parse(iso);
const labels = (ticks: ReturnType<typeof hourTicks>) => ticks.map((t) => [new Date(t.t).toISOString().slice(0, 16), t.label]);

describe("hour ticks come from the axis and the zone, not from sample times", () => {
  it("Africa/Johannesburg (UTC+2) gets 12 AM / 6 AM / 12 PM / 6 PM at 22, 04, 10 and 16 UTC", () => {
    const ticks = hourTicks(T("2026-10-12T00:00:00Z"), T("2026-10-14T00:00:00Z"), "Africa/Johannesburg", 12);
    expect(labels(ticks).slice(0, 5)).toEqual([
      ["2026-10-12T04:00", "6 AM"],
      ["2026-10-12T10:00", "12 PM"],
      ["2026-10-12T16:00", "6 PM"],
      ["2026-10-12T22:00", "12 AM"],
      ["2026-10-13T04:00", "6 AM"],
    ]);
    expect(ticks.filter((t) => t.midnight).map((t) => new Date(t.t).toISOString())).toEqual(["2026-10-12T22:00:00.000Z", "2026-10-13T22:00:00.000Z"]);
  });

  it("Pacific/Honolulu (UTC-10) and Asia/Kathmandu (UTC+5:45) get their own instants", () => {
    const hnl = hourTicks(T("2026-10-12T00:00:00Z"), T("2026-10-13T00:00:00Z"), "Pacific/Honolulu", 12);
    expect(labels(hnl)).toEqual([
      ["2026-10-12T04:00", "6 PM"],
      ["2026-10-12T10:00", "12 AM"],
      ["2026-10-12T16:00", "6 AM"],
      ["2026-10-12T22:00", "12 PM"],
    ]);
    const ktm = hourTicks(T("2026-10-12T00:00:00Z"), T("2026-10-13T00:00:00Z"), "Asia/Kathmandu", 12);
    expect(labels(ktm)).toEqual([
      ["2026-10-12T00:15", "6 AM"],
      ["2026-10-12T06:15", "12 PM"],
      ["2026-10-12T12:15", "6 PM"],
      ["2026-10-12T18:15", "12 AM"],
    ]);
  });

  it("every one of the app's time zones gets day ticks on a 3-hourly UTC grid (the old CH-1 defect)", () => {
    // The old chart kept only samples at local midnight: nothing for 42 of 50 spots.
    for (const tz of ["Africa/Johannesburg", "Asia/Makassar", "America/Lima", "Pacific/Fiji", "Europe/Lisbon", "Australia/Adelaide", "Asia/Kolkata"]) {
      const mids = localMidnights(T("2026-10-12T00:00:00Z"), T("2026-10-15T00:00:00Z"), tz);
      expect(mids.length, tz).toBeGreaterThanOrEqual(2);
      for (const m of mids) expect(new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit", hourCycle: "h23" }).format(m)).toBe("00:00");
    }
  });

  it("adapts the spacing to the zoom (3 h when wide, 12 h when narrow)", () => {
    const span = [T("2026-10-12T00:00:00Z"), T("2026-10-13T00:00:00Z")] as const;
    expect(hourTicks(...span, "UTC", 24)).toHaveLength(9);
    expect(hourTicks(...span, "UTC", 12)).toHaveLength(5);
    expect(hourTicks(...span, "UTC", 6)).toHaveLength(3);
  });

  it("Europe/Lisbon 2026-10-25 is a 25-hour day: 12 AM to 6 AM is seven hours", () => {
    const tl = lisbonFallBack(jbay());
    const day = dayInfos(tl.days)[0]!;
    expect((day.end - day.start) / H).toBe(25);
    const ticks = hourTicks(day.start, day.end, "Europe/Lisbon", 12);
    expect(labels(ticks)).toEqual([
      ["2026-10-24T23:00", "12 AM"],
      ["2026-10-25T06:00", "6 AM"],
      ["2026-10-25T12:00", "12 PM"],
      ["2026-10-25T18:00", "6 PM"],
      ["2026-10-26T00:00", "12 AM"],
    ]);
  });

  describe("does not depend on the browser's zone", () => {
    const before = process.env.TZ;
    afterEach(() => {
      process.env.TZ = before;
    });
    it("labels in the spot zone with TZ=America/New_York", () => {
      const a = labels(hourTicks(T("2026-10-12T00:00:00Z"), T("2026-10-14T00:00:00Z"), "Africa/Johannesburg", 12));
      process.env.TZ = "America/New_York";
      const b = labels(hourTicks(T("2026-10-12T00:00:00Z"), T("2026-10-14T00:00:00Z"), "Africa/Johannesburg", 12));
      expect(b).toEqual(a);
      expect(localDate(T("2026-10-12T22:30:00Z"), "Africa/Johannesburg")).toBe("2026-10-13");
      expect(selectedTimeTitle(T("2026-10-15T05:00:00Z"), "Africa/Johannesburg")).toBe("Thursday, Oct 15 · 7:00 AM SAST");
    });
  });

  it("handles the date line: Pacific/Fiji's local date is a day ahead of UTC", () => {
    expect(localDate(T("2026-10-14T13:00:00Z"), "Pacific/Fiji")).toBe("2026-10-15");
    expect(labels(hourTicks(T("2026-10-14T12:00:00Z"), T("2026-10-14T13:00:00Z"), "Pacific/Fiji", 12))).toEqual([["2026-10-14T12:00", "12 AM"]]);
  });
});

describe("zone names", () => {
  it("prints the zone's own abbreviation with the offset", () => {
    const t = T("2026-10-15T05:00:00Z");
    expect(zoneName(t, "Africa/Johannesburg")).toBe("SAST");
    expect(zoneLabel(t, "Africa/Johannesburg")).toBe("SAST (GMT+2)");
    expect(zoneLabel(t, "America/Los_Angeles")).toBe("PDT (GMT-7)");
    expect(zoneLabel(t, "America/Lima")).toBe("GMT-5");
    expect(zoneLabel(T("2026-11-01T09:00:00Z"), "America/Los_Angeles")).toBe("PST (GMT-8)");
  });
});

describe("one scale for every row", () => {
  it("maps time to pixels and back", () => {
    const s = makeScale(T("2026-10-12T00:00:00Z"), 15);
    expect(s(T("2026-10-12T02:00:00Z"))).toBe(30);
    expect(s.invert(45)).toBe(T("2026-10-12T03:00:00Z"));
  });
  it("fits about three days on a desktop and one on a phone, within readable limits", () => {
    expect(pxPerHourFor(1100, 72)).toBe(15.25);
    expect(pxPerHourFor(290, 24)).toBe(12);
    expect(pxPerHourFor(100, 72)).toBe(6);
    expect(pxPerHourFor(5000, 24)).toBe(28);
  });
});

describe("steps are drawn as wide as the time they stand for", () => {
  it("3-hourly steps span 3 h centred on their valid time", () => {
    const steps = stepExtents(jbay().steps);
    expect(steps).toHaveLength(23);
    for (const s of steps) {
      expect((s.x1 - s.x0) / H).toBe(3);
      expect(s.t - s.x0).toBe(1.5 * H);
    }
  });
  it("a 3 h then 6 h run gives wider 6-hour bars, no invented steps", () => {
    const tl = mixed3h6h(jbay());
    const steps = stepExtents(tl.steps);
    expect(steps).toHaveLength(tl.steps.length);
    expect((steps[2]!.x1 - steps[2]!.x0) / H).toBe(3);
    expect((steps[9]!.x1 - steps[9]!.x0) / H).toBe(4.5); // halfway back 1.5 h, forward 3 h
    expect((steps[12]!.x1 - steps[12]!.x0) / H).toBe(6);
    expect(steps.map((s) => s.t)).toEqual(tl.steps.map((s) => Date.parse(s.t)));
    expect(segmentsText(tl.steps_meta.segments, tl.timezone)).toMatch(/^3-hour steps, from \w{3} \d+:\d{2} [AP]M 6-hour steps$/);
  });
  it("names the step length", () => {
    expect(stepLengthText(3)).toBe("3-hour step");
    expect(stepLengthText(1)).toBe("1-hour step");
    expect(stepLengthText(0)).toBe("single forecast time");
  });
  it("keeps a step without a length visible and never claims one (SYNTHETIC 0 h step)", () => {
    const tl = jbay();
    // A lone last step of unknown length: as wide as the gap before it.
    tl.steps[tl.steps.length - 1]!.step_hours = 0;
    const steps = stepExtents(tl.steps);
    const last = steps[steps.length - 1]!;
    expect((last.x1 - last.x0) / H).toBe(3);
    // The run's only time: drawn one hour wide (display only), so the axis is not empty.
    const one = stepExtents([{ ...tl.steps[0]!, step_hours: 0 }]);
    expect((one[0]!.x1 - one[0]!.x0) / H).toBe(1);
    const model = timelineModel({ ...tl, steps: [{ ...tl.steps[0]!, step_hours: 0 }] });
    expect(model.end - model.start).toBe(H);
  });
});

describe("selection helpers", () => {
  const times = [0, 3, 6, 12, 18].map((h) => h * H);
  it("snaps to the nearest step (ties go to the earlier one)", () => {
    expect(nearestStepIndex(times, 4 * H)).toBe(1);
    expect(nearestStepIndex(times, 4.5 * H)).toBe(1);
    expect(nearestStepIndex(times, 5 * H)).toBe(2);
    expect(nearestStepIndex(times, -10 * H)).toBe(0);
    expect(nearestStepIndex(times, 99 * H)).toBe(4);
    expect(nearestStepIndex([], 0)).toBe(-1);
  });
  it("moves a day at the same time of day", () => {
    const hourly = Array.from({ length: 72 }, (_, i) => i * H);
    expect(nextDay(hourly, 5, 1)).toBe(29);
    expect(nextDay(hourly, 29, -1)).toBe(5);
  });
  it("opens on the step covering now, or the next daylight step at night", () => {
    const tl = jbay();
    // 2026-10-12T13:00Z is daylight in J-Bay: the 12:00Z step covers it.
    expect(tl.steps[defaultStepIndex(tl, T("2026-10-12T13:00:00Z"))]!.t).toBe("2026-10-12T12:00:00Z");
    expect(tl.steps[openingStepIndex(tl, T("2026-10-12T13:00:00Z"))]!.t).toBe("2026-10-12T12:00:00Z");
    // 20:00Z is night: the next daylight step is the next morning's.
    const i = openingStepIndex(tl, T("2026-10-12T20:00:00Z"));
    expect(tl.steps[i]!.is_daylight).toBe(true);
    expect(Date.parse(tl.steps[i]!.t)).toBeGreaterThan(T("2026-10-12T20:00:00Z"));
  });
});

describe("night bands follow the API's first and last light", () => {
  it("span exactly last light to the next day's first light", () => {
    const tl = jbay();
    const days = dayInfos(tl.days);
    const bands = nightBands(days);
    // Before the first day's first light, between days, after the last day's last light.
    expect(bands[0]).toEqual([days[0]!.start, T(tl.days[0]!.first_light!)]);
    expect(bands[1]).toEqual([T(tl.days[0]!.last_light!), T(tl.days[1]!.first_light!)]);
    expect(bands[2]).toEqual([T(tl.days[1]!.last_light!), T(tl.days[2]!.first_light!)]);
    expect(bands[3]).toEqual([T(tl.days[2]!.last_light!), days[2]!.end]);
  });
  it("is night all day in a polar night and never in a polar day", () => {
    const tl = jbay();
    const d = dayInfos(tl.days);
    expect(nightBands([{ ...d[0]!, polar: "polar_night", firstLight: null, lastLight: null }])).toEqual([[d[0]!.start, d[0]!.end]]);
    expect(nightBands([{ ...d[0]!, polar: "polar_day", firstLight: null, lastLight: null }])).toEqual([]);
  });
});

describe("the shared model", () => {
  it("lists only the partitions the API sent, in order", () => {
    const tl = jbay();
    expect(partitionKinds(tl.steps)).toEqual(["primary_swell", "secondary_swell", "tertiary_swell", "wind_sea"]);
    const none = structuredClone(tl);
    for (const s of none.steps) s.partitions = s.partitions.filter((p) => p.kind === "primary_swell");
    expect(timelineModel(none).kinds).toEqual(["primary_swell"]);
  });
  it("has no tide when the block is unavailable, and the block's points when it is not", () => {
    expect(timelineModel(jbay()).tide).toBeNull();
    const m = timelineModel(withSyntheticTide(jbay()));
    expect(m.tide!.curve.length).toBeGreaterThan(100);
    expect(m.tide!.events.every((e) => e.t >= m.start && e.t <= m.end)).toBe(true);
  });
  it("groups labels by run of equal class (and day or night) and wind relation", () => {
    const steps = stepExtents(jbay().steps);
    const runs = qualityRuns(steps);
    expect(runs.reduce((n, r) => n + r.last - r.first + 1, 0)).toBe(steps.length);
    for (const r of runs) for (let i = r.first; i <= r.last; i++) expect(steps[i]!.step.quality.label).toBe(r.label);
    expect(relationRuns(steps).every((r) => steps[r.first]!.step.wind!.relation === r.relation)).toBe(true);
  });
});

describe("drawing helpers", () => {
  it("draws the tide curve through its points", () => {
    const d = smoothPath([
      [0, 10],
      [10, 0],
      [20, 10],
    ]);
    expect(d.startsWith("M0,10C")).toBe(true);
    expect(d).toContain(",10,0C");
    expect(d.endsWith(",20,10")).toBe(true);
  });
  it("never prints -0.0 for a tide height", () => {
    expect(tideHeightText(-0.01, "ft")).toBe("0.0 ft");
    expect(tideHeightText(-0.4, "m")).toBe("-0.4 m");
    expect(tideHeightText(0.3048, "ft")).toBe("1.0 ft");
  });
});
