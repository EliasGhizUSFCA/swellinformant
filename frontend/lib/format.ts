/** Formatting helpers. Times are always rendered in an explicit IANA zone. */

export type Units = "ft" | "m";

const FT_PER_M = 3.28084;

export function toUnits(valueFt: number, units: Units): number {
  return units === "m" ? valueFt / FT_PER_M : valueFt;
}

export function fromUnits(value: number, units: Units): number {
  return units === "m" ? value * FT_PER_M : value;
}

function trim(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1).replace(/\.0$/, "");
}

/** "6–9 ft" or "1.8–2.7 m" from a breaking-height range stored in feet. */
export function heightRange(minFt: number, maxFt: number, units: Units = "ft"): string {
  const a = toUnits(minFt, units);
  const b = toUnits(maxFt, units);
  const round = (v: number) => (units === "m" ? Math.round(v * 10) / 10 : Math.round(v * 2) / 2);
  const lo = trim(round(a));
  const hi = trim(round(b));
  return lo === hi ? `${lo} ${units}` : `${lo}–${hi} ${units}`;
}

export function metres(value: number | null | undefined, digits = 1): string {
  return value == null ? "—" : `${value.toFixed(digits)} m`;
}

export function seconds(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value)} s`;
}

export function kmh(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value)} km/h`;
}

const POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

export function compass(deg: number | null | undefined): string {
  if (deg == null) return "—";
  return POINTS[Math.floor((((deg % 360) + 360) % 360 + 11.25) / 22.5) % 16] ?? "—";
}

export function formatDate(iso: string, timeZone: string, opts: Intl.DateTimeFormatOptions = {}): string {
  return new Intl.DateTimeFormat("en-US", { timeZone, month: "short", day: "numeric", ...opts }).format(new Date(iso));
}

export function formatDateTime(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(iso));
}

const part = (parts: Intl.DateTimeFormatPart[], type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";

/**
 * A forecast time in the spot's own zone: "Thu, Oct 15, 7:00 AM"; with `zone` the zone's own
 * name is added as the spot timeline writes it (`zoneName`: "… 7:00 AM SAST", "… 1:30 AM
 * EDT", the offset "GMT+5:45" where no abbreviation exists). `hour12: false` gives "07:00".
 */
export function formatSpotDateTime(
  iso: string,
  tz: string,
  opts: { zone?: boolean; hour12?: boolean; weekday?: "short" | "long" } = {},
): string {
  const hour12 = opts.hour12 ?? true;
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    weekday: opts.weekday ?? "short",
    month: "short",
    day: "numeric",
    hour: hour12 ? "numeric" : "2-digit",
    minute: "2-digit",
    hourCycle: hour12 ? "h12" : "h23",
  }).formatToParts(new Date(iso));
  const time = `${part(parts, "hour")}:${part(parts, "minute")}${hour12 ? ` ${part(parts, "dayPeriod")}` : ""}`;
  const text = `${part(parts, "weekday")}, ${part(parts, "month")} ${part(parts, "day")}, ${time}`;
  return opts.zone ? `${text} ${zoneName(Date.parse(iso), tz)}` : text;
}

/**
 * A short forecast time in the spot's zone for compact cards: "Thu 7:00 AM SAST" (weekday,
 * time and zone; no date, so only for times within the coming week). Use
 * formatSpotDateTime when the date matters.
 */
export function formatSpotTime(iso: string, tz: string, opts: { zone?: boolean } = {}): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
    hourCycle: "h12",
  }).formatToParts(new Date(iso));
  const text = `${part(parts, "weekday")} ${part(parts, "hour")}:${part(parts, "minute")} ${part(parts, "dayPeriod")}`;
  return opts.zone === false ? text : `${text} ${zoneName(Date.parse(iso), tz)}`;
}

/** Hour-axis label at epoch milliseconds `t` in `tz`: "12 AM", "6 AM", "12 PM", "6 PM" ("6:30 AM"
 * where the zone's offset is not a whole hour from the tick). */
