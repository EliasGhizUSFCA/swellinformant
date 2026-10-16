/**
 * The spot forecast timeline's geometry and time logic (pure functions, no React).
 *
 * Every chart row shares ONE scale, x = (t - t0) / 1 h × pxPerHour, so day boundaries and the
 * selected time line up across rows by construction. Times are epoch milliseconds; labels
 * are always in the spot's IANA zone.
 *
 * Steps are the API's stored valid times (never interpolated). A step is drawn over the
 * times for which it is the nearest valid step ("nearest-valid-step" in the API's
 * steps_meta): from halfway to the previous step to halfway to the next, so 3-hourly steps
 * are 3 hours wide, 6-hourly steps 6 hours wide, and a bar's centre is its valid time.
 */

import { compass16 } from "@/lib/directions";
import { formatHourTick, zoneName } from "@/lib/format";
import { asQuality, QUALITY_LABELS } from "@/lib/quality";
import type { HeightUnit } from "@/lib/units";
import type { PartitionOut, QualityLabel, SpotTimeline, TimelineDay, TimelineStep } from "@/types/api";

export { zoneName };

export const HOUR_MS = 3_600_000;
export const DAY_MS = 24 * HOUR_MS;

export interface Scale {
  (t: number): number;
  t0: number;
  pxPerHour: number;
  invert: (x: number) => number;
}

/** The shared time scale: x in pixels from t0 (epoch ms). */
export function makeScale(t0: number, pxPerHour: number): Scale {
  const k = pxPerHour / HOUR_MS;
  const scale = ((t: number) => (t - t0) * k) as Scale;
  scale.t0 = t0;
  scale.pxPerHour = pxPerHour;
  scale.invert = (x: number) => t0 + x / k;
  return scale;
}

/** Pixels per hour so that `hoursInView` hours fill `viewWidth`, rounded to 0.25 px and
 * clamped to a readable range. */
export function pxPerHourFor(viewWidth: number, hoursInView: number): number {
  const raw = viewWidth / Math.max(1, hoursInView);
  return Math.min(28, Math.max(6, Math.round(raw * 4) / 4));
}

// --- local time ---------------------------------------------------------------------

const PARTS = new Map<string, Intl.DateTimeFormat>();

function partsFormat(tz: string): Intl.DateTimeFormat {
  let f = PARTS.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
      hourCycle: "h23",
    });
    PARTS.set(tz, f);
  }
  return f;
}

/** Offset of `tz` from UTC at instant `t` (ms; local wall clock = t + offset). */
export function zoneOffsetMs(t: number, tz: string): number {
  const p = Object.fromEntries(partsFormat(tz).formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  const wall = Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day), Number(p.hour) % 24, Number(p.minute), Number(p.second));
  return wall - Math.floor(t / 1000) * 1000;
}

/** Local hour (0-23) and minute at `t` in `tz`. */
export function localHourMinute(t: number, tz: string): [number, number] {
  const wall = t + zoneOffsetMs(t, tz);
  const minutes = Math.floor(wall / 60_000);
  return [Math.floor(minutes / 60) % 24, minutes % 60];
}

/** Hour-tick spacing (local hours) that keeps labels apart at this zoom. */
export function tickIntervalHours(pxPerHour: number): number {
  if (pxPerHour >= 22) return 3;
  if (pxPerHour >= 8) return 6;
  return 12;
}

export interface HourTick {
  t: number;
  label: string;
  /** Local midnight. */
  midnight: boolean;
}

/**
 * Local-hour ticks between `start` and `end` (ms): 12 AM / 6 AM / 12 PM / 6 PM (every 3 h
 * when wide, every 12 h when narrow), generated from the axis and the zone, never from the
 * sample times. Half- and quarter-hour zones get their own instants (Kathmandu's 12 AM is
 * 18:15 UTC); a local time skipped by a clock change has no tick.
 */
