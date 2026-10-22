"use client";

import { AlertTriangle } from "lucide-react";

import type { FieldIndex } from "@/lib/map-fields";
import { cycleLabel, leadLabel, modelName } from "@/lib/map-time";
import { utcDateTime } from "@/lib/sync";
import { cn } from "@/lib/utils";
import type { MapSnapshot } from "@/types/api";

const HOUR = 3_600_000;

/** Age in hours of the model data behind the map, and whether it counts as out of date. */
export function mapStaleness(
  index: FieldIndex | null | undefined,
  snapshot: MapSnapshot | null | undefined,
  now: number,
): { stale: boolean; ageHours: number | null; limitHours: number } {
  const limitHours = index?.stale_after_hours ?? 12;
  const ages: number[] = [];
  // Ocean layers: the server's age of the served model cycle.
  if (index?.available && index.stale && index.age_hours != null) ages.push(index.age_hours);
  // Spot predictions: the newest run behind them, counted from its issue time.
  if (snapshot?.stale && snapshot.newest_run_issued_at) {
    const h = (now - Date.parse(snapshot.newest_run_issued_at)) / HOUR;
    if (Number.isFinite(h)) ages.push(h);
  }
  const stale = (!!index?.available && index.stale) || !!snapshot?.stale;
  return { stale, ageHours: ages.length ? Math.max(...ages) : null, limitHours };
}

/**
 * The model run behind the ocean layers and when it reached the map: the sync publication that
 * made it the served run (`published_at`), or, without one (older backends, or before the first
 * sync), when storing the run finished (`updated_at`). Null without layers or a cycle time.
 */
export function mapPublication(index: FieldIndex | null | undefined): { cycle: string; at: string | null; published: boolean } | null {
  if (!index?.available || !index.cycle_time) return null;
  if (index.published_at) return { cycle: index.cycle_time, at: index.published_at, published: true };
  return { cycle: index.cycle_time, at: index.updated_at ?? null, published: false };
}

/** "model run 12Z Oct 18 (Z = UTC), published Oct 18, 14:32 UTC" (or "stored ..." without a
 * publication); null without layers. */
export function publicationText(index: FieldIndex | null | undefined): string | null {
  const pub = mapPublication(index);
  if (!pub) return null;
  const when = pub.at ? `, ${pub.published ? "published" : "stored"} ${utcDateTime(pub.at)}` : "";
  return `model run ${cycleLabel(pub.cycle)} (Z = UTC)${when}`;
}

/**
 * The map's freshness chip: "Forecast out of date — model run 54 h old" once the newest model
 * run behind the ocean layers or the spot markers is older than its limit (12 h by default).
 * The layers and markers keep showing that run; this says so. Nothing while the data is fresh.
 * When a sync published the layers' run, larger screens add when: "· published Oct 16, 14:32
 * UTC" (phones keep one line; screen readers still get it). The tooltip names the run.
 */
export function MapFreshness({
  index,
  snapshot,
  now,
  className,
}: {
  index: FieldIndex | null | undefined;
  snapshot: MapSnapshot | null | undefined;
  /** Epoch ms for ages computed here (the spot predictions' run). */
  now: number;
  className?: string;
}) {
  const { stale, ageHours, limitHours } = mapStaleness(index, snapshot, now);
  if (!stale) return null;
  const pub = mapPublication(index);
  const run = publicationText(index);
  return (
    <p
      role="status"
      data-testid="map-stale"
      title={run ? `Ocean layers: ${run}` : undefined}
      className={cn(
        "flex w-fit items-center gap-1.5 rounded-full bg-amber-300 px-3 py-1.5 text-xs font-semibold text-[#2a1d00] shadow-[0_6px_22px_rgba(0,0,0,0.38)]",
        className,
      )}
    >
      <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
      {ageHours != null ? `Forecast out of date — model run ${Math.round(ageHours)} h old` : `Forecast out of date — model run over ${Math.round(limitHours)} h old`}
      {pub?.published && pub.at && (
        <span className="sr-only font-medium sm:not-sr-only sm:-ml-1" data-testid="map-published">
          {" "}
          · published {utcDateTime(pub.at)}
        </span>
      )}
    </p>
  );
}

/**
 * Beside the timeline bubble: the model run and the selected step's lead time after it,
 * "GFS 18Z Oct 11 · +6 h", or "analysis" at the run's own time (still model output, not an
 * observation). Phones show the lead time only (screen readers still get the run). The
 * tooltip has the full source and when the run was published (or stored, before any sync
 * published one); the legend has the source too.
 */
export function ModelRunLabel({ index, at }: { index: FieldIndex | null | undefined; at: string | null }) {
  if (!index?.available || !index.source || !index.cycle_time) return null;
  const run = `${modelName(index.source)} ${cycleLabel(index.cycle_time)}`;
  const lead = leadLabel(at, index.cycle_time);
  return (
    <span
      data-testid="map-lead-time"
      data-published-at={index.published_at ?? undefined}
      title={`${index.source.name}, ${publicationText(index)}. Forecast values, not observations.`}
      // Inline, not a flex box: flex items would drop the spaces around the "·".
      className="pointer-events-auto inline-block rounded-full bg-[#15191d]/90 px-2 py-0.5 text-[11px] font-medium whitespace-nowrap text-white/85 tabular-nums shadow ring-1 ring-white/15"
    >
      <span className="sr-only">Model run </span>
      <span className="sr-only sm:not-sr-only">
        {run}
        {lead && " · "}
      </span>
      {lead && (
        <span className={cn(lead === "analysis" ? "font-semibold text-[#c4ec4a]" : "text-white")} data-testid="map-lead">
          {lead}
        </span>
      )}
    </span>
  );
}
