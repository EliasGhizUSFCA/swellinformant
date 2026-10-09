import { CalendarDays, Wind } from "lucide-react";
import Link from "next/link";

import { ConfidenceBadge, DemoBadge, QualityBadge } from "@/components/quality";
import { compass, formatRange, heightRange, leadLabel, metres, seconds, type Units } from "@/lib/format";
import { colorForScore, WIND_RELATION_LABEL } from "@/lib/quality";
import type { SwellEvent } from "@/types/api";

export function EventCard({ event, units = "ft", dark = false }: { event: SwellEvent; units?: Units; dark?: boolean }) {
  const tz = event.spot.timezone;
  return (
    <Link
      href={`/spots/${event.spot.slug}`}
      className={
        dark
          ? "group block rounded-2xl border border-white/15 bg-white/[0.06] p-4 text-white backdrop-blur transition hover:bg-white/[0.1]"
          : "group block rounded-2xl border bg-card p-4 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md"
      }
      data-testid="event-card"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className={dark ? "text-xs text-white/60" : "text-xs text-muted-foreground"}>
            {event.spot.region_group} · {event.spot.country}
          </div>
          <div className="truncate font-semibold">{event.spot.name}</div>
        </div>
        <span className="h-10 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: colorForScore(event.peak_score) }} />
      </div>
      <div className="mt-3 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-display text-2xl font-semibold whitespace-nowrap">
          {heightRange(event.peak_breaking_height_min_ft, event.peak_breaking_height_max_ft, units)}
        </span>
        <QualityBadge label={event.peak_label} score={event.peak_score} />
      </div>
      <div className={dark ? "mt-2 space-y-1 text-xs text-white/75" : "mt-2 space-y-1 text-xs text-muted-foreground"}>
        <div className="flex items-center gap-1.5">
          <CalendarDays className="size-3.5" />
          {formatRange(event.start_time, event.end_time, tz)} · {leadLabel(event.lead_days)}
        </div>
        <div className="flex items-center gap-1.5">
          <Wind className="size-3.5" />
          {metres(event.peak_swell_height_m)} @ {seconds(event.peak_swell_period_s)} {compass(event.peak_swell_direction_deg)} ·{" "}
          {WIND_RELATION_LABEL[event.peak_wind_relation] ?? event.peak_wind_relation} wind
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <ConfidenceBadge label={event.confidence_label} />
        {event.is_demo && <DemoBadge />}
      </div>
    </Link>
  );
}
