import type { Page } from "@playwright/test";

import { apiGet } from "./helpers";

/**
 * Timeline payloads for timeline.spec.ts, derived from the demo backend's own
 * /api/spots/{slug}/timeline response. Everything these builders add is SYNTHETIC and exists
 * only in tests: a made-up tide curve (status "demo"), the demo's hourly steps thinned to a
 * 3-hourly-then-6-hourly cadence (values kept at their real valid times), removed values.
 */

const HOUR = 3_600_000;
const iso = (t: number) => new Date(t).toISOString().replace(".000Z", "Z");

// The parts of the contract these builders touch (the e2e folder is not type-checked against
// the generated OpenAPI types).
export interface MockStep {
  t: string;
  step_hours: number;
  is_daylight: boolean;
  breaking: { central_ft: number; typical_ft: number; sets_ft: number; p10_ft: number | null; p90_ft: number | null };
  wind: { speed_kmh: number | null; gust_kmh: number | null; dir_from_deg: number | null; relation: string } | null;
  tide: { height_m: number; rising: boolean | null; stage: number | null } | null;
  quality: { score: number; label: string };
  partitions: Array<{ kind: string; height_m: number; dir_from_deg: number | null; drives_surf: boolean }>;
}
export interface MockTimeline {
  spot_slug: string;
  timezone: string;
  available: boolean;
  message: string | null;
  steps: MockStep[];
  days: Array<{ date: string; start_utc: string; end_utc: string; first_light: string | null; last_light: string | null; best_time: string | null }>;
  steps_meta: { segments: Array<{ start: string; end: string; step_hours: number }>; interpolation: string; policy: string };
  tide: Record<string, unknown> & { status: string; curve: Array<{ t: string; height_m: number }>; events: Array<{ t: string; kind: string; height_m: number }> };
}

export async function realTimeline(page: Page, slug = "jeffreys-bay"): Promise<MockTimeline> {
  return (await apiGet(page.request, `/api/spots/${slug}/timeline?days=10`)) as MockTimeline;
}

/** SYNTHETIC semidiurnal tide (two made-up constituents) at 30-minute points, with highs and
 * lows; status "demo", datum model mean sea level; each step gets its height. */
export function withSyntheticTide(tl: MockTimeline): MockTimeline {
  const out = structuredClone(tl);
  const h = (t: number) => 0.6 * Math.cos((2 * Math.PI * t) / (12.42 * HOUR)) + 0.2 * Math.cos((2 * Math.PI * t) / (12 * HOUR) + 1.1);
  const t0 = Date.parse(out.steps[0]!.t) - 3 * HOUR;
  const t1 = Date.parse(out.steps[out.steps.length - 1]!.t) + 6 * HOUR;
  const curve: Array<{ t: string; height_m: number; interpolated: boolean }> = [];
  for (let t = t0; t <= t1; t += HOUR / 2) curve.push({ t: iso(t), height_m: Math.round(h(t) * 1000) / 1000, interpolated: false });
  const events: Array<{ t: string; kind: string; height_m: number }> = [];
  for (let i = 1; i < curve.length - 1; i++) {
    const [a, b, c] = [curve[i - 1]!.height_m, curve[i]!.height_m, curve[i + 1]!.height_m];
    if (b > a && b >= c) events.push({ t: curve[i]!.t, kind: "high", height_m: b });
    if (b < a && b <= c) events.push({ t: curve[i]!.t, kind: "low", height_m: b });
  }
  out.tide = {
    status: "demo",
    reason: "SYNTHETIC e2e tide",
    station: { code: "TEST", name: "Synthetic test station", provider: "test", kind: "model", distance_km: 3.2 },
    source: "synthetic-e2e",
    datum: "model_msl",
    units: "m",
    curve,
    events,
    preference: { value: "mid", research_class: "moderate", decision: null, text: "Mid tide (synthetic e2e preference)." },
    attribution: null,
    fetched_at: null,
  };
  for (const s of out.steps) {
    const t = Date.parse(s.t);
    s.tide = { height_m: Math.round(h(t) * 1000) / 1000, rising: h(t + 60_000) > h(t), stage: null };
  }
  return out;
}

/** The demo's steps thinned to every 3 hours for `hours3` hours, then every 6 hours (an
 * ECMWF-like cadence change); the values stay at their own valid times (SYNTHETIC cadence). */
export function mixedCadence(tl: MockTimeline, hours3 = 48): MockTimeline {
  const out = structuredClone(tl);
  const t0 = Date.parse(out.steps[0]!.t);
  const keep = out.steps.filter((s) => {
    const dh = Math.round((Date.parse(s.t) - t0) / HOUR);
    return dh <= hours3 ? dh % 3 === 0 : (dh - hours3) % 6 === 0;
  });
  keep.forEach((s, i) => {
    const next = keep[i + 1];
    s.step_hours = next ? (Date.parse(next.t) - Date.parse(s.t)) / HOUR : 6;
  });
  out.steps = keep;
  const cut = keep.find((s) => Date.parse(s.t) - t0 === hours3 * HOUR)!;
  const last = keep[keep.length - 1]!;
  out.steps_meta.segments = [
    { start: keep[0]!.t, end: cut.t, step_hours: 3 },
    { start: cut.t, end: iso(Date.parse(last.t) + 6 * HOUR), step_hours: 6 },
  ];
  return out;
}

export async function routeTimeline(page: Page, body: unknown, status = 200, slug = "jeffreys-bay") {
  await page.route(new RegExp(`/api/spots/${slug}/timeline`), (r) => r.fulfill({ status, json: body }));
}
