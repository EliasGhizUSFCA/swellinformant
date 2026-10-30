"use client";

import { ChevronDown, Loader2, MapPin } from "lucide-react";

import { AttributionLinks } from "@/components/forecast-provenance";
import { QualityBadge } from "@/components/quality";
import type { Freshness } from "@/lib/forecast-fields";
import { updatedAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AttributionLink, Conditions, SpotSummary } from "@/types/api";

import { FOCUS, GLASS } from "./map-ui";

/** Sort key for the "best at the selected time" list: current forecasts above out-of-date
 * ones (a stale spot is never listed as one of the best now), daylight hours above night ones. */
function rankKey(c: Conditions | null | undefined, stale: boolean): number {
  if (!c) return -1;
  return (stale ? 0 : 10_000) + (c.is_daylight ? 1000 : 0) + c.score;
}

/**
 * Collapsible best-spots sheet: the 15 best visible spots at the selected time, titled "Best
 * spots now" at the current step and "Best spots · 9am Sun PDT" at any other (`timeLabel`).
 */
export function SpotList({
  spots,
  total,
  conditions,
  freshness,
  onSelect,
  open,
  onOpenChange,
  updating,
  note,
  timeLabel,
  links,
  className,
}: {
  spots: SpotSummary[];
  total: number;
  conditions: (slug: string) => Conditions | null | undefined;
  /** The spot's served-forecast freshness (`freshness` of the map snapshot): an out-of-date
   * spot is marked and listed after the current ones. */
  freshness?: (slug: string) => Freshness | null | undefined;
  onSelect: (slug: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  updating: boolean;
  note?: string | null;
  /** The selected time when it is not the current step (null = now). */
  timeLabel?: string | null;
  /** Credit and licence links of the sources behind the ratings (the snapshot's). */
  links?: readonly AttributionLink[] | null;
  className?: string;
}) {
  const title = timeLabel ? `Best spots · ${timeLabel}` : "Best spots now";
  const isStale = (slug: string) => freshness?.(slug)?.stale === true;
  const ranked = [...spots]
    // Current forecasts first, then daylight: a high score at night is not surf anyone can ride.
    .sort((a, b) => rankKey(conditions(b.slug), isStale(b.slug)) - rankKey(conditions(a.slug), isStale(a.slug)))
    .slice(0, 15);
  return (
    <section aria-label={title} className={cn("flex min-h-0 flex-col overflow-hidden rounded-2xl", GLASS, className)}>
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        aria-controls="map-top-spots"
        className={cn("flex w-full items-center gap-1.5 rounded-2xl px-3.5 py-3 text-left", FOCUS)}
      >
        {/* The pin turns into a spinner while the list refreshes, so the header never reflows. */}
        {updating ? (
          <Loader2 className="size-4 shrink-0 text-[#c4ec4a] motion-safe:animate-spin" aria-hidden />
        ) : (
          <MapPin className="size-4 shrink-0 text-[#c4ec4a]" aria-hidden />
        )}
        {/* The selected time is the point of the title: it may wrap (between "·" and the time),
            never be cut off. */}
        <span className="min-w-0 text-sm leading-snug font-semibold" data-testid="best-spots-title">
          {timeLabel ? (
            <>
              Best spots · <span className="whitespace-nowrap">{timeLabel}</span>
            </>
          ) : (
            title
          )}
        </span>
        <span className="ml-auto shrink-0 pl-1 text-[11px] text-white/70" data-testid="visible-count">
          <span className="sr-only">Showing </span>
          {spots.length} of {total} spots
          {updating && <span className="sr-only"> · updating…</span>}
        </span>
        <ChevronDown className={cn("size-4 shrink-0 transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      {open && (
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain border-t border-white/10 px-2 pb-2">
          {note && <p className="px-2 pt-2 text-xs text-amber-200">{note}</p>}
          <ul className="space-y-0.5 pt-1" id="map-top-spots" data-testid="top-spots">
            {ranked.map((s) => {
              const c = conditions(s.slug);
              const fresh = freshness?.(s.slug);
              return (
                <li key={s.slug}>
                  <button
                    type="button"
                    onClick={() => onSelect(s.slug)}
                    className={cn("flex w-full items-center justify-between gap-2 rounded-lg px-2 py-2 text-left text-sm hover:bg-white/10", FOCUS)}
                  >
                    <span className="min-w-0 truncate">
                      {s.name} <span className="text-white/65">· {s.country}</span>
                    </span>
                    {fresh?.stale && (
                      <span className="shrink-0 rounded-full bg-amber-400/20 px-1.5 py-0.5 text-[11px] text-amber-100" data-testid="list-stale">
                        out of date
                        <span className="sr-only">
                          : {fresh.lastUpdatedAt ? `forecast last updated ${updatedAgo(fresh.lastUpdatedAt)}` : "not a current forecast"}
                        </span>
                      </span>
                    )}
                    {c && !c.is_daylight && (
                      <span className="shrink-0 text-xs text-white/65" data-testid="night-tag">
                        night
                      </span>
                    )}
                    {c ? <QualityBadge label={c.label} score={c.score} /> : <span className="shrink-0 text-xs text-white/65">no data</span>}
                  </button>
                </li>
              );
            })}
            {spots.length === 0 && <li className="px-2 py-2 text-sm text-white/70">No spots match these filters at this time.</li>}
          </ul>
          {/* The ratings come from these sources' model data: credited next to them. */}
          <AttributionLinks links={links} dark className="px-2 pt-2 text-white/65" />
        </div>
      )}
    </section>
  );
}
