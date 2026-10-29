"use client";

import { ExternalLink } from "lucide-react";

import { ConfidenceBadge } from "@/components/confidence";
import { useNowMs } from "@/components/forecast-provenance";
import { ProviderCredit } from "@/components/forecast/provider-credit";
import { forecastConfidenceOf, hourOutputs } from "@/lib/forecast-fields";
import { compass, relativeTime, seconds, titleCase } from "@/lib/format";
import { cycleLabel } from "@/lib/map-time";
import { componentFetchedText, componentRuns } from "@/lib/model-runs";
import { runTimeEstimated } from "@/lib/provider";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { num } from "@/lib/rating";
import { offshoreHeightText, tideHeightText } from "@/lib/timeline";
import { formatHeightRange, formatWind, type HeightUnit, type WindUnit } from "@/lib/units";
import { cn } from "@/lib/utils";
import { safeExternalUrl, SOURCE_TYPE_LABEL } from "@/lib/confidence";
import type { Conditions, ForecastPoint, SpotDetail, SpotForecast, TimelineStep, TideBlock } from "@/types/api";

/** Run-time flags of the published version (GET /api/forecasts/version, contract C4), when the
 * page has them: Windy runs carry an estimated time (Windy publishes no model run time). */
export interface VersionRunFlags {
  wind_run_estimated?: boolean | null;
  wave_run_estimated?: boolean | null;
}

function Section({ title, children, testId }: { title: string; children: React.ReactNode; testId: string }) {
  return (
    <section className="border-t pt-2" data-testid={testId}>
      <h4 className="mb-1 font-semibold text-ink">{title}</h4>
      {children}
    </section>
  );
}

function Row({ k, v }: { k: string; v: string | null }) {
  if (v == null) return null;
  return (
    <div className="flex items-baseline justify-between gap-3 py-px">
      <dt>{k}</dt>
      <dd className="text-right font-medium text-ink tabular-nums">{v}</dd>
    </div>
  );
}

function swellText(h: number | null | undefined, t: number | null | undefined, d: number | null | undefined, units: HeightUnit): string | null {
  if (h == null) return null;
  return `${offshoreHeightText(h, units)} @ ${seconds(t)}${d != null ? ` from ${compass(d)}` : ""}`;
}

const FIT_WORD = (v: number) => (v >= 0.75 ? "favorable" : v >= 0.4 ? "workable" : "unfavorable");

/**
 * Every variable of the hour that the API sent (nothing filled in): our breaking estimate, the
 * offshore sea, each swell system and the wind sea, the wind and gusts, the tide at that step
 * and the air pressure. Directions are where things come FROM.
 */
export function HourVariables({
  c,
  point,
  step,
  units,
  windUnit,
}: {
  c: Conditions;
  point: ForecastPoint | null;
  step: TimelineStep | null;
  units: HeightUnit;
  windUnit: WindUnit;
}) {
  // The hour's offshore-wind class ("Moderate offshore") when the scoring sent one, as the
  // summary above shows it; else the relation alone ("Offshore").
  const offshoreClass = hourOutputs(point ?? step?.quality).offshoreClass;
  const relation = offshoreClass ? offshoreClass.charAt(0).toUpperCase() + offshoreClass.slice(1) : (WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation);
  return (
    <Section title="Every variable at this hour" testId="rating-variables">
      <dl>
        <Row k="Breaking faces (our estimate)" v={formatHeightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)} />
        <Row k="Likely range (unverified)" v={c.breaking_p10_ft != null && c.breaking_p90_ft != null ? formatHeightRange(c.breaking_p10_ft, c.breaking_p90_ft, units) : null} />
        <Row
          k="Offshore sea (model Hs)"
          v={point ? swellText(point.sig_wave_height_m, point.wave_period_s, point.wave_direction_deg, units) : c.sig_wave_height_m != null ? offshoreHeightText(c.sig_wave_height_m, units) : null}
        />
        <Row k="Primary swell" v={point ? swellText(point.primary_swell_height_m, point.primary_swell_period_s, point.primary_swell_direction_deg, units) : null} />
        <Row k="Secondary swell" v={point ? swellText(point.secondary_swell_height_m, point.secondary_swell_period_s, point.secondary_swell_direction_deg, units) : null} />
        <Row k="Third swell" v={point ? swellText(point.tertiary_swell_height_m, point.tertiary_swell_period_s, point.tertiary_swell_direction_deg, units) : null} />
        <Row k="Wind waves" v={point ? swellText(point.wind_wave_height_m, point.wind_wave_period_s, point.wind_wave_direction_deg, units) : null} />
        <Row
          k="Wind"
          v={c.wind_speed_kmh != null ? `${formatWind(c.wind_speed_kmh, windUnit)}${c.wind_direction_deg != null ? ` from ${compass(c.wind_direction_deg)}` : ""} · ${relation}` : null}
        />
        <Row k="Gusts" v={point?.wind_gust_kmh != null ? formatWind(point.wind_gust_kmh, windUnit) : null} />
        <Row k="Tide" v={step?.tide ? `${tideHeightText(step.tide.height_m, units)}${step.tide.rising != null ? (step.tide.rising ? ", rising" : ", falling") : ""}` : null} />
        <Row k="Air pressure" v={point?.pressure_msl_hpa != null ? `${Math.round(point.pressure_msl_hpa)} hPa` : null} />
      </dl>
    </Section>
  );
}

