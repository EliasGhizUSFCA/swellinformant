"use client";

import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { GLASS } from "@/components/map/map-ui";
import { useForecastVersion } from "@/hooks/use-forecast-version";
import { zoneName } from "@/lib/format";
import { cn } from "@/lib/utils";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
/** How long the label stays highlighted after a new version's data appeared. */
const HIGHLIGHT_MS = 8_000;

/** "just now", "5 min ago", "3 h ago", "2 days ago" (an age, never a future time). */
export function ageText(fromMs: number, nowMs: number): string {
  const age = Math.max(0, nowMs - fromMs);
  if (age < MINUTE) return "just now";
  if (age < HOUR) return `${Math.floor(age / MINUTE)} min ago`;
  if (age < 48 * HOUR) return `${Math.floor(age / HOUR)} h ago`;
  return `${Math.floor(age / (24 * HOUR))} days ago`;
}

const clock = (timeZone?: string) => new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const day = (timeZone?: string) => new Intl.DateTimeFormat("en-US", { timeZone, month: "short", day: "numeric" });
const full = (timeZone?: string) =>
  new Intl.DateTimeFormat("en-US", { timeZone, weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short" });

/** "14:32" in the viewer's time zone (`timeZone` for tests), "Oct 26, 14:32" on another day. */
export function updatedClock(iso: string, nowMs: number, timeZone?: string): string {
  const t = new Date(iso);
  const sameDay = day(timeZone).format(t) === day(timeZone).format(new Date(nowMs));
  return sameDay ? clock(timeZone).format(t) : `${day(timeZone).format(t)}, ${clock(timeZone).format(t)}`;
}

/** Epoch ms now, re-read every `everyMs` (a local clock for relative ages, no request). */
function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs]);
  return now;
}

/**
 * "Forecast updated 14:32 AEDT · 5 min ago": when the forecast shown was published (the version
 * whose data the page shows, in the viewer's zone or `timeZone`, named, with its age), or on a
 * spot page when that spot's forecast was last updated (`updatedAt`). When a newer version replaces
 * it on an open page the label changes with the data, is highlighted briefly, and screen readers
 * hear "New forecast loaded" (aria-live, once the new data is on screen). Nothing before the
 * version is known or while nothing has been published. `stale`: the data under the label is
 * older than the version (a spot whose refresh failed keeps its previous run), so the label is
 * not drawn and the page's stale notice speaks for it; the announcement region stays. `tone`
 * "map" is the floating dark pill of the map's controls.
 */
export function ForecastUpdated({
  tone = "page",
  stale = false,
  updatedAt,
  timeZone,
  className,
}: {
  tone?: "page" | "map";
  stale?: boolean;
  /** A spot page: when this spot's forecast was last updated (its `freshness.last_updated_at`;
   * null while unknown). The label shows that time instead of the version's publication, and a
   * new version is highlighted and announced only when it updated this spot: a version that
   * only retried other spots changes nothing here. Undefined (the map): the version's time. */
  updatedAt?: string | null;
  /** The zone the clock is shown in (a spot page: the spot's, like its other times); the
   * viewer's by default. The zone's name follows the clock. */
  timeZone?: string;
  className?: string;
}) {
  const { status, shown: version, renderedId, changedAt } = useForecastVersion();
  const now = useNow(30_000);
  // The replacing version whose data is on screen: highlighted until HIGHLIGHT_MS later.
  const fresh = renderedId != null && renderedId === version?.version_id;
  const [faded, setFaded] = useState<number | null>(null);
  useEffect(() => {
    if (!fresh) return;
    const id = window.setTimeout(() => setFaded(renderedId), HIGHLIGHT_MS);
    return () => window.clearTimeout(id);
  }, [fresh, renderedId]);
  // The spot's update time under each version shown, and the one before it (`updatedAt`).
  const versionId = version?.version_id ?? null;
  const [track, setTrack] = useState<{ id: number | null; at: string | null | undefined; before: string | null | undefined }>({
    id: null,
    at: undefined,
    before: undefined,
  });
  if (updatedAt !== undefined && (track.id !== versionId || track.at !== updatedAt)) {
    // Adjusted while rendering (React's pattern for state that follows props): the first
    // render under a new version already compares with the previous version's time.
    setTrack(track.id === versionId ? { ...track, at: updatedAt } : { id: versionId, at: updatedAt, before: track.id == null ? undefined : track.at });
  }
  const spotChanged = updatedAt === undefined || (track.id === versionId && track.before !== undefined && track.before !== updatedAt);
  const announced = fresh && spotChanged;
  const highlight = announced && faded !== renderedId;

  const published = version?.published_at ?? null;
  if (status !== "known" || version?.version_id == null || !published) return null;
  const shownIso = updatedAt ?? published;
  const at = Date.parse(shownIso);
  if (!Number.isFinite(at)) return null;
  const nowMs = Math.max(now, changedAt ?? 0);
  const zone = timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  // The live region stays in place whatever is drawn (a region added with its text is not
  // read out); it is absolutely positioned, so it takes no room in the header.
  const announcement = (
    <span className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid="forecast-updated-announcement">
      {announced && !stale ? "New forecast loaded" : ""}
    </span>
  );
  return (
    <>
      {/* A stale spot: "updated just now" would call its old data current. */}
      {!stale && (
        <span
          data-testid="forecast-updated"
          data-version-id={version.version_id}
          data-published-at={published}
          data-updated-at={shownIso}
          data-highlight={highlight ? "" : undefined}
          title={`Forecast version ${version.version_id}, published ${full().format(new Date(at))}. Open pages pick up new versions by themselves.`}
          className={cn(
            "inline-flex w-fit items-center gap-1.5 rounded-full text-xs whitespace-nowrap tabular-nums transition-colors duration-700",
            // On short screens (phones in landscape) the map keeps its little height for the map:
            // the pill is only read out there, not drawn.
            tone === "map" ? cn("px-3 py-1.5 [@media(max-height:500px)]:sr-only", GLASS) : "border bg-card px-2.5 py-0.5 text-muted-foreground",
            highlight && (tone === "map" ? "ring-[#c4ec4a]/70" : "border-emerald-300 bg-emerald-50 text-emerald-800"),
            className,
          )}
        >
          <RefreshCw className={cn("size-3 shrink-0", tone === "map" ? "text-[#c4ec4a]" : "text-ocean")} aria-hidden />
          <span>
            Forecast updated{" "}
            <span className={cn("font-medium", tone === "map" ? "text-white" : "text-ink")}>
              {updatedClock(shownIso, nowMs, timeZone)} <span data-testid="forecast-updated-zone">{zoneName(at, zone)}</span>
            </span>
            <span className={tone === "map" ? "text-white/70" : undefined} data-testid="forecast-updated-age">
              {" "}
              · {ageText(at, nowMs)}
            </span>
          </span>
        </span>
      )}
      {announcement}
    </>
  );
}
