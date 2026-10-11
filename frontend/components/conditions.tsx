import { Waves, Wind } from "lucide-react";

import { ForecastConfidence } from "@/components/confidence";
import { RatingHeadline } from "@/components/rating";
import { compass, formatDateTime, heightRange, kmh, metres, seconds, type Units } from "@/lib/format";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { ratingReason } from "@/lib/rating";
import type { Conditions } from "@/types/api";

/**
 * Compact summary of one forecast hour: rating, our breaking-wave estimate, the offshore
 * swell and wind behind it, and forecast confidence. ``spotRange`` (the spot's working range
 * in feet) sharpens the one-line reason; without it the API's explanation is used.
 */
export function ConditionsSummary({
  c,
  tz,
  units,
  title,
  spotRange,
}: {
  c: Conditions;
  tz: string;
  units: Units;
  title: string;
  spotRange?: { minFt: number; maxFt: number } | null;
}) {
  const reason = ratingReason({
    periodS: c.swell_period_s,
    windRelation: c.wind_relation,
    windSpeedKmh: c.wind_speed_kmh,
    explanation: c.explanation,
    breakingMinFt: c.breaking_height_min_ft,
    breakingMaxFt: c.breaking_height_max_ft,
    spotMinFt: spotRange?.minFt,
    spotMaxFt: spotRange?.maxFt,
  });
  return (
    <div className="rounded-xl border bg-card p-4" data-testid="conditions-summary">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2">
        <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{title}</div>
        <div className="text-xs text-muted-foreground">
          {formatDateTime(c.valid_time, tz)} local{c.is_daylight ? "" : " · night (not surfable)"}
        </div>
      </div>
      <RatingHeadline label={c.label} score={c.score} reason={reason} size="sm" className="mt-2" />
      <div className="mt-2 flex flex-wrap items-baseline gap-x-2">
        <span className="font-display text-2xl font-semibold">{heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)}</span>
        <span className="text-xs text-muted-foreground">est. breaking faces</span>
      </div>
      {c.breaking_p10_ft != null && c.breaking_p90_ft != null && (
        <div className="text-xs text-muted-foreground">80% range {heightRange(c.breaking_p10_ft, c.breaking_p90_ft, units)}</div>
      )}
      <div className="mt-2 space-y-0.5 text-sm text-muted-foreground">
        <div className="flex items-center gap-1.5">
          <Waves className="size-3.5 shrink-0" />
          {metres(c.swell_height_m)} @ {seconds(c.swell_period_s)} {compass(c.swell_direction_deg)} offshore swell
        </div>
        <div className="flex items-center gap-1.5">
          <Wind className="size-3.5 shrink-0" />
          {kmh(c.wind_speed_kmh)} {compass(c.wind_direction_deg)} · {WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation}
        </div>
      </div>
      <ForecastConfidence
        className="mt-2"
        value={c.forecast_confidence_class}
        reasons={c.confidence_reasons}
        leadHours={c.lead_hours}
        legacyIndex={c.confidence}
      />
    </div>
  );
}