export function hourTicks(start: number, end: number, tz: string, pxPerHour: number): HourTick[] {
  const every = tickIntervalHours(pxPerHour) * HOUR_MS;
  const out: HourTick[] = [];
  let guess = zoneOffsetMs(start, tz);
  // Candidate local wall-clock times, aligned to the interval in local time.
  for (let wall = Math.ceil((start + guess) / every) * every; wall - guess <= end + every; wall += every) {
    let t = wall - guess;
    const off = zoneOffsetMs(t, tz);
    if (off !== guess) {
      t = wall - off;
      if (zoneOffsetMs(t, tz) !== off) {
        guess = off; // the wall time does not exist (clocks jumped forward)
        continue;
      }
      guess = off;
    }
    if (t < start || t > end) continue;
    const [h, m] = localHourMinute(t, tz);
    out.push({ t, label: formatHourTick(t, tz), midnight: h === 0 && m === 0 });
  }
  return out;
}

// --- days and night -------------------------------------------------------------------

export interface DayInfo {
  date: string;
  start: number;
  end: number;
  firstLight: number | null;
  lastLight: number | null;
  polar: TimelineDay["polar_state"];
  day: TimelineDay;
}

const ms = (iso: string | null | undefined): number | null => (iso ? Date.parse(iso) : null);

export function dayInfos(days: readonly TimelineDay[]): DayInfo[] {
  return days.map((d) => ({
    date: d.date,
    start: Date.parse(d.start_utc),
    end: Date.parse(d.end_utc),
    firstLight: ms(d.first_light),
    lastLight: ms(d.last_light),
    polar: d.polar_state,
    day: d,
  }));
}

/** Local midnights (each day's start), for the day separators. */
export function dayBoundaries(days: readonly DayInfo[]): Array<{ date: string; t: number }> {
  return days.map((d) => ({ date: d.date, t: d.start }));
}

/**
 * Night as [from, to] intervals: from each day's last light to the next day's first light
 * (sun.py times from the API, not the per-step daylight flags), before the first day's
 * first light and after the last day's last light; a polar night is night all day and a
 * polar day has none.
 */
export function nightBands(days: readonly DayInfo[]): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  const push = (a: number, b: number) => {
    if (b <= a) return;
    const last = out[out.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  };
  for (const d of days) {
    if (d.polar === "polar_night") push(d.start, d.end);
    if (d.polar !== "normal") continue;
    // Each day contributes its own dark ends; at midnight they join into one band from
    // last light to the next day's first light.
    if (d.firstLight != null) push(d.start, d.firstLight);
    if (d.lastLight != null) push(d.lastLight, d.end);
  }
  return out;
}

// --- steps ---------------------------------------------------------------------------

export interface StepInfo {
  i: number;
  t: number;
  /** Interval drawn for the step: halfway to the neighbouring valid times. */
  x0: number;
  x1: number;
  hours: number;
  step: TimelineStep;
}

/** Extents [t - gap_prev/2, t + gap_next/2) of each step (the first uses its own length).
 * A step the API gives no length (0 h: the run's only stored time) is drawn one hour wide so
 * it stays visible; its text says "single forecast time", never a length. */
export function stepExtents(steps: readonly TimelineStep[]): StepInfo[] {
  const ts = steps.map((s) => Date.parse(s.t));
  return steps.map((s, i) => {
    const t = ts[i]!;
    const own = s.step_hours * HOUR_MS;
    const before = i > 0 ? t - ts[i - 1]! : 0;
    const next = own > 0 ? own : before > 0 ? before : HOUR_MS;
    const prev = i > 0 ? before : next;
    return { i, t, x0: t - prev / 2, x1: t + next / 2, hours: s.step_hours, step: s };
  });
}

/** Index of the step nearest to `t` (ms) in ascending times; -1 when there are none. */
export function nearestStepIndex(times: readonly number[], t: number): number {
  if (!times.length) return -1;
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid]! < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(times[lo - 1]! - t) <= Math.abs(times[lo]! - t)) return lo - 1;
  return lo;
}

export const nextStep = (n: number, i: number): number => Math.min(n - 1, i + 1);
export const prevStep = (i: number): number => Math.max(0, i - 1);

/** The step nearest to the same time one day later (`dir` 1) or earlier (-1). */
export function nextDay(times: readonly number[], i: number, dir: 1 | -1): number {
  const t = times[i];
  if (t == null) return i;
  const j = nearestStepIndex(times, t + dir * DAY_MS);
  return j === i ? (dir > 0 ? nextStep(times.length, i) : prevStep(i)) : j;
}

