/**
 * Builders for the timeline tests. `jbay()` is the real 3-hourly J-Bay payload of
 * tests/fixtures (see its README). Every other builder derives a SYNTHETIC variant from it
 * (a made-up tide curve, re-timed steps, removed values): those shapes exist only to exercise
 * the charts and describe no real forecast.
 */
import raw from "./fixtures/timeline-jbay-3h.json";

import type { SpotTimeline, TimelineDay, TimelineStep } from "@/types/api";

const HOUR = 3_600_000;

export function jbay(): SpotTimeline {
  return structuredClone(raw) as unknown as SpotTimeline;
}

const iso = (t: number) => new Date(t).toISOString().replace(".000Z", "Z");

/** SYNTHETIC semidiurnal tide (two made-up constituents), 30-minute points with highs and
 * lows, status "demo" and model mean sea level as the datum; each step gets its height. */
export function withSyntheticTide(tl: SpotTimeline): SpotTimeline {
  const out = structuredClone(tl);
  const h = (t: number) => {
    const x = t / HOUR;
    return 0.6 * Math.cos((2 * Math.PI * x) / 12.42) + 0.2 * Math.cos((2 * Math.PI * x) / 12 + 1.1);
  };
  const t0 = Date.parse(out.steps[0]!.t) - 3 * HOUR;
  const t1 = Date.parse(out.steps[out.steps.length - 1]!.t) + 6 * HOUR;
  const curve: SpotTimeline["tide"]["curve"] = [];
  for (let t = t0; t <= t1; t += HOUR / 2) curve.push({ t: iso(t), height_m: Math.round(h(t) * 1000) / 1000, interpolated: false });
  const events: SpotTimeline["tide"]["events"] = [];
  for (let i = 1; i < curve.length - 1; i++) {
    const [a, b, c] = [curve[i - 1]!.height_m, curve[i]!.height_m, curve[i + 1]!.height_m];
    if (b > a && b >= c) events.push({ t: curve[i]!.t, kind: "high", height_m: b });
    if (b < a && b <= c) events.push({ t: curve[i]!.t, kind: "low", height_m: b });
  }
  out.tide = {
    status: "demo",
    reason: "SYNTHETIC test tide",
    station: null,
    source: "synthetic-test",
    datum: "model_msl",
    units: "m",
    curve,
    events,
    preference: { value: "mid", research_class: "moderate", decision: null, text: "Mid tide (synthetic test preference)." },
    attribution: null,
    fetched_at: null,
  };
  for (const s of out.steps) {
    const t = Date.parse(s.t);
    s.tide = { height_m: Math.round(h(t) * 1000) / 1000, rising: h(t + 60_000) > h(t), stage: null };
  }
  return out;
}

/** SYNTHETIC: the first `n3` steps 3 hours apart, the rest 6 hours apart (as an ECMWF run
 * changes spacing at +144 h), with segments and step lengths to match. */
export function mixed3h6h(tl: SpotTimeline, n3 = 10): SpotTimeline {
  const out = structuredClone(tl);
  const t0 = Date.parse(out.steps[0]!.t);
  out.steps.forEach((s, i) => {
    const t = i < n3 ? t0 + i * 3 * HOUR : t0 + (n3 - 1) * 3 * HOUR + (i - n3 + 1) * 6 * HOUR;
    s.t = iso(t);
    s.step_hours = i < n3 - 1 ? 3 : 6;
  });
  const last = Date.parse(out.steps[out.steps.length - 1]!.t);
  out.steps_meta.segments = [
    { start: out.steps[0]!.t, end: out.steps[n3 - 1]!.t, step_hours: 3 },
    { start: out.steps[n3 - 1]!.t, end: iso(last + 6 * HOUR), step_hours: 6 },
  ];
  out.days = localDays(out.days[0]!, Math.ceil((last - t0) / (24 * HOUR)) + 1);
  return out;
}

/** Consecutive 24-hour days after `first` (SYNTHETIC: light times copied from the first day). */
function localDays(first: TimelineDay, n: number): TimelineDay[] {
  const shift = (v: string | null, k: number) => (v ? iso(Date.parse(v) + k * 24 * HOUR) : null);
  return Array.from({ length: n }, (_, k) => {
    const start = Date.parse(first.start_utc) + k * 24 * HOUR;
    return {
      ...first,
      date: new Date(Date.parse(`${first.date}T12:00:00Z`) + k * 24 * HOUR).toISOString().slice(0, 10),
      start_utc: iso(start),
      end_utc: iso(start + 24 * HOUR),
      sunrise: shift(first.sunrise, k),
      sunset: shift(first.sunset, k),
      first_light: shift(first.first_light, k),
      last_light: shift(first.last_light, k),
      best_time: null,
    };
  });
}

/** SYNTHETIC: the payload's values placed hourly around the Europe/Lisbon clock change of
 * 2026-10-25 (WEST to WET at 01:00 UTC, a 25-hour local day). */
export function lisbonFallBack(tl: SpotTimeline): SpotTimeline {
  const out = structuredClone(tl);
  out.timezone = "Europe/Lisbon";
  const t0 = Date.parse("2026-10-24T23:00:00Z"); // local midnight, Oct 25 (WEST)
  out.steps = out.steps.map((s, i) => ({ ...s, t: iso(t0 + i * HOUR), step_hours: 1 }));
  out.steps_meta.segments = [{ start: out.steps[0]!.t, end: iso(t0 + out.steps.length * HOUR), step_hours: 1 }];
  const day = (date: string, start: string, end: string, rise: string, set: string): TimelineDay => ({
    ...out.days[0]!,
    date,
    start_utc: start,
    end_utc: end,
    sunrise: rise,
    sunset: set,
    first_light: iso(Date.parse(rise) - 20 * 60_000),
    last_light: iso(Date.parse(set) + 20 * 60_000),
    best_time: null,
  });
  out.days = [day("2026-10-25", "2026-10-24T23:00:00Z", "2026-10-26T00:00:00Z", "2026-10-25T07:20:00Z", "2026-10-25T17:40:00Z")];
  return out;
}

/** A copy of step `i` changed by `patch` (SYNTHETIC values). */
export function patchStep(tl: SpotTimeline, i: number, patch: (s: TimelineStep) => void): SpotTimeline {
  const out = structuredClone(tl);
  patch(out.steps[i]!);
  return out;
}
