"use client";

import { AlertTriangle } from "lucide-react";

import type { FieldIndex } from "@/lib/map-fields";
import { cycleLabel, leadLabel, modelName } from "@/lib/map-time";
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
 * The map's freshness chip: "Forecast out of date — model run 54 h old" once the newest model
 * run behind the ocean layers or the spot markers is older than its limit (12 h by default).
 * The layers and markers keep showing that run; this says so. Nothing while the data is fresh.
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
  return (
    <p
      role="status"
      data-testid="map-stale"
      className={cn(
        "flex w-fit items-center gap-1.5 rounded-full bg-amber-300 px-3 py-1.5 text-xs font-semibold text-[#2a1d00] shadow-[0_6px_22px_rgba(0,0,0,0.38)]",
        className,
      )}
    >
      <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
      {ageHours != null ? `Forecast out of date — model run ${Math.round(ageHours)} h old` : `Forecast out of date — model run over ${Math.round(limitHours)} h old`}
    </p>
  );
}

/**
 * Beside the timeline bubble: the model run and the selected step's lead time after it,
 * "GFS 18Z Oct 11 · +6 h", or "analysis" at the run's own time (still model output, not an
 * observation). Phones show the lead time only (screen readers still get the run); the full
 * source is in the tooltip and the legend.
 */
export function ModelRunLabel({ index, at }: { index: FieldIndex | null | undefined; at: string | null }) {
  if (!index?.available || !index.source || !index.cycle_time) return null;
  const run = `${modelName(index.source)} ${cycleLabel(index.cycle_time)}`;
  const lead = leadLabel(at, index.cycle_time);
  return (
    <span
      data-testid="map-lead-time"
      title={`${index.source.name}, model run ${cycleLabel(index.cycle_time)} (Z = UTC). Forecast values, not observations.`}
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
