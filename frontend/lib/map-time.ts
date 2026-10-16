/**
 * Map timeline: the forecast steps and their labels.
 *
 * Steps come from the ocean-layer index (`valid_times`) when it has any, otherwise from the
 * spot snapshot's cached range (start = the server's "now" hour, every `step_hours` until
 * `range_end`), exactly as before the ocean layers existed. "Now" is always the server's
 * time (the snapshot's `valid_time`), never the browser clock.
 *
 * Labels use the browser's time zone via Intl, grouping steps by local calendar date (not
 * by 24 h arithmetic), so days with a DST change simply hold 23 or 25 hours of steps.
 */

export interface SnapshotRange {
  start: string;
  end: string;
  stepHours: number;
}

const HOUR = 3_600_000;

/** Ordered, de-duplicated step times (ISO strings, used as-is for `?at=`). */
export function timelineSteps(validTimes: readonly string[] | null | undefined, range: SnapshotRange | null): string[] {
  if (validTimes && validTimes.length) {
    const seen = new Map<number, string>();
    for (const t of validTimes) {
      const ms = Date.parse(t);
      if (Number.isFinite(ms) && !seen.has(ms)) seen.set(ms, t);
    }
    return [...seen.entries()].sort((a, b) => a[0] - b[0]).map(([, t]) => t);
  }
  if (!range || !(range.stepHours > 0)) return [];
  const start = Date.parse(range.start);
  const end = Date.parse(range.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return [];
  const n = Math.max(0, Math.floor((end - start) / (range.stepHours * HOUR)));
  return Array.from({ length: n + 1 }, (_, i) => new Date(start + i * range.stepHours * HOUR).toISOString());
}

/** Index of the step closest to `iso` (0 when there is no reference time). */
export function nearestStepIndex(steps: readonly string[], iso: string | null | undefined): number {
  if (!steps.length || !iso) return 0;
  const target = Date.parse(iso);
  let best = 0;
  let gap = Infinity;
  steps.forEach((s, i) => {
    const d = Math.abs(Date.parse(s) - target);
    if (d < gap) {
      gap = d;
      best = i;
    }
  });
  return best;
}

/** Hours between consecutive steps (median, so one irregular gap does not matter). */
export function stepHours(steps: readonly string[]): number {
  if (steps.length < 2) return 3;
  const gaps = steps.slice(1).map((s, i) => (Date.parse(s) - Date.parse(steps[i]!)) / HOUR).sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)]!;
}

const dateKeyFormat = (tz: string) => new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });

export interface TimelineDay {
  key: string; // local calendar date, YYYY-MM-DD
  weekday: string; // "Sun"
  label: string; // "Sun Oct 25"
  first: number; // first step index on that day
  last: number; // last step index on that day
}

/** Consecutive steps grouped by the local calendar date in `tz`. */
export function timelineDays(steps: readonly string[], tz: string): TimelineDay[] {
  const keyOf = dateKeyFormat(tz);
  const name = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", month: "short", day: "numeric" });
  const days: TimelineDay[] = [];
  steps.forEach((s, i) => {
    const d = new Date(s);
    const key = keyOf.format(d);
    const last = days[days.length - 1];
    if (last && last.key === key) {
      last.last = i;
      return;
    }
    const parts = name.formatToParts(d);
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
    days.push({ key, weekday: get("weekday"), label: `${get("weekday")} ${get("month")} ${get("day")}`, first: i, last: i });
  });
  return days;
}

/**
 * "8am Sun PDT" (minutes only when not on the hour: "8:30am Sun IST"). `withDate` adds the
 * month and day ("8am Sun Oct 25 PDT") for timelines longer than a week, where a weekday
 * alone would name two different days.
 */
export function bubbleLabel(iso: string, tz: string, withDate = false): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZoneName: "short",
    ...(withDate ? { month: "short", day: "numeric" } : {}),
  }).formatToParts(new Date(iso));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const minute = get("minute");
  const time = `${get("hour")}${minute && minute !== "00" ? `:${minute}` : ""}${get("dayPeriod").toLowerCase()}`;
  const day = withDate ? `${get("weekday")} ${get("month")} ${get("day")}` : get("weekday");
  return `${time} ${day} ${get("timeZoneName")}`.trim();
}

/** True when the steps span more than seven local days, so weekday names repeat. */
export const weekdaysRepeat = (days: readonly TimelineDay[]): boolean => days.length > 7;

/** Long form for screen readers: "Sunday, October 25, 8:00 AM PDT". */
export function longTimeLabel(iso: string, tz: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    weekday: "long",
    month: "long",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(iso));
}

/** Indices that get an hour tick: every step that lands 3 h after the previous tick. */
export function tickIndices(steps: readonly string[]): number[] {
  const every = Math.max(1, Math.round(3 / Math.max(stepHours(steps), 0.25)));
  return steps.map((_, i) => i).filter((i) => i % every === 0);
}

/**
 * A model cycle the way forecasters write it, in UTC: "18Z Oct 11" ("18:30Z Oct 11" off the
 * hour).
 */
export function cycleLabel(iso: string): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const minute = get("minute");
  return `${get("hour")}${minute && minute !== "00" ? `:${minute}` : ""}Z ${get("month")} ${get("day")}`;
}

/**
 * Lead time of a valid time after its model cycle: "+6 h", "analysis" at the cycle time
 * itself (the model's initial state, still model output), "−3 h" before it. Null when either
 * time is missing.
 */
export function leadLabel(validIso: string | null | undefined, cycleIso: string | null | undefined): string | null {
  if (!validIso || !cycleIso) return null;
  const h = Math.round((Date.parse(validIso) - Date.parse(cycleIso)) / HOUR);
  if (!Number.isFinite(h)) return null;
  if (h === 0) return "analysis";
  return h > 0 ? `+${h} h` : `−${-h} h`;
}

/** Short model name for a field or forecast source: "GFS", "ECMWF", else its name without the
 * parenthesised detail. */
export function modelName(source: { code: string; name: string }): string {
  const text = `${source.code} ${source.name}`;
  if (/gfs/i.test(text)) return "GFS";
  if (/ecmwf|\bifs\b/i.test(text)) return "ECMWF";
  return source.name.replace(/\s*\(.*\)\s*$/, "");
}

/** The selected map time as a `?t=` value: ISO 8601 in UTC, without milliseconds. */
export const timeParam = (iso: string): string => new Date(iso).toISOString().replace(".000Z", "Z");
