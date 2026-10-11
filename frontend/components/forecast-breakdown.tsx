import { Bot, Waves, Wind } from "lucide-react";

import { uncertaintyLabel } from "@/components/charts/forecast-charts";
import { ForecastConfidence, LocalConfidence } from "@/components/confidence";
import { InfoPopover } from "@/components/info-popover";
import { DemoBadge } from "@/components/quality";
import { RatingHeadline, WhyThisRating } from "@/components/rating";
import { Disclosure } from "@/components/ui/disclosure";
import { compass, formatDateTime, heightRange, kmh, metres, seconds, type Units } from "@/lib/format";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { ratingFactors, ratingReason } from "@/lib/rating";
import type { Conditions, ForecastPoint } from "@/types/api";

const METHOD_LABEL: Record<string, string> = {
  komar_gaughan_v1_uncalibrated: "Komar–Gaughan breaker height with an uncalibrated spot factor",
  calibrated_power_law: "Spot model calibrated against observations",
  linear_wave_v2: "Linear wave theory: shoaling, refraction and depth-limited breaking",
};

export function methodLabel(method?: string | null): string {
  if (!method) return "—";
  return METHOD_LABEL[method] ?? method.replaceAll("_", " ");
}

function Row({ k, v }: { k: React.ReactNode; v: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <dt className="text-muted-foreground">{k}</dt>
      <dd className="text-right font-medium tabular-nums">{v}</dd>
    </div>
  );
}

function swellText(h: number | null | undefined, t: number | null | undefined, d: number | null | undefined): string | null {
  if (h == null) return null;
  return `${metres(h)} @ ${seconds(t)}${d != null ? ` from ${compass(d)}` : ""}`;
}

/** Secondary offshore fields of one forecast hour (only those the model provides). */
function MoreOffshoreData({ p }: { p: ForecastPoint }) {
  const rows: Array<[string, string | null]> = [
    ["Secondary swell", swellText(p.secondary_swell_height_m, p.secondary_swell_period_s, p.secondary_swell_direction_deg)],
    ["Third swell", swellText(p.tertiary_swell_height_m, p.tertiary_swell_period_s, p.tertiary_swell_direction_deg)],
    ["Wind waves (local chop)", swellText(p.wind_wave_height_m, p.wind_wave_period_s, p.wind_wave_direction_deg)],
    ["Wind gusts", p.wind_gust_kmh != null ? kmh(p.wind_gust_kmh) : null],
    ["Sea level incl. tide", p.sea_level_m != null ? `${p.sea_level_m.toFixed(2)} m` : null],
    ["Wave power", p.wave_power_kw_m != null ? `${Math.round(p.wave_power_kw_m)} kW/m` : null],
    ["Air pressure", p.pressure_msl_hpa != null ? `${Math.round(p.pressure_msl_hpa)} hPa` : null],
  ];
  const shown = rows.filter((r): r is [string, string] => r[1] != null);
  if (shown.length === 0) return null;
  return (
    <Disclosure summary="More offshore data" className="mt-2" data-testid="more-offshore-data">
      <dl className="text-sm">
        {shown.map(([k, v]) => (
          <Row key={k} k={k} v={v} />
        ))}
      </dl>
      {p.sea_level_m == null && <p className="mt-1 text-[11px] text-muted-foreground">No tide data for this forecast point.</p>}
    </Disclosure>
  );
}

/**
 * One forecast hour: our prediction first (rating, breaking-wave estimate and its range,
 * forecast confidence), then the raw numerical-model data it is based on. They are different
 * physical quantities, so their difference is never shown as an error.
 *
 * Optional, additive inputs: ``components`` (the API's score components, for "Why this
 * rating?"), ``point`` (the full forecast point, for secondary swells and sea level) and
 * ``spotRange`` (the spot's working range in feet, for the one-line reason).
 */
