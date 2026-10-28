"use client";

import { Bot, Waves, Wind } from "lucide-react";

import { uncertaintyLabel } from "@/components/charts/forecast-charts";
import { ForecastConfidence } from "@/components/confidence";
import { ForecastProvenance, useNowMs } from "@/components/forecast-provenance";
import { InfoPopover } from "@/components/info-popover";
import { DemoBadge } from "@/components/quality";
import { RatingHeadline, WhyThisRating } from "@/components/rating";
import { Disclosure } from "@/components/ui/disclosure";
import { WindDisputeNote } from "@/components/wind-dispute";
import { forecastReasonsOf, modelRunText, rangeWords, validInText } from "@/lib/confidence";
import { compass, formatSpotDateTime, heightRange, seconds, type Units } from "@/lib/format";
import { cycleLabel } from "@/lib/map-time";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { ratingFactors } from "@/lib/rating";
import { hourReason } from "@/lib/rating-sentence";
import { hourOutputs } from "@/lib/forecast-fields";
import { offshoreHeightText } from "@/lib/timeline";
import { formatWind, useWindUnit, type WindUnit } from "@/lib/units";
import type { Conditions, ForecastPoint, ForecastRunInfo, ForecastSourceInfo } from "@/types/api";

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

function swellText(h: number | null | undefined, t: number | null | undefined, d: number | null | undefined, units: Units): string | null {
  if (h == null) return null;
  return `${offshoreHeightText(h, units)} @ ${seconds(t)}${d != null ? ` from ${compass(d)}` : ""}`;
}

