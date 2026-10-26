"use client";

import { Waves, Wind } from "lucide-react";

import { ForecastConfidence } from "@/components/confidence";
import { useNowMs } from "@/components/forecast-provenance";
import { InfoPopover } from "@/components/info-popover";
import { RatingHeadline } from "@/components/rating";
import { WindDisputeNote } from "@/components/wind-dispute";
import { forecastReasonsOf, rangeWords, validInText } from "@/lib/confidence";
import { compass, formatSpotDateTime, formatSpotTime, heightRange, seconds, type Units } from "@/lib/format";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { ratingReason } from "@/lib/rating";
import { offshoreHeightText } from "@/lib/timeline";
import { formatWind, useWindUnit } from "@/lib/units";
import type { Conditions } from "@/types/api";

const WEEK_MS = 6.5 * 24 * 3_600_000;

/** "Thu 6:00 PM SAST" within the coming week, the full date further out (spot's zone). */
function spotWhen(iso: string, tz: string, nowMs: number): string {
  return Math.abs(Date.parse(iso) - nowMs) < WEEK_MS ? formatSpotTime(iso, tz) : formatSpotDateTime(iso, tz, { zone: true });
}

/**
 * Compact summary of one forecast step: rating, our breaking-wave estimate and its likely
 * range, the offshore swell and wind behind it, and forecast confidence. It is model output,
 * so it is titled "Forecast" with the step's valid time in the spot's zone and how far away
 * it is ("in 2 h"), never "Now". `title` names what the step is (e.g. "Best surf window,
 * next 7 days"); the legacy title "Now" is not shown. `spotRange` (the spot's working range in
 * feet) sharpens the one-line reason; without it the API's explanation is used.
 */
export function ConditionsSummary({
  c,
  tz,
  units,
  title,
  spotRange,
  nowMs,
  dark = false,
}: {
  c: Conditions;
  tz: string;
  units: Units;
  title?: string;
  spotRange?: { minFt: number; maxFt: number } | null;
  /** The time "in 2 h" is counted from (defaults to the browser clock). */
  nowMs?: number;
  /** On a dark surface (the map's spot panel): the quality marker uses the dark ramp. */
  dark?: boolean;
}) {
  const clock = useNowMs();
  const [windUnit] = useWindUnit();
  const now = nowMs ?? clock;
  const reason = ratingReason({
    periodS: c.swell_period_s,
    windRelation: c.wind_relation,
    windSpeedKmh: c.wind_speed_kmh,
    windStrength: c.wind_strength,
    windFavourability: c.wind_favourability,
    explanation: c.explanation,
    breakingMinFt: c.breaking_height_min_ft,
    breakingMaxFt: c.breaking_height_max_ft,
    breakingMidFt: c.breaking_height_mid_ft,
    spotMinFt: spotRange?.minFt,
    spotMaxFt: spotRange?.maxFt,
  });
  const label = title && title.trim().toLowerCase() !== "now" ? title : "Forecast";
  const away = validInText(c.valid_time, now);
  const range = rangeWords(c.uncertainty_kind);
  return (
    <div className="rounded-xl border bg-card p-4" data-testid="conditions-summary">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5" data-testid="conditions-title">
        <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{label}</div>
        <div className="text-xs text-muted-foreground" data-testid="conditions-time">
          <span className="font-medium text-foreground/80">{spotWhen(c.valid_time, tz, now)}</span>
          {away ? ` · ${away}` : ""}
          {c.is_daylight ? "" : " · night"}
        </div>
      </div>
      <RatingHeadline label={c.label} score={c.score} reason={reason} size="sm" dark={dark} className="mt-2" />
      <div className="mt-2 flex flex-wrap items-baseline gap-x-2">
        <span className="font-display text-2xl font-semibold">{heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)}</span>
        <span className="text-xs text-muted-foreground">breaking (est.)</span>
      </div>
      {c.breaking_p10_ft != null && c.breaking_p90_ft != null && (
        <div className="flex items-center gap-1 text-xs text-muted-foreground" data-testid="likely-range">
          {range.name} {heightRange(c.breaking_p10_ft, c.breaking_p90_ft, units)} ({range.note}) <InfoPopover term="likelyRange" />
        </div>
      )}
      <div className="mt-2 space-y-0.5 text-sm text-muted-foreground">
        {/* A swell is named by where it comes from ("SW swell"); the wind says "from" below. */}
        <div className="flex items-center gap-1.5" title={`Offshore model swell${c.swell_direction_deg != null ? ` from ${compass(c.swell_direction_deg)}` : ""}, not the surf height`}>
          <Waves className="size-3.5 shrink-0" aria-hidden="true" />
          {offshoreHeightText(c.swell_height_m, units)} @ {seconds(c.swell_period_s)} {c.swell_direction_deg != null ? `${compass(c.swell_direction_deg)} ` : ""}swell
        </div>
        <div className="flex items-center gap-1.5">
          <Wind className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="sr-only">Wind: </span>
          {formatWind(c.wind_speed_kmh, windUnit)}
          {c.wind_direction_deg != null ? ` from ${compass(c.wind_direction_deg)}` : ""} · {WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation}
        </div>
      </div>
      <WindDisputeNote note={c.wind_dispute_note} reasons={c.confidence_reasons} className="mt-1.5" />
      <ForecastConfidence
        className="mt-2"
        value={c.forecast_confidence_class}
        reasons={forecastReasonsOf(c)}
        notes={c.confidence_reasons}
        validTime={c.valid_time}
        runIssuedAt={c.run_issued_at}
        blend={c.source_code === "blend"}
        nowMs={now}
        showReason={false}
        legacyIndex={c.confidence}
      />
    </div>
  );
}