/** "3-hour step" / "1-hour step" / "6-hour step" ("single forecast time" without a length). */
export function stepLengthText(hours: number): string {
  if (!(hours > 0)) return "single forecast time";
  const h = Math.round(hours * 10) / 10;
  return `${Number.isInteger(h) ? h : h.toFixed(1)}-hour step`;
}

/** Local midnights between `start` and `end` (for a day-tick axis such as the model
 * comparison's), from the axis and the zone, never from sample times. */
export function localMidnights(start: number, end: number, tz: string): number[] {
  return hourTicks(start, end, tz, 8)
    .filter((tick) => tick.midnight)
    .map((tick) => tick.t);
}

// --- partitions and wind ---------------------------------------------------------------

export type PartitionKind = PartitionOut["kind"];

export const PARTITION_ORDER: readonly PartitionKind[] = ["primary_swell", "secondary_swell", "tertiary_swell", "wind_sea"];

export const PARTITION_LABEL: Record<PartitionKind, string> = {
  primary_swell: "Primary swell",
  secondary_swell: "Secondary swell",
  tertiary_swell: "Third swell",
  wind_sea: "Wind sea",
};

/** Row labels for the narrow gutter. */
export const PARTITION_SHORT: Record<PartitionKind, string> = {
  primary_swell: "Primary",
  secondary_swell: "Secondary",
  tertiary_swell: "Third",
  wind_sea: "Wind sea",
};

/** The partition kinds the API sent anywhere in the timeline, in display order. */
export function partitionKinds(steps: readonly TimelineStep[]): PartitionKind[] {
  const seen = new Set<PartitionKind>();
  for (const s of steps) for (const p of s.partitions) seen.add(p.kind);
  return PARTITION_ORDER.filter((k) => seen.has(k));
}

/** The partition that drives the surf (``drives_surf``), or null when the API marks none. */
export function drivingPartition(step: TimelineStep): PartitionOut | null {
  return step.partitions.find((p) => p.drives_surf) ?? null;
}

/** Short wind-relation names for narrow chart labels (the full names are in the summary). */
export const RELATION_SHORT: Record<string, string> = {
  glassy: "Glassy",
  offshore: "Off",
  "cross-offshore": "X-off",
  "cross-shore": "Cross",
  "cross-onshore": "X-on",
  onshore: "On",
  unknown: "?",
};

export interface RelationRun {
  relation: string;
  from: number;
  to: number;
  first: number;
}

/** Consecutive steps with the same wind relation (steps without wind break a run). With
 * `breaks` (e.g. the local midnights), a run is also cut at each break, so a relation that
 * lasts for days gets a label in every day rather than one in the middle of the run. */
export function relationRuns(steps: readonly StepInfo[], breaks: readonly number[] = []): RelationRun[] {
  const out: RelationRun[] = [];
  let prev = -2;
  for (const s of steps) {
    const rel = s.step.wind?.relation;
    if (!rel) continue;
    const last = out[out.length - 1];
    if (last && last.relation === rel && prev === s.i - 1) last.to = s.x1;
    else out.push({ relation: rel, from: s.x0, to: s.x1, first: s.i });
    prev = s.i;
  }
  if (!breaks.length) return out;
  const cuts = [...breaks].sort((a, b) => a - b);
  return out.flatMap((run) => {
    const pieces: RelationRun[] = [];
    let from = run.from;
    for (const c of cuts) {
      if (c <= from || c >= run.to) continue;
      pieces.push({ ...run, from, to: c });
      from = c;
    }
    pieces.push({ ...run, from, to: run.to });
    return pieces;
  });
}

/** "1-hour steps" or "1-hour steps, then 3-hour steps from Tue 2:00 PM" from
 * steps_meta.segments. */