/**
 * The spot's tide preference and whether the tide counted in this rating: its fit at this hour
 * when the score used it (a spot without an established preference is never scored on tide).
 */
export function TidePreference({ spot, tide, components }: { spot: SpotDetail; tide: TideBlock | null | undefined; components: Record<string, unknown> | null | undefined }) {
  // A tide weighted 0 (SCORING_WEIGHTS_JSON) keeps its fit in the components but is out of
  // the score: the stored weights say so.
  const weights = components?.weights_used;
  const weight = weights && typeof weights === "object" ? num((weights as Record<string, unknown>).tide) : null;
  const fit = weight === 0 ? null : num(components?.tide);
  const text = tide?.preference.text ?? (spot.tide_preference ? titleCase(spot.tide_preference) : "Not established");
  return (
    <Section title="Tide" testId="rating-tide">
      <p>
        Preferred tide here: <span className="font-medium text-ink">{text}</span>
      </p>
      <p data-testid="rating-tide-fit">
        {fit != null
          ? `Tide is part of this rating: ${FIT_WORD(fit)} at this hour (fit ${Math.max(0, Math.min(1, fit)).toFixed(2)}).`
          : "Tide is not part of this rating (no established, researched preference for this break)."}
      </p>
    </Section>
  );
}

/**
 * Where the forecast behind the rating comes from: the provider, the atmosphere (wind) and wave
 * models with their own run times (they can differ: a component refreshed alone keeps the other
 * one's original run), an estimated run time marked as such, when the data was fetched, and the
 * provider's credit.
 */
export function ModelInfo({ fc, c, version }: { fc: SpotForecast | null | undefined; c: Conditions; version?: VersionRunFlags | null }) {
  const source = fc?.source;
  const run = fc?.run;
  const code = source?.code ?? c.source_code ?? null;
  const windEstimated = runTimeEstimated(code, version?.wind_run_estimated);
  const waveEstimated = runTimeEstimated(code, version?.wave_run_estimated);
  const atmosRun = run?.atmosphere_model_run_at ?? null;
  const waveRun = run?.wave_model_run_at ?? null;
  const fallbackRun = !atmosRun && !waveRun ? (run?.issued_at ?? c.run_issued_at ?? null) : null;
  const runText = (iso: string | null, estimated: boolean) => (iso ? `run ${cycleLabel(iso)}${estimated ? " (estimated)" : ""}` : "run time not reported");
  // A component refreshed alone was fetched at another time than the carried one. The ages
  // follow a ticking clock: on a page left open they keep up with the header's.
  const now = new Date(useNowMs());
  const parts = componentRuns(run?.members);
  const fetchedText = (parts ? componentFetchedText(parts, now) : null) ?? (run?.completed_at ? relativeTime(run.completed_at, now) : null);
  return (
    <Section title="Forecast models" testId="rating-models">
      <dl>
        <Row k="Provider" v={source?.name ?? c.source_name ?? null} />
        {source?.atmosphere_model && <Row k={`Wind: ${source.atmosphere_model}`} v={runText(atmosRun ?? fallbackRun, windEstimated)} />}
        {source?.wave_model && <Row k={`Waves: ${source.wave_model}`} v={runText(waveRun ?? fallbackRun, waveEstimated)} />}
        <Row k="Fetched" v={fetchedText} />
      </dl>
      {(windEstimated || waveEstimated) && (
        <p className="mt-0.5" data-testid="rating-models-estimated">
          Estimated: this provider publishes no model run time, so it is estimated from the newest model cycle that was due.
        </p>
      )}
      <ProviderCredit sourceCode={code} links={source?.attribution_links} className="mt-1" />
    </Section>
  );
}

