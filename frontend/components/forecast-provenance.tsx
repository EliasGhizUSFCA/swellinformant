"use client";

import { Clock } from "lucide-react";
import { useEffect, useState } from "react";

import { DemoBadge } from "@/components/quality";
import { safeExternalUrl } from "@/lib/confidence";
import { relativeTime } from "@/lib/format";
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

/** "18Z Oct 11": a model cycle in the convention forecasters use (cycles are UTC). */
export function cycleLabel(iso: string): string {
  const d = new Date(iso);
  const month = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short" }).format(d);
  return `${String(d.getUTCHours()).padStart(2, "0")}Z ${month} ${d.getUTCDate()}`;
}

/**
 * The credit and licence links the data sources ask for, shown next to their data: Open-Meteo
 * ("Weather data by Open-Meteo.com", CC BY 4.0), ECMWF ("Copyright ECMWF", CC BY 4.0), NOAA.
 * The API's `source.attribution_links` (every member's for a blend). Only http(s) URLs
 * become links. Nothing is shown without links (e.g. the demo source).
 */
export function AttributionLinks({ links, dark = false, className }: { links: readonly AttributionLink[] | null | undefined; dark?: boolean; className?: string }) {
  if (!links?.length) return null;
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

/**
 * Where a forecast comes from: the source, every model run behind it with its own init time
 * (a blend's issue time is only its oldest member's, so each member is named), when the data
 * was fetched and, with `links`, the sources' credit and licence links. `members` is the
 * API's `run.members`; without it the run's issue time is shown as the model run.
 */
export function ForecastProvenance({
  source,
  run,
  members,
  generatedAt,
  links,
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
  className?: string;
}) {
  const clock = useNowMs();
  if (!source && !run) return null;
  const served = generatedAt ? Date.parse(generatedAt) : NaN;
  const now = new Date(Number.isFinite(served) ? Math.max(clock, served) : clock);
  const runs = (members ?? []).filter((m) => m.init_time);
  return (
    <div className={cn("space-y-0.5 text-[11px] text-muted-foreground", className)} data-testid="forecast-provenance">
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        {source && <span className="font-medium text-foreground/80">{source.name}</span>}
        {source?.is_demo && <DemoBadge />}
      </div>
      {runs.length > 0 ? (
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
      {run?.completed_at && (
        <div className="flex items-center gap-1" title={new Date(run.completed_at).toISOString()}>
          <Clock className="size-3" aria-hidden="true" /> Fetched {relativeTime(run.completed_at, now)}
        </div>
      )}
      <AttributionLinks links={links} />
    </div>
  );
}