export function ForecastBreakdown({
  c,
  tz,
  units,
  components,
  point,
  spotRange,
}: {
  c: Conditions;
  tz: string;
  units: Units;
  components?: Record<string, unknown> | null;
  point?: ForecastPoint | null;
  spotRange?: { minFt: number; maxFt: number } | null;
}) {
  const dir = c.swell_direction_deg;
  const factorInput = {
    periodS: c.swell_period_s,
    windRelation: c.wind_relation,
    windSpeedKmh: c.wind_speed_kmh,
    components,
    explanation: c.explanation,
    breakingMinFt: c.breaking_height_min_ft,
    breakingMaxFt: c.breaking_height_max_ft,
    spotMinFt: spotRange?.minFt,
    spotMaxFt: spotRange?.maxFt,
  };
  const reason = ratingReason(factorInput);
  return (
    <div className="space-y-3" data-testid="forecast-breakdown">
      <div className="text-xs text-muted-foreground">
        {formatDateTime(c.valid_time, tz)} local · {c.is_daylight ? "daylight" : "night (not surfable)"}
      </div>

      <section className="rounded-xl border border-ocean/30 bg-ocean/5 p-3" data-testid="our-prediction">
        <div className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-ocean uppercase">
          <Bot className="size-3.5" /> Our surf prediction
        </div>
        <RatingHeadline label={c.label} score={c.score} reason={reason} className="mt-1.5" />
        <div className="mt-2 flex flex-wrap items-baseline gap-x-2" data-testid="breaking-height">
          <span className="font-display text-2xl font-semibold">{heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)}</span>
          <span className="text-xs text-muted-foreground">
            est. breaking faces, typical–sets <InfoPopover term="breakingHeight" />
          </span>
        </div>
        {c.breaking_p10_ft != null && c.breaking_p90_ft != null && (
          <div className="text-xs text-muted-foreground" data-testid="uncertainty-range">
            80% range {heightRange(c.breaking_p10_ft, c.breaking_p90_ft, units)} · {uncertaintyLabel(c.uncertainty_kind)} <InfoPopover term="range80" />
          </div>
        )}
        <div className="mt-2.5 space-y-1.5">
          <ForecastConfidence value={c.forecast_confidence_class} reasons={c.confidence_reasons} leadHours={c.lead_hours} legacyIndex={c.confidence} />
          <LocalConfidence value={c.local_confidence_class} forecastValue={c.forecast_confidence_class} reasons={c.confidence_reasons} />
        </div>
        <WhyThisRating
          className="mt-2"
          score={c.score}
          explanation={c.explanation}
          components={components}
          factors={ratingFactors(factorInput)}
          method={
            <>
              Method: {methodLabel(c.method)}
              {c.algorithm_version ? ` (v${c.algorithm_version})` : ""}
              {c.method !== "calibrated_power_law" && " — not yet calibrated against surf observations at this break"}
              {c.ml_model_version ? ` · offshore input corrected by ${c.ml_model_version}` : ""}
            </>
          }
        />
      </section>

      <section className="rounded-xl border p-3" data-testid="raw-model">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            <Waves className="size-3.5" /> Offshore model data
            <InfoPopover title="Offshore vs. breaking height">
              <p>
                Offshore height is the open-water sea state from the numerical model; breaking height is our estimate of wave faces at this break.
                They are different quantities, so their difference is not a forecast error.
              </p>
            </InfoPopover>
          </div>
          {c.is_demo && <DemoBadge />}
        </div>
        <div className="mt-0.5 text-[11px] text-muted-foreground">
          {c.source_name ?? "Forecast source"}
          {c.run_issued_at ? ` · model run ${formatDateTime(c.run_issued_at, "UTC")} UTC` : ""}
        </div>
        <dl className="mt-2 text-sm">
          <Row
            k={
              <span className="inline-flex items-center gap-1">
                Significant wave height (offshore) <InfoPopover term="offshoreHeight" />
              </span>
            }
            v={metres(c.sig_wave_height_m)}
          />
          <Row k="Main component (drives the surf)" v={`${metres(c.swell_height_m)} @ ${seconds(c.swell_period_s)}`} />
          <Row k="Main component direction" v={dir != null ? `${compass(dir)} (${Math.round(dir)}°)` : "—"} />
          <Row
            k="Wind"
            v={
              <span className="inline-flex items-center gap-1">
                <Wind className="size-3.5" />
                {kmh(c.wind_speed_kmh)} from {compass(c.wind_direction_deg)} · {WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation}
              </span>
            }
          />
        </dl>
        {point && <MoreOffshoreData p={point} />}
      </section>
    </div>
  );
}