/**
 * How the rating is made and how well the break is researched, kept apart from the forecast
 * confidence: the scoring method and version, the forecast confidence class of the hour (a
 * heuristic), the spot's research confidence with its first sources and the methodology.
 */
export function ResearchAndMethod({ spot, c, method }: { spot: SpotDetail; c: Conditions; method?: React.ReactNode }) {
  const research = spot.research;
  const sources = research?.sources ?? [];
  const fcConf = forecastConfidenceOf(c);
  return (
    <Section title="Method and research" testId="rating-research">
      <div className="space-y-1.5">
        {method && <p>{method}</p>}
        <p>
          A deterministic score from this break&apos;s researched ideal conditions (swell direction, period and size, wind relative to the
          coast, tide where established). Weights and cut-offs are documented heuristics, not calibrated against observed surf.
        </p>
        <div className="flex flex-wrap items-center gap-1.5">
          <ConfidenceBadge value={fcConf?.level ?? c.forecast_confidence_class} kind="forecast" data-testid="rating-forecast-confidence" />
          <span>a heuristic class (lead time, freshness, model agreement), not a probability</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <ConfidenceBadge value={research?.research_class ?? spot.research_class} kind="research" data-testid="rating-research-confidence" />
          <span>how well this break is documented (separate from the forecast)</span>
        </div>
        {sources.length > 0 && (
          <ul className="space-y-0.5" data-testid="rating-research-sources">
            {sources.slice(0, 3).map((s, i) => {
              const href = safeExternalUrl(s.url);
              return (
                <li key={`${i}-${s.title}`} className="min-w-0">
                  {href ? (
                    <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-start gap-1 font-medium text-ocean hover:underline">
                      {s.title}
                      <ExternalLink className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
                      <span className="sr-only">(opens in a new tab)</span>
                    </a>
                  ) : (
                    <span className="font-medium text-ink">{s.title}</span>
                  )}
                  <span> · {[s.publisher, SOURCE_TYPE_LABEL[s.source_type] ?? s.source_type].filter(Boolean).join(" · ")}</span>
                </li>
              );
            })}
          </ul>
        )}
        <p>
          {sources.length > 3 ? `${sources.length - 3} more sources, the gaps and each condition's evidence are in ` : "The gaps and each condition's evidence are in "}
          <a href="#research" className="font-medium text-ocean hover:underline">
            About this break
          </a>
          {research?.methodology_version ? ` (research methodology ${research.methodology_version}).` : "."}
        </p>
      </div>
    </Section>
  );
}

/** The spot page's extra sections of "Why this rating?", in reading order. */
export function RatingDetails({
  spot,
  c,
  point,
  step,
  fc,
  tide,
  units,
  windUnit,
  method,
  version,
  className,
}: {
  spot: SpotDetail;
  c: Conditions;
  point: ForecastPoint | null;
  step: TimelineStep | null;
  fc: SpotForecast | null | undefined;
  tide: TideBlock | null | undefined;
  units: HeightUnit;
  windUnit: WindUnit;
  method?: React.ReactNode;
  version?: VersionRunFlags | null;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2", className)} data-testid="rating-details">
      <HourVariables c={c} point={point} step={step} units={units} windUnit={windUnit} />
      <TidePreference spot={spot} tide={tide} components={point?.components ?? null} />
      <ModelInfo fc={fc} c={c} version={version} />
      <ResearchAndMethod spot={spot} c={c} method={method} />
    </div>
  );
}
