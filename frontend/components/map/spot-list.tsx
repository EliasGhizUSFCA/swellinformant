"use client";

import { ChevronDown, MapPin } from "lucide-react";

import { QualityBadge } from "@/components/quality";
import { cn } from "@/lib/utils";
import type { Conditions, SpotSummary } from "@/types/api";

import { FOCUS, GLASS } from "./map-ui";

/** Sort key for the "best at the selected time" list: daylight hours above night ones. */
export function rankKey(c: Conditions | null | undefined): number {
  if (!c) return -1;
  return (c.is_daylight ? 1000 : 0) + c.score;
}

/**
 * Collapsible best-spots sheet: the 15 best visible spots at the selected time, titled "Best
 * spots now" at the current step and "Best spots · 9am Sun PDT" at any other (`timeLabel`).
 */
export function SpotList({
  spots,
  total,
  conditions,
  onSelect,
  open,
  onOpenChange,
  updating,
  note,
  timeLabel,
  className,
}: {
  spots: SpotSummary[];
  total: number;
  conditions: (slug: string) => Conditions | null | undefined;
  onSelect: (slug: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  updating: boolean;
  note?: string | null;
  /** The selected time when it is not the current step (null = now). */
  timeLabel?: string | null;
  className?: string;
}) {
  const title = timeLabel ? `Best spots · ${timeLabel}` : "Best spots now";
  const ranked = [...spots]
    // Daylight first: a high score at night is not surf anyone can ride.
    .sort((a, b) => rankKey(conditions(b.slug)) - rankKey(conditions(a.slug)))
    .slice(0, 15);
  return (
    <section aria-label={title} className={cn("flex min-h-0 flex-col overflow-hidden rounded-2xl", GLASS, className)}>
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        aria-controls="map-top-spots"
        className={cn("flex w-full items-center gap-2 rounded-2xl px-4 py-3 text-left", FOCUS)}
      >
        <MapPin className="size-4 text-[#c4ec4a]" aria-hidden />
        <span className="min-w-0 truncate text-sm font-semibold" data-testid="best-spots-title">
          {title}
        </span>
        <span className="ml-auto shrink-0 text-xs text-white/70" data-testid="visible-count">
          Showing {spots.length} of {total} spots
          {updating && " · updating…"}
        </span>
        <ChevronDown className={cn("size-4 shrink-0 transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      {open && (
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain border-t border-white/10 px-2 pb-2">
          {note && <p className="px-2 pt-2 text-xs text-amber-200">{note}</p>}
          <ul className="space-y-0.5 pt-1" id="map-top-spots" data-testid="top-spots">
            {ranked.map((s) => {
              const c = conditions(s.slug);
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
        </div>
      )}
    </section>
  );
}