/** Secondary offshore fields of one forecast hour (only those the model provides). */
function MoreOffshoreData({ p, units, windUnit }: { p: ForecastPoint; units: Units; windUnit: WindUnit }) {
  const rows: Array<[string, string | null]> = [
    ["Secondary swell", swellText(p.secondary_swell_height_m, p.secondary_swell_period_s, p.secondary_swell_direction_deg, units)],
    ["Third swell", swellText(p.tertiary_swell_height_m, p.tertiary_swell_period_s, p.tertiary_swell_direction_deg, units)],
    ["Wind waves (local chop)", swellText(p.wind_wave_height_m, p.wind_wave_period_s, p.wind_wave_direction_deg, units)],
    ["Wind gusts", p.wind_gust_kmh != null ? formatWind(p.wind_gust_kmh, windUnit) : null],
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

/** "NOAA GFS 18Z + ECMWF 12Z"-style short label of the model runs behind a step. */
function runsLabel(c: Conditions, run: ForecastRunInfo | null | undefined, nowMs: number): string | null {
  const members = (run?.members ?? []).filter((m) => m.init_time);
  if (members.length > 1) return members.map((m) => `${m.name.split(" (")[0]} ${cycleLabel(m.init_time!)}`).join(" + ");
  if (members.length === 1) return `${c.source_name ?? members[0]!.name} ${cycleLabel(members[0]!.init_time!)}`;
  if (!c.run_issued_at) return c.source_name ?? null;
  const age = modelRunText(c.run_issued_at, nowMs);
  // A blend's issue time is only its oldest member's.
  return `${c.source_name ?? "Forecast source"} · ${c.source_code === "blend" ? `oldest ${age}` : age}`;
}

/**
 * One forecast step: our prediction first (rating, breaking-wave estimate and its likely
 * range, the swell and wind behind it, forecast confidence), then the raw
 * numerical-model data it is based on, folded under "Offshore model data". They are different
 * physical quantities, so their difference is never shown as an error.
 *
 * Optional, additive inputs: ``components`` (the API's score components, for "Why this
 * rating?"), ``point`` (the full forecast point, for secondary swells and sea level),
 * ``spotRange`` (the spot's working range in feet, for the one-line reason) and ``source`` /
 * ``run`` / ``generatedAt`` (the forecast's provenance: each model run with its init time and
 * when the data was fetched).
 */
export function ForecastBreakdown({
  c,
  tz,
  units,
  components,
  point,
  spotRange,
  source,
  run,
  generatedAt,
  nowMs,
}: {
  c: Conditions;
  tz: string;
  units: Units;
  components?: Record<string, unknown> | null;
  point?: ForecastPoint | null;
  spotRange?: { minFt: number; maxFt: number } | null;
  source?: ForecastSourceInfo | null;
  run?: ForecastRunInfo | null;
  generatedAt?: string | null;
  nowMs?: number;
}) {
  const clock = useNowMs();
  const [windUnit] = useWindUnit();
  const now = nowMs ?? clock;
  const dir = c.swell_direction_deg;
  const factorInput = {
    periodS: c.swell_period_s,
    windRelation: c.wind_relation,
    windSpeedKmh: c.wind_speed_kmh,
    windStrength: c.wind_strength,
    windFavourability: c.wind_favourability,
    components,
    explanation: c.explanation,
    breakingMinFt: c.breaking_height_min_ft,
    breakingMaxFt: c.breaking_height_max_ft,
    breakingMidFt: c.breaking_height_mid_ft,
    spotMinFt: spotRange?.minFt,
    spotMaxFt: spotRange?.maxFt,
  };
  // The hour's own favourable / unfavourable conditions (on its forecast point when given),
  // else the factor wording.
  const hour = point ?? c;
  const outputs = hourOutputs(hour);
  const reason = hourReason(hour, factorInput, units, windUnit);
  const away = validInText(c.valid_time, now);
  const range = rangeWords(c.uncertainty_kind);
  const runs = runsLabel(c, run, now);
  // Directions are where the wind and swell come FROM (the map and chart arrows point where they go).
  const wind = `${formatWind(c.wind_speed_kmh, windUnit)}${c.wind_direction_deg != null ? ` from ${compass(c.wind_direction_deg)}` : ""} · ${WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation}`;
  return (
    <div className="space-y-3" data-testid="forecast-breakdown">
      <div className="text-xs text-muted-foreground" data-testid="breakdown-time">
        Forecast for <span className="font-medium text-foreground/80">{formatSpotDateTime(c.valid_time, tz, { zone: true })}</span> (local time)
        {away ? ` · ${away}` : ""}
        {c.is_daylight ? "" : " · night"}
      </div>

      <section className="rounded-xl border border-ocean/30 bg-ocean/5 p-3" data-testid="our-prediction">
        <div className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-ocean uppercase">
          <Bot className="size-3.5" aria-hidden="true" /> Our surf prediction
        </div>
        <RatingHeadline label={c.label} score={c.score} reason={reason} className="mt-1.5" />
        <div className="mt-2 flex flex-wrap items-baseline gap-x-2" data-testid="breaking-height">
          <span className="font-display text-2xl font-semibold">{heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)}</span>
          <span className="text-xs text-muted-foreground">
            est. breaking faces, typical–sets <InfoPopover term="breakingHeight" />
          </span>
        </div>
        {c.breaking_p10_ft != null && c.breaking_p90_ft != null && (
          <div className="flex items-center gap-1 text-xs text-muted-foreground" data-testid="uncertainty-range">
            {range.name} {heightRange(c.breaking_p10_ft, c.breaking_p90_ft, units)} ({range.note})
            {/* The popover explains the range; screen readers get its intent and basis inline. */}
            <span className="sr-only">: meant as an 80% range, {uncertaintyLabel(c.uncertainty_kind)}</span>
            <InfoPopover term="likelyRange" />
          </div>
        )}
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground" data-testid="prediction-inputs">
          <span className="inline-flex items-center gap-1">
            <Waves className="size-3.5 shrink-0" aria-hidden="true" />
            {offshoreHeightText(c.swell_height_m, units)} @ {seconds(c.swell_period_s)} swell{dir != null ? ` from ${compass(dir)}` : ""}
          </span>
          <span className="inline-flex items-center gap-1">
            <Wind className="size-3.5 shrink-0" aria-hidden="true" />
            {wind}
          </span>
        </div>
        <WindDisputeNote note={c.wind_dispute_note} reasons={c.confidence_reasons} className="mt-1.5" />
        {/* The classes on the first screen; their reasons are in "Confidence details". */}
        <div className="mt-2.5 space-y-1.5">
          <ForecastConfidence
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
        <WhyThisRating
          className="mt-2"
          score={c.score}
          explanation={c.explanation}
          components={components}
          factors={ratingFactors(factorInput)}
          favourable={outputs.favourable}
          unfavourable={outputs.unfavourable}
          units={units}
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

      <Disclosure
        variant="card"
        className="border-border"
        data-testid="raw-model"
        summary={
          <span className="flex min-w-0 flex-col items-start gap-0.5">
            <span className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              <Waves className="size-3.5" aria-hidden="true" /> Offshore model data
            </span>
            {runs && (
              <span className="max-w-full text-left text-[11px] font-normal text-muted-foreground" data-testid="raw-model-runs">
                {runs}
              </span>
            )}
          </span>
        }
        hint={c.is_demo ? <DemoBadge /> : undefined}
        contentClassName="px-3 pb-3"
      >
        <p className="text-[11px] text-muted-foreground">
          Open-water values from the numerical model, not surf heights: their difference from our breaking estimate is not a forecast error.
        </p>
        <dl className="mt-2 text-sm">
          <Row
            k={
              <span className="inline-flex items-center gap-1">
                Significant wave height (offshore) <InfoPopover term="offshoreHeight" />
              </span>
            }
            v={offshoreHeightText(c.sig_wave_height_m, units)}
          />
          <Row k="Main component (drives the surf)" v={`${offshoreHeightText(c.swell_height_m, units)} @ ${seconds(c.swell_period_s)}`} />
          <Row k="Main component direction" v={dir != null ? `${compass(dir)} (${Math.round(dir)}°)` : "—"} />
          <Row
            k="Wind"
            v={
              <span className="inline-flex items-center gap-1">
                <Wind className="size-3.5" aria-hidden="true" />
                {formatWind(c.wind_speed_kmh, windUnit)} from {compass(c.wind_direction_deg)} · {WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation}
              </span>
            }
          />
        </dl>
        {point && <MoreOffshoreData p={point} units={units} windUnit={windUnit} />}
        <ForecastProvenance
          className="mt-2 border-t pt-2"
          source={source ?? (c.source_name ? { name: c.source_name, is_demo: c.is_demo ?? false } : null)}
          // A blend's own issue time is only its oldest member's: not shown as "the" run.
          run={run ?? (c.run_issued_at && c.source_code !== "blend" ? { issued_at: c.run_issued_at } : null)}
          members={run?.members}
          generatedAt={generatedAt}
          links={source?.attribution_links}
          sourceCode={source?.code ?? c.source_code}
        />
      </Disclosure>
    </div>
  );
}
