"use client";

import Link from "next/link";

import { EventCard } from "@/components/event-card";
import { AttributionLinks, mergeAttributionLinks, useNowMs } from "@/components/forecast-provenance";
import { Skeleton } from "@/components/ui/skeleton";
import { useEvents, useMapConditions, useSystemStatus } from "@/hooks/queries";
import { modelRunText } from "@/lib/confidence";
import { cn } from "@/lib/utils";
import type { MapSnapshot, SwellEvent, SystemStatus } from "@/types/api";

export type ForecastFreshness = { kind: "demo" } | { kind: "fresh" | "stale"; runText: string | null } | { kind: "unknown" };

/**
 * What the landing page may say about the forecast behind its swells. Model output is a
 * forecast, never "live": demo data says so; otherwise the newest model run's age, and "out of
 * date" when the events' run is stale or every source is past its stale limit.
 */
export function forecastFreshness(status: SystemStatus | undefined, events: readonly SwellEvent[] | undefined, nowMs: number): ForecastFreshness {
  const demo = status ? status.mode.forecast_providers.includes("demo") : (events ?? []).some((e) => e.is_demo);
  if (demo) return { kind: "demo" };
  const sources = (status?.forecast.sources ?? []).filter((s) => !s.is_demo);
  const issued = sources
    .map((s) => s.latest_successful?.issued_at)
    .filter((t): t is string => !!t)
    .sort()
    .at(-1);
  const stale = (events ?? []).some((e) => e.stale) || (sources.length > 0 && sources.every((s) => s.stale));
  if (!status && !(events ?? []).length) return { kind: "unknown" };
  return { kind: stale ? "stale" : "fresh", runText: issued ? modelRunText(issued, nowMs) : null };
}

function freshnessText(f: ForecastFreshness): string {
  if (f.kind === "demo") return "demo forecast";
  if (f.kind === "unknown") return "forecast";
  const run = f.runText ? ` · ${f.runText}` : "";
  return f.kind === "stale" ? `Forecast out of date${run}` : `Forecast${run}`;
}

/** Upcoming swell events from the forecast (public), shown on the landing page. */
export function LiveSwells() {
  const { data, isLoading, isError } = useEvents(4, 57);
  const { data: status } = useSystemStatus();
  // The landing page has no demo ribbon: never call synthetic forecasts real, and never
  // call model output "live".
  const now = useNowMs();
  const freshness = forecastFreshness(status, data, now);
  return (
    <div className="rounded-3xl border border-white/15 bg-white/[0.04] p-4 backdrop-blur-sm sm:p-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold tracking-wide text-white/90">Swells on the radar</h2>
        <span
          className={cn("flex items-center gap-1.5 text-right text-xs", freshness.kind === "stale" ? "text-amber-200" : "text-white/60")}
          data-testid="forecast-freshness"
          data-freshness={freshness.kind}
        >
          <span className={cn("size-1.5 shrink-0 rounded-full", freshness.kind === "stale" ? "bg-amber-300" : "bg-seafoam")} aria-hidden="true" />
          {freshnessText(freshness)}
        </span>
      </div>
      {isLoading && (
        <div className="grid gap-3 sm:grid-cols-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-44 bg-white/10" />
          ))}
        </div>
      )}
      {isError && <p className="py-10 text-center text-sm text-white/70">The forecast service is unavailable right now.</p>}
      {data && data.length === 0 && (
        <p className="py-10 text-center text-sm text-white/70">
          No qualifying swells in the current forecast. Check the <Link className="underline" href="/map">surf map</Link>.
        </p>
      )}
      {data && data.length > 0 && (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            {data.map((e) => (
              <EventCard key={e.id} event={e} dark />
            ))}
          </div>
          {/* The cards show these sources' model swell and wind: credited next to them. */}
          <AttributionLinks links={mergeAttributionLinks(data)} dark className="mt-3 text-white/65" />
        </>
      )}
    </div>
  );
}

/** Days of forecast still ahead in the stored predictions, to the nearest day ("10-day" for
 * a 240-hour run issued a few hours ago), or null when none are ahead. */
export function horizonDays(snapshot: Pick<MapSnapshot, "range_end"> | undefined, nowMs: number): number | null {
  const end = snapshot?.range_end ? Date.parse(snapshot.range_end) : NaN;
  if (!Number.isFinite(end) || end <= nowMs) return null;
  return Math.round((end - nowMs) / 86_400_000);
}

/** The landing page's horizon stat, read from the predictions actually stored (it depends
 * on the configured providers), never a fixed number. */
export function ForecastHorizon() {
  const { data } = useMapConditions(null);
  const now = useNowMs();
  const days = horizonDays(data, now);
  return (
    <div data-testid="forecast-horizon">
      <dt className="font-display text-2xl font-semibold">{days != null && days > 0 ? `${days}-day` : "—"}</dt>
      <dd className="text-xs text-white/65">forecast horizon</dd>
    </div>
  );
}
