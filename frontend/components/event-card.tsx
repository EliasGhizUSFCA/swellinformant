import { CalendarDays, Waves, Wind } from "lucide-react";
import Link from "next/link";

import { ConfidenceBadge } from "@/components/confidence";
import { DemoBadge } from "@/components/quality";
import { RatingHeadline } from "@/components/rating";
import { compass, formatRange, heightRange, kmh, leadLabel, metres, seconds, type Units } from "@/lib/format";
import { colorForScore, WIND_RELATION_LABEL } from "@/lib/quality";
import { ratingReason } from "@/lib/rating";
import { cn } from "@/lib/utils";
import type { SwellEvent } from "@/types/api";

/**
 * One upcoming swell event: rating at its peak, our breaking-wave estimate, the surf window,
 * the offshore swell and wind behind it, and its forecast confidence ("Not assessed yet"
 * until the API provides a class). ``featured`` gives the best event more room.
 */
export function EventCard({
  event,
  units = "ft",
  dark = false,
  featured = false,
}: {
  event: SwellEvent;
  units?: Units;
  dark?: boolean;
  featured?: boolean;
}) {
  const tz = event.spot.timezone;
  const reason = ratingReason({
    periodS: event.peak_swell_period_s,
    windRelation: event.peak_wind_relation,
    windSpeedKmh: event.peak_wind_speed_kmh,
  });
  const muted = dark ? "text-white/70" : "text-muted-foreground";
  return (
    <Link
      href={`/spots/${event.spot.slug}`}
      className={cn(
        "group block rounded-2xl border p-4 transition",
        dark ? "border-white/15 bg-white/[0.06] text-white backdrop-blur hover:bg-white/[0.1]" : "bg-card shadow-sm hover:-translate-y-0.5 hover:shadow-md",
        featured && !dark && "border-ocean/30 bg-gradient-to-br from-card to-secondary/60 sm:p-5",
      )}
      data-testid="event-card"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className={cn("text-xs", muted)}>
            {featured && <span className="font-semibold text-ocean">Best upcoming surf · </span>}
            {event.spot.region_group} · {event.spot.country}
          </div>
          <div className={cn("truncate font-semibold", featured && "font-display text-xl")}>{event.spot.name}</div>
        </div>
        <span className="h-10 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: colorForScore(event.peak_score) }} aria-hidden="true" />
      </div>
      <RatingHeadline label={event.peak_label} score={event.peak_score} reason={reason} size={featured ? "md" : "sm"} dark={dark} className="mt-2" />
      <div className="mt-2 flex flex-wrap items-baseline gap-x-2">
        <span className={cn("font-display font-semibold whitespace-nowrap", featured ? "text-3xl" : "text-2xl")}>
          {heightRange(event.peak_breaking_height_min_ft, event.peak_breaking_height_max_ft, units)}
        </span>
        <span className={cn("text-xs", muted)}>breaking at peak (our estimate)</span>
      </div>
      <div className={cn("mt-2 space-y-1 text-xs", muted)}>
        <div className="flex items-center gap-1.5">
          <CalendarDays className="size-3.5 shrink-0" />
          {formatRange(event.start_time, event.end_time, tz)} · {leadLabel(event.lead_days)}
        </div>
        <div className="flex items-center gap-1.5">
          <Waves className="size-3.5 shrink-0" />
          {metres(event.peak_swell_height_m)} @ {seconds(event.peak_swell_period_s)} {compass(event.peak_swell_direction_deg)} offshore swell
        </div>
        <div className="flex items-center gap-1.5">
          <Wind className="size-3.5 shrink-0" />
          {WIND_RELATION_LABEL[event.peak_wind_relation] ?? event.peak_wind_relation} wind
          {event.peak_wind_speed_kmh != null ? ` · ${kmh(event.peak_wind_speed_kmh)}` : ""}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <ConfidenceBadge value={event.forecast_confidence_class} />
        {event.is_demo && <DemoBadge />}
      </div>
    </Link>
  );
}
