import type { Conditions, ForecastPoint, SpotForecast } from "@/types/api";

interface BreakdownPartition {
  name?: unknown;
  height_m?: unknown;
  period_s?: unknown;
  direction_deg?: unknown;
  breaking_m?: unknown;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/**
 * Height, period and direction of the wave system that drives the surf at this hour: the
 * partition with the largest predicted breaking height in the stored nearshore breakdown
 * (``components.breaking.partitions``), exactly as the backend's
 * ``breaking.dominant_component`` picks it for ``Conditions``. That can be the wind sea or a
 * secondary swell, not necessarily the "primary" swell (the wave model ranks swells by
 * offshore height only). Without a breakdown, the bulk sea state (Hs, period, direction).
 */
export function drivingComponent(p: ForecastPoint): { height: number | null; period: number | null; direction: number | null } {
  const breaking = (p.components as Record<string, unknown> | undefined)?.breaking as { partitions?: BreakdownPartition[] } | undefined;
  const parts = (breaking?.partitions ?? []).filter((x) => (num(x.breaking_m) ?? 0) > 0);
  if (parts.length) {
    const best = parts.reduce((a, b) => ((num(b.breaking_m) ?? 0) > (num(a.breaking_m) ?? 0) ? b : a));
    return { height: num(best.height_m), period: num(best.period_s), direction: num(best.direction_deg) };
  }
  return { height: p.sig_wave_height_m ?? null, period: p.wave_period_s ?? null, direction: p.wave_direction_deg ?? null };
}

/** One hour of a spot forecast in the shape the conditions/breakdown components use. */
export function pointToConditions(p: ForecastPoint, fc: SpotForecast): Conditions {
  const drive = drivingComponent(p);
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
    swell_height_m: drive.height,
    swell_period_s: drive.period,
    swell_direction_deg: drive.direction,
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