export function formatHourTick(t: number, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit", hourCycle: "h12" }).formatToParts(new Date(t));
  const minute = part(parts, "minute");
  return `${part(parts, "hour")}${minute === "00" ? "" : `:${minute}`} ${part(parts, "dayPeriod")}`;
}

/** Short name of the zone's offset at that instant: "PDT", "EST", "GMT+2", "GMT+5:45". */
export function zoneAbbrev(iso: string, tz: string): string {
  return part(new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "short" }).formatToParts(new Date(iso)), "timeZoneName");
}

// English locales tried, in order, for a zone's own abbreviation: en-US alone prints "GMT+2"
// for Johannesburg, en-ZA prints "SAST". Only the zone name comes from these locales; dates
// and times are always formatted in en-US.
const ZONE_LOCALES = ["en-US", "en-GB", "en-AU", "en-ZA", "en-IE", "en-NZ", "en-IN", "en-CA"];
const ZONE_NAME = new Map<string, string>();

/**
 * The zone's name at instant `t` as people there write it ("SAST", "PDT", "WITA", "AEDT"),
 * or the offset ("GMT-5", "GMT+5:45") where no locale has an abbreviation. Abbreviations
 * are ambiguous worldwide ("IST"), so the offset is shown next to them (zoneLabel).
 */
export function zoneName(t: number, tz: string): string {
  const offset = zoneAbbrev(new Date(t).toISOString(), tz);
  const key = `${tz}|${offset}`;
  const cached = ZONE_NAME.get(key);
  if (cached) return cached;
  let name = offset;
  for (const locale of ZONE_LOCALES) {
    try {
      const v = new Intl.DateTimeFormat(locale, { timeZone: tz, timeZoneName: "short" }).formatToParts(new Date(t)).find((x) => x.type === "timeZoneName")?.value;
      if (v && !/^(GMT|UTC)/.test(v)) {
        name = v;
        break;
      }
    } catch {
      // A runtime without this locale: try the next one.
    }
  }
  ZONE_NAME.set(key, name);
  return name;
}

/** Plain calendar dates ("2026-07-10") rendered without any timezone shifting. */
export function formatCalendarDate(value: string | null | undefined, opts: Intl.DateTimeFormatOptions = {}): string {
  if (!value) return "—";
  const [y, m, d] = value.slice(0, 10).split("-").map(Number);
  return new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric", ...opts }).format(
    new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1)),
  );
}

/** "Jul 12–14" / "Jul 30 – Aug 2" in the spot's local calendar. */
export function formatRange(startIso: string, endIso: string, timeZone: string): string {
  const parts = (iso: string) => {
    const f = new Intl.DateTimeFormat("en-US", { timeZone, month: "short", day: "numeric" }).formatToParts(new Date(iso));
    return { month: f.find((p) => p.type === "month")?.value ?? "", day: f.find((p) => p.type === "day")?.value ?? "" };
  };
  const a = parts(startIso);
  const b = parts(new Date(new Date(endIso).getTime() - 60_000).toISOString());
  if (a.month === b.month && a.day === b.day) return `${a.month} ${a.day}`;
  if (a.month === b.month) return `${a.month} ${a.day}–${b.day}`;
  return `${a.month} ${a.day} – ${b.month} ${b.day}`;
}

export function formatDuration(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

export function formatMoney(amount: string | number | null | undefined, currency: string | null | undefined): string {
  if (amount == null || !currency) return "—";
  const value = typeof amount === "string" ? Number(amount) : amount;
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 0 }).format(value);
  } catch {
    return `${Math.round(value)} ${currency}`;
  }
}

export function relativeTime(iso: string, now: Date = new Date()): string {
  const diff = new Date(iso).getTime() - now.getTime();
  const abs = Math.abs(diff);
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["day", 86_400_000],
    ["hour", 3_600_000],
    ["minute", 60_000],
  ];
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, ms] of units) {
    if (abs >= ms || unit === "minute") return rtf.format(Math.round(diff / ms), unit);
  }
  return "now";
}

export function leadLabel(days: number): string {
  if (days < 0) return "in progress";
  if (days < 1) return "within a day";
  return `in ${Math.round(days)} day${Math.round(days) === 1 ? "" : "s"}`;
}

export function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
