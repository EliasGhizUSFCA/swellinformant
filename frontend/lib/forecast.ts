import type { Conditions, ForecastPoint, SpotForecast } from "@/types/api";

/** One hour of a spot forecast in the shape the conditions/breakdown components use. */
export function pointToConditions(p: ForecastPoint, fc: SpotForecast): Conditions {
  return {
    valid_time: p.valid_time,
    score: p.score,
    label: p.label,
    breaking_height_min_ft: p.breaking_height_min_ft,
    breaking_height_max_ft: p.breaking_height_max_ft,
    breaking_height_mid_ft: p.breaking_height_mid_ft ?? null,
    breaking_p10_ft: p.breaking_p10_ft ?? null,
    breaking_p90_ft: p.breaking_p90_ft ?? null,
    uncertainty_kind: p.uncertainty_kind ?? null,
    ml_model_version: p.ml_model_version ?? null,
    swell_height_m: p.primary_swell_height_m ?? p.sig_wave_height_m ?? null,
    swell_period_s: p.primary_swell_period_s ?? p.wave_period_s ?? null,
    swell_direction_deg: p.primary_swell_direction_deg ?? p.wave_direction_deg ?? null,
    wind_speed_kmh: p.wind_speed_kmh ?? null,
    wind_direction_deg: p.wind_direction_deg ?? null,
    wind_relation: p.wind_relation,
    confidence: p.confidence,
    confidence_label: p.confidence_label,
    is_daylight: p.is_daylight,
    sig_wave_height_m: p.sig_wave_height_m ?? null,
    source_code: fc.source?.code ?? null,
    source_name: fc.source?.name ?? null,
    is_demo: fc.source?.is_demo ?? false,
    run_issued_at: fc.run?.issued_at ?? null,
    method: p.breaking_height_method ?? null,
    algorithm_version: p.algorithm_version ?? null,
    explanation: p.explanation,
    forecast_confidence_class: p.forecast_confidence_class ?? null,
    local_confidence_class: p.local_confidence_class ?? null,
    confidence_reasons: p.confidence_reasons ?? [],
    lead_hours: p.lead_hours ?? null,
    local_confidence_reason: p.local_confidence_reason ?? null,
    forecast_confidence_reasons: p.forecast_confidence_reasons ?? [],
    wind_dispute_note: p.wind_dispute_note ?? null,
    wind_strength: p.wind_strength ?? null,
    wind_favourability: p.wind_favourability ?? null,
    gust_kind: p.gust_kind ?? null,
  };
}

/** Index of the point nearest to ``iso`` (or the first daylight point from now). */
export function nearestPointIndex(points: ForecastPoint[], iso: string | null): number {
  if (!points.length) return -1;
  const target = iso ? new Date(iso).getTime() : Date.now();
  let best = 0;
  let gap = Infinity;
  points.forEach((p, i) => {
    const d = Math.abs(new Date(p.valid_time).getTime() - target);
    if (d < gap) {
      gap = d;
      best = i;
    }
  });
  return best;
}