export function segmentsText(segments: SpotTimeline["steps_meta"]["segments"], tz: string): string {
  if (!segments.length) return "";
  const len = (h: number) => `${Number.isInteger(h) ? h : h.toFixed(1)}-hour steps`;
  if (segments.length === 1) return len(segments[0]!.step_hours);
  return segments
    .map((seg, i) => (i === 0 ? len(seg.step_hours) : `then ${len(seg.step_hours)} from ${shortTime(Date.parse(seg.start), tz)}`))
    .join(", ");
}

/** Where the zone's name changes inside the timeline (a daylight-saving change): the first
 * step of each new name, e.g. [{ t, label: "WET (GMT+0)" }]. Empty when one name covers the
 * whole window. */
export function zoneChanges(times: readonly number[], tz: string): Array<{ t: number; label: string }> {
  const out: Array<{ t: number; label: string }> = [];
  let last = times.length ? zoneLabel(times[0]!, tz) : "";
  for (const t of times) {
    const label = zoneLabel(t, tz);
    if (label !== last) out.push({ t, label });
    last = label;
  }
  return out;
}

// --- display text ---------------------------------------------------------------------

const FAMILY_SHORT: Record<string, string> = { ncep_gfs: "GFS", ecmwf_ifs: "ECMWF", ecmwf_aifs: "ECMWF AIFS", ncep_gefs: "GEFS" };

/** "ECMWF" for a blend member's source code, from the run's members (family names as
 * forecasters say them); the code itself when the member is unknown. */
export function memberShortName(code: string, members: ReadonlyArray<{ code: string; family: string }> | null | undefined): string {
  const m = members?.find((x) => x.code === code);
  return (m && FAMILY_SHORT[m.family]) ?? code;
}

/** The step's interpolation note: "ECMWF value aligned in time" when the blend interpolated
 * a member between that member's own steps, "Value interpolated by the source" otherwise;
 * null for a step of native model values. */
export function interpolationText(step: TimelineStep, members: ReadonlyArray<{ code: string; family: string }> | null | undefined): string | null {
  if (!step.interpolated) return null;
  const names = [...new Set((step.interpolated_members ?? []).map((c) => memberShortName(c, members)))];
  return names.length ? `${names.join(" and ")} ${names.length > 1 ? "values" : "value"} aligned in time` : "Value interpolated by the source";
}

/** An offshore (model) height given in metres, in the viewer's unit: "1.6 m" / "5.2 ft". */
export function offshoreHeightText(m: number | null | undefined, unit: HeightUnit): string {
  if (m == null || !Number.isFinite(m)) return "—";
  return unit === "m" ? `${m.toFixed(1)} m` : `${(m / 0.3048).toFixed(1)} ft`;
}

/** A tide height given in metres (relative to its datum, so it can be negative), in the
 * viewer's unit to 0.1: "1.2 m", "-0.4 ft" (never "-0.0"). */
export function tideHeightText(m: number, unit: HeightUnit): string {
  const v = Math.round((unit === "m" ? m : m / 0.3048) * 10) / 10;
  return `${(Object.is(v, -0) ? 0 : v).toFixed(1)} ${unit}`;
}

/** A nautical FROM direction: "SW 225°" (or "—"). */
export function directionText(fromDeg: number | null | undefined): string {
  return fromDeg == null || !Number.isFinite(fromDeg) ? "—" : `${compass16(fromDeg)} ${Math.round(fromDeg) % 360}°`;
}

/** "1.6 m · 12 s · SW 225°" for one wave system (height in the viewer's unit). */
export function partitionText(p: PartitionOut, unit: HeightUnit): string {
  return `${offshoreHeightText(p.height_m, unit)} · ${p.period_s != null ? `${Math.round(p.period_s)} s` : "— s"} · ${directionText(p.dir_from_deg)}`;
}

// --- quality -------------------------------------------------------------------------

export interface QualityRun {
  label: QualityLabel;
  daylight: boolean;
  from: number;
  to: number;
  first: number;
  last: number;
}

/** Consecutive steps with the same label (and the same day or night), for the strip's text
 * labels. */
