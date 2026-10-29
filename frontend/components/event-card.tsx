"use client";

import { CalendarDays, Waves, Wind } from "lucide-react";
import Link from "next/link";

import { ConfidenceBadge } from "@/components/confidence";
import { DemoBadge } from "@/components/quality";
import { RatingHeadline } from "@/components/rating";
import { StaleBadge, StaleLine } from "@/components/stale-banner";
import { WindDisputeNote } from "@/components/wind-dispute";
import { currentFirst, freshnessOf, outOfDate } from "@/lib/forecast-fields";
import { compass, formatRange, heightRange, leadLabel, seconds, type Units } from "@/lib/format";
import { colorForScore, WIND_RELATION_LABEL } from "@/lib/quality";
import { ratingReason } from "@/lib/rating";
import { offshoreHeightText } from "@/lib/timeline";
import { formatWind, useWindUnit } from "@/lib/units";
import { cn } from "@/lib/utils";
import type { SwellEvent } from "@/types/api";

/**
 * One upcoming swell event: rating at its peak, our breaking-wave estimate, the surf window,
 * the offshore swell and wind behind it, and its forecast confidence ("Not assessed yet"
 * until the API provides a class; never combined with research confidence, spec 20). An
 * out-of-date forecast is tagged "Out of date": a stale run (`stale`) or a spot the published
 * version marks stale (`freshness`, e.g. its refresh failed), which also says when the spot
 * was last updated (spec 14).
 * ``featured`` gives the best event more room.
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
  const [windUnit] = useWindUnit();
  const reason = ratingReason({
    periodS: event.peak_swell_period_s,
    windRelation: event.peak_wind_relation,
    windSpeedKmh: event.peak_wind_speed_kmh,
    windStrength: event.wind_strength,
    windFavourability: event.wind_favourability,
  });
  const muted = dark ? "text-white/70" : "text-muted-foreground";
  const fresh = freshnessOf(event);
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
            {featured && <span className={cn("font-semibold", dark ? "text-seafoam" : "text-ocean")}>Best upcoming surf · </span>}
            {event.spot.country}
          </div>
          <div className={cn("truncate font-semibold", featured && "font-display text-xl")}>{event.spot.name}</div>
        </div>
        <span className="h-10 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: colorForScore(event.peak_score, dark ? "dark" : "light") }} aria-hidden="true" />
      </div>
      <RatingHeadline label={event.peak_label} score={event.peak_score} reason={reason} size={featured ? "md" : "sm"} dark={dark} className="mt-2" />
      <div className="mt-2 flex flex-wrap items-baseline gap-x-2">
        <span className={cn("font-display font-semibold whitespace-nowrap", featured ? "text-3xl" : "text-2xl")}>
          {heightRange(event.peak_breaking_height_min_ft, event.peak_breaking_height_max_ft, units)}
        </span>
        <span className={cn("text-xs", muted)}>breaking at peak (est.)</span>
      </div>
      <div className={cn("mt-2 space-y-1 text-xs", muted)}>
        <div className="flex items-center gap-1.5">
          <CalendarDays className="size-3.5 shrink-0" aria-hidden="true" />
          {formatRange(event.start_time, event.end_time, tz)} · {leadLabel(event.lead_days)}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="inline-flex items-center gap-1.5">
            <Waves className="size-3.5 shrink-0" aria-hidden="true" />
            {offshoreHeightText(event.peak_swell_height_m, units)} @ {seconds(event.peak_swell_period_s)} {compass(event.peak_swell_direction_deg)} swell
          </span>
          <span className="inline-flex items-center gap-1.5">
            <Wind className="size-3.5 shrink-0" aria-hidden="true" />
            {WIND_RELATION_LABEL[event.peak_wind_relation] ?? event.peak_wind_relation}
            <span className="sr-only"> wind</span>
            {event.peak_wind_speed_kmh != null ? ` ${formatWind(event.peak_wind_speed_kmh, windUnit)}` : ""}
          </span>
        </div>
      </div>
      <WindDisputeNote note={event.wind_dispute_note} reasons={event.confidence_reasons} className="mt-1.5" />
      {/* The rating above is from the spot's last update, not a current forecast. */}
      <StaleLine freshness={fresh} className={cn("mt-2", dark && "text-amber-200")} data-testid="event-card-stale" />
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {/* Forecast confidence only: the combined "confidence at the break" mixed research
            and forecast confidence (spec 20) and is no longer shown. */}
        <ConfidenceBadge value={event.forecast_confidence_class} />
        <StaleBadge
          stale={event.stale || fresh?.stale}
          dark={dark}
          title={event.stale ? undefined : "This spot's forecast was not updated with the latest refresh; it is not a current forecast."}
        />
        {event.is_demo && <DemoBadge />}
      </div>
    </Link>
  );
}

/** The dashboard's top swells: the current spots' first, then the out-of-date ones; the
 * first current one is featured ("Best upcoming surf"), never a stale spot's (spec 14). */
export function TopEventCards({ events, units = "ft" }: { events: readonly SwellEvent[]; units?: Units }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2" data-testid="top-events">
      {currentFirst(events).map((e, i) => {
        const featured = i === 0 && !outOfDate(e);
        return (
          <div key={e.id} className={featured ? "min-w-0 sm:col-span-2" : "min-w-0"}>
            <EventCard event={e} units={units} featured={featured} />
          </div>
        );
      })}
    </div>
  );
}
