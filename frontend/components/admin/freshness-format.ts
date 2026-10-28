/**
 * Formatting for the admin freshness dashboard. Times are shown in UTC (model cycles and
 * provider quotas are UTC), durations in the largest unit that keeps them readable.
 */
import type { BadgeVariant as Tone } from "@/lib/delivery";

import type { AdminUsageWindow } from "./freshness-types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const pad = (n: number) => String(n).padStart(2, "0");

/** "28 Oct 06:00 UTC" ("—" when unknown). */
export function utc(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

/** "06Z 28 Oct": a model cycle. */
export function cycle(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const minutes = d.getUTCMinutes() ? `:${pad(d.getUTCMinutes())}` : "";
  return `${pad(d.getUTCHours())}${minutes}Z ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

/** A duration in seconds: "850 ms", "4.2 s", "45 s", "12 min", "5.3 h", "2.1 days". */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.abs(seconds);
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 90 * 60) return `${Math.round(s / 60)} min`;
  if (s < 48 * 3600) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}

/** A duration in milliseconds. */
export const durationMs = (ms: number | null | undefined) => duration(ms == null ? ms : ms / 1000);

/** A duration in minutes (e.g. a typical lag). */
export const durationMin = (minutes: number | null | undefined) => duration(minutes == null ? minutes : minutes * 60);

/** "12", "1,234", "0.5" — counts and weighted calls. */
export function count(value: number | null | undefined): string {
  if (value == null) return "—";
  return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toLocaleString("en-US", { maximumFractionDigits: 1 });
}

/** "5.1 %" of a 0..1 fraction. */
export function percent(fraction: number | null | undefined): string {
  if (fraction == null) return "—";
  const pct = fraction * 100;
  return `${pct < 10 ? pct.toFixed(1) : Math.round(pct)} %`;
}

/** A usage meter's tone: used up (danger), at or past the alert fraction (warn), else good;
 * muted when the counter could not be read. */
export function usageTone(window: AdminUsageWindow | null | undefined, alertFraction: number): Tone {
  if (!window || window.fraction == null) return "muted";
  if (window.fraction >= 1) return "danger";
  if (window.fraction >= alertFraction) return "warn";
  return "good";
}

/** "82 of 10,000 (0.8 %)", or why there is no figure. */
export function usageLine(window: AdminUsageWindow | null | undefined): string {
  if (!window) return "not counted";
  if (window.used == null) return window.limit == null ? "not counted" : `unknown of ${count(window.limit)} (counter unreadable)`;
  if (window.limit == null) return count(window.used);
  return `${count(window.used)} of ${count(window.limit)} (${percent(window.fraction)})`;
}

/** Badge tone of a sync or job status. */
export function statusTone(status: string): Tone {
  if (status === "success") return "good";
  if (status === "partial" || status === "running") return "info";
  if (status === "failed" || status === "abandoned") return "danger";
  return "muted";
}

/** Badge tone of the overall status. */
export function overallTone(status: string): Tone {
  return status === "ok" ? "good" : status === "degraded" ? "warn" : "danger";
}

/** Plain words for a stale spot's reason (backend versions.SpotFreshness). */
export function staleReason(reason: string | null | undefined): string {
  switch (reason) {
    case "refresh_failed":
      return "latest refresh failed";
    case "missing":
      return "no data";
    case "atmosphere_age":
      return "wind older than its model's interval + threshold";
    case "wave_age":
      return "waves older than their model's interval + threshold";
    case "age":
      return "older than the threshold";
    default:
      return reason ?? "stale";
  }
}