export function qualityRuns(steps: readonly StepInfo[]): QualityRun[] {
  const out: QualityRun[] = [];
  for (const s of steps) {
    const label = asQuality(s.step.quality.label);
    const daylight = s.step.is_daylight;
    const last = out[out.length - 1];
    if (last && last.label === label && last.daylight === daylight && last.last === s.i - 1) {
      last.to = s.x1;
      last.last = s.i;
    } else out.push({ label, daylight, from: s.x0, to: s.x1, first: s.i, last: s.i });
  }
  return out;
}

// --- axes and curves -------------------------------------------------------------------

/** Round axis ticks from 0 to at least `max` (about 3-4 ticks). */
export function niceTicks(max: number, target = 3): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  // From 0 up to the first tick at or above max.
  const out: number[] = [0];
  while (out[out.length - 1]! < max - 1e-9) out.push(Math.round(out.length * step * 1000) / 1000);
  return out;
}

/** A smooth SVG path through points (monotone cubic, so it never overshoots between them). */
export function smoothPath(points: ReadonlyArray<readonly [number, number]>): string {
  const n = points.length;
  if (n === 0) return "";
  const f = (v: number) => Math.round(v * 10) / 10;
  if (n === 1) return `M${f(points[0]![0])},${f(points[0]![1])}`;
  const dx = (i: number) => points[i + 1]![0] - points[i]![0];
  const slope = (i: number) => (points[i + 1]![1] - points[i]![1]) / (dx(i) || 1);
  const m: number[] = new Array(n).fill(0);
  m[0] = slope(0);
  m[n - 1] = slope(n - 2);
  for (let i = 1; i < n - 1; i++) {
    const a = slope(i - 1);
    const b = slope(i);
    m[i] = a * b <= 0 ? 0 : (3 * (dx(i - 1) + dx(i))) / ((2 * dx(i) + dx(i - 1)) / a + (dx(i) + 2 * dx(i - 1)) / b);
  }
  let d = `M${f(points[0]![0])},${f(points[0]![1])}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = points[i]!;
    const [x1, y1] = points[i + 1]!;
    const h = (x1 - x0) / 3;
    d += `C${f(x0 + h)},${f(y0 + m[i]! * h)},${f(x1 - h)},${f(y1 - m[i + 1]! * h)},${f(x1)},${f(y1)}`;
  }
  return d;
}

// --- labels --------------------------------------------------------------------------

const LONG_DAY = new Map<string, Intl.DateTimeFormat>();

/** "SAST (GMT+2)", "PDT (GMT-7)", or just "GMT-5" where the zone has no abbreviation. */
export function zoneLabel(t: number, tz: string): string {
  const name = zoneName(t, tz);
  return /^(GMT|UTC)/.test(name) ? name : `${name} (GMT${formatOffset(zoneOffsetMs(t, tz))})`;
}

function formatOffset(ms: number): string {
  const sign = ms < 0 ? "-" : "+";
  const minutes = Math.round(Math.abs(ms) / 60_000);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${sign}${h}:${String(m).padStart(2, "0")}` : `${sign}${h}`;
}

/** "Thursday, Oct 15 · 7:00 AM SAST" (the spot's zone, with its own name). */
export function selectedTimeTitle(t: number, tz: string): string {
  let f = LONG_DAY.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "long", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hourCycle: "h12" });
    LONG_DAY.set(tz, f);
  }
  const p = Object.fromEntries(f.formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  return `${p.weekday}, ${p.month} ${p.day} · ${p.hour}:${p.minute} ${p.dayPeriod} ${zoneName(t, tz)}`;
}

const CLOCK = new Map<string, Intl.DateTimeFormat>();

/** "7:00 AM" in the spot's zone. */
export function clockTime(t: number, tz: string): string {
  let f = CLOCK.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit", hourCycle: "h12" });
    CLOCK.set(tz, f);
  }
  return f.format(new Date(t));
}

/** Local calendar date ("2026-10-15") of instant `t` in `tz`. */
export function localDate(t: number, tz: string): string {
  const wall = new Date(t + zoneOffsetMs(t, tz));
  return wall.toISOString().slice(0, 10);
}

const SHORT = new Map<string, Intl.DateTimeFormat>();

