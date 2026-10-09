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
  return `${trim(round(a))}–${trim(round(b))} ${units}`;
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

export function formatTime(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-US", { timeZone, hour: "2-digit", minute: "2-digit", hour12: false }).format(
    new Date(iso),
  );
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
