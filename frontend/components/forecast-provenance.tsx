"use client";

import { Clock } from "lucide-react";
import { useEffect, useState } from "react";

import { DemoBadge } from "@/components/quality";
import { safeExternalUrl } from "@/lib/confidence";
import { relativeTime } from "@/lib/format";
import { cycleLabel } from "@/lib/map-time";
import { componentFetchedText, componentRuns } from "@/lib/model-runs";
import { creditLinks } from "@/lib/provider";
import { cn } from "@/lib/utils";
import type { AttributionLink, RunMember } from "@/types/api";

/** The browser clock in epoch ms, refreshed every minute (for "in 2 h" and "model run 16 h ago"
 * labels): read once when the component mounts and then by the interval, so re-renders in
 * between show the same time. */
export function useNowMs(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

/**
 * The credit and licence links the data sources ask for, shown next to their data: Open-Meteo
 * ("Weather data by Open-Meteo.com", CC BY 4.0), ECMWF ("Copyright ECMWF", CC BY 4.0), NOAA,
 * and Windy ("Contains data from the Windy database", lib/provider creditLinks; `sourceCode`
 * adds it for Windy data the links do not credit yet). The API's `source.attribution_links`
 * (every member's for a blend). Only http(s) URLs become links. Nothing is shown without links
 * (e.g. the demo source).
 */
export function AttributionLinks({
  links: given,
  sourceCode,
  dark = false,
  className,
}: {
  links: readonly AttributionLink[] | null | undefined;
  sourceCode?: string | null;
  dark?: boolean;
  className?: string;
}) {
  const links = creditLinks(given, sourceCode);
  if (!links.length) return null;
  return (
    <p className={cn("text-[11px] text-muted-foreground", className)} data-testid="attribution-links">
      <span className="font-medium">Data: </span>
      {links.map((l, i) => {
        const href = safeExternalUrl(l.href);
        return (
          <span key={`${l.label}-${l.href}`}>
            {i > 0 ? " · " : ""}
            {href ? (
              <a href={href} target="_blank" rel="noopener noreferrer" className={cn("underline underline-offset-2", dark ? "hover:text-white" : "hover:text-ink")}>
                {l.label}
              </a>
            ) : (
              l.label
            )}
          </span>
        );
      })}
    </p>
  );
}

/** The distinct credit links of several items (e.g. the events on a page), in first-seen
 * order, so one credit line under a list covers every card's model values. */
export function mergeAttributionLinks(items: ReadonlyArray<{ attribution_links?: readonly AttributionLink[] | null }> | null | undefined): AttributionLink[] {
  const seen = new Map<string, AttributionLink>();
  for (const item of items ?? []) for (const l of item.attribution_links ?? []) if (!seen.has(`${l.label}|${l.href}`)) seen.set(`${l.label}|${l.href}`, l);
  return [...seen.values()];
}

/**
 * Where a forecast comes from: the source, every model run behind it with its own init time
 * (a blend's issue time is only its oldest member's, so each member is named), when the data
 * was fetched and, with `links`, the sources' credit and licence links. `members` is the
 * API's `run.members`; without it the run's issue time is shown as the model run. When the
 * waves and the wind come from different model runs (a component refreshed alone), both runs
 * and both fetch times are named.
 */
export function ForecastProvenance({
  source,
  run,
  members,
  generatedAt,
  links,
  sourceCode,
  className,
}: {
  source: { name: string; is_demo: boolean } | null | undefined;
  run: { issued_at: string; completed_at?: string | null } | null | undefined;
  members?: readonly RunMember[] | null;
  /** The server's time for the response: a browser clock behind it never makes the fetch look
   * newer than the response. */
  generatedAt?: string | null;
  /** The sources' credit and licence links (`source.attribution_links`). */
  links?: readonly AttributionLink[] | null;
  /** The source's code (Windy data adds its credit, lib/provider creditLinks). */
  sourceCode?: string | null;
  className?: string;
}) {
  const clock = useNowMs();
  if (!source && !run) return null;
  const served = generatedAt ? Date.parse(generatedAt) : NaN;
  const now = new Date(Number.isFinite(served) ? Math.max(clock, served) : clock);
  const runs = (members ?? []).filter((m) => m.init_time);
  const parts = componentRuns(members);
  const fetchedParts = parts ? componentFetchedText(parts, now) : null;
  return (
    <div className={cn("space-y-0.5 text-[11px] text-muted-foreground", className)} data-testid="forecast-provenance">
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        {source && <span className="font-medium text-foreground/80">{source.name}</span>}
        {source?.is_demo && <DemoBadge />}
      </div>
      {parts ? (
        // The waves and the wind come from different model runs (one was refreshed alone):
        // each is named with its own run, never only the older one.
        <div data-testid="provenance-members">
          <span data-testid="provenance-member">Wave run {cycleLabel(parts.wave.init_time!)}</span>
          {" · "}
          <span data-testid="provenance-member">Wind run {cycleLabel(parts.atmosphere.init_time!)}</span>
        </div>
      ) : runs.length > 0 ? (
        <div data-testid="provenance-members">
          {runs.length > 1 ? "Model runs: " : "Model run: "}
          {runs.map((m, i) => (
            <span key={`${m.code}-${m.run_key}`} data-testid="provenance-member">
              {i > 0 ? " · " : ""}
              {runs.length > 1 ? `${m.name} ` : ""}
              {cycleLabel(m.init_time!)}
            </span>
          ))}
        </div>
      ) : (
        run && <div data-testid="provenance-members">Model run: {cycleLabel(run.issued_at)}</div>
      )}
      {fetchedParts ? (
        <div className="flex items-center gap-1" data-testid="provenance-fetched">
          <Clock className="size-3" aria-hidden="true" /> Fetched {fetchedParts}
        </div>
      ) : (
        run?.completed_at && (
          <div className="flex items-center gap-1" title={new Date(run.completed_at).toISOString()} data-testid="provenance-fetched">
            <Clock className="size-3" aria-hidden="true" /> Fetched {relativeTime(run.completed_at, now)}
          </div>
        )
      )}
      <AttributionLinks links={links} sourceCode={sourceCode} />
    </div>
  );
}