/** "Thu 7:00 AM" in the spot's zone. */
export function shortTime(t: number, tz: string): string {
  let f = SHORT.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", hour: "numeric", minute: "2-digit", hourCycle: "h12" });
    SHORT.set(tz, f);
  }
  const p = Object.fromEntries(f.formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  return `${p.weekday} ${p.hour}:${p.minute} ${p.dayPeriod}`;
}

const DATED = new Map<string, Intl.DateTimeFormat>();

/** "Fri Oct 23, 11:00 AM" in the spot's zone: a step time that stays unambiguous over a
 * horizon longer than a week. */
export function datedTime(t: number, tz: string): string {
  let f = DATED.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hourCycle: "h12" });
    DATED.set(tz, f);
  }
  const p = Object.fromEntries(f.formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  return `${p.weekday} ${p.month} ${p.day}, ${p.hour}:${p.minute} ${p.dayPeriod}`;
}

/** The slider's spoken value: "Thu Oct 15, 7:00 AM, Very Good, 5–7 ft" (with the date: the
 * horizon spans more than one Thursday). */
export function stepValueText(step: TimelineStep, tz: string, heightText: string): string {
  return `${datedTime(Date.parse(step.t), tz)}, ${QUALITY_LABELS[asQuality(step.quality.label)]}, ${heightText}`;
}

/** The step to show first: the one covering `now` (or the first step after it). */
export function defaultStepIndex(timeline: Pick<SpotTimeline, "steps">, now: number): number {
  const steps = stepExtents(timeline.steps);
  if (!steps.length) return -1;
  const covering = steps.find((s) => s.x0 <= now && now < s.x1);
  if (covering) return covering.i;
  const after = steps.find((s) => s.t >= now);
  return after ? after.i : steps.length - 1;
}

/**
 * The step the spot page opens on: the step covering `now` when it is daylight, otherwise the
 * next daylight step (the next surfable light), otherwise the step covering now. The week's
 * best step is shown separately above the timeline, so the timeline opens at the present.
 */
export function openingStepIndex(timeline: Pick<SpotTimeline, "steps">, now: number): number {
  const i = defaultStepIndex(timeline, now);
  if (i < 0 || timeline.steps[i]!.is_daylight) return i;
  const next = timeline.steps.findIndex((s, k) => k > i && s.is_daylight);
  return next >= 0 ? next : i;
}

/** Parse ?t= into epoch ms (null when absent or not a date). */
export function parseTimeParam(value: string | null | undefined): number | null {
  if (!value) return null;
  const t = Date.parse(value);
  return Number.isFinite(t) ? t : null;
}

// --- the shared model ----------------------------------------------------------------------

export interface TidePoint {
  t: number;
  h: number;
}

export interface TideEvent {
  t: number;
  kind: "high" | "low";
  h: number;
}

export interface TimelineModel {
  tz: string;
  steps: StepInfo[];
  times: number[];
  days: DayInfo[];
  nights: Array<[number, number]>;
  /** The axis: from the first step's extent to the last's. */
  start: number;
  end: number;
  /** Partition kinds present in the response, in display order. */
  kinds: PartitionKind[];
  tide: { curve: TidePoint[]; events: TideEvent[] } | null;
}

/** Everything the rows share, computed once per response. */
export function timelineModel(tl: SpotTimeline): TimelineModel {
  const steps = stepExtents(tl.steps);
  const days = dayInfos(tl.days);
  const start = steps.length ? steps[0]!.x0 : days[0]?.start ?? 0;
  const end = steps.length ? steps[steps.length - 1]!.x1 : days[days.length - 1]?.end ?? 0;
  const hasTide = tl.tide.status !== "unavailable" && tl.tide.curve.length > 1;
  return {
    tz: tl.timezone,
    steps,
    times: steps.map((s) => s.t),
    days,
    nights: nightBands(days),
    start,
    end,
    kinds: partitionKinds(tl.steps),
    tide: hasTide
      ? {
          curve: tl.tide.curve.map((p) => ({ t: Date.parse(p.t), h: p.height_m })).filter((p) => p.t >= start && p.t <= end),
          events: tl.tide.events.map((e) => ({ t: Date.parse(e.t), kind: e.kind, h: e.height_m })).filter((e) => e.t >= start && e.t <= end),
        }
      : null,
  };
}
