"use client";

import { ChevronLeft, ChevronRight, Moon } from "lucide-react";
import { memo } from "react";

import { ConfidenceBadge } from "@/components/confidence";
import { InfoPopover } from "@/components/info-popover";
import { datumText, TIDE_UNAVAILABLE } from "@/components/forecast/tide-row";
import { useTimelineActions, useTimelineSelection } from "@/components/forecast/timeline-selection";
import { WhyThisForecast } from "@/components/forecast/why-this-forecast";
import { FAVOURABILITY_LABEL } from "@/components/forecast/wind-row";
import { arrowRotationDeg } from "@/lib/directions";
import { asQuality, QUALITY_COLORS, QUALITY_LABELS, QUALITY_RANK, WIND_RELATION_LABEL } from "@/lib/quality";
import { score10 } from "@/lib/rating";
import {
  directionText,
  drivingPartition,
  nearestStepIndex,
  nextStep,
  PARTITION_LABEL,
  partitionText,
  prevStep,
  selectedTimeTitle,
  stepLengthText,
  tideHeightText,
  type TimelineModel,
} from "@/lib/timeline";
import { convertHeightFt, formatHeightRange, formatWind, type HeightUnit, type WindUnit } from "@/lib/units";
import { cn } from "@/lib/utils";
import type { SpotTimeline, TimelineStep } from "@/types/api";

const STRENGTH_LABEL: Record<string, string> = { glassy: "Glassy", light: "Light", moderate: "Moderate", strong: "Strong" };

/** A small arrow pointing where something travels (FROM + 180), with the FROM text beside. */
export function ToArrow({ fromDeg, className }: { fromDeg: number; className?: string }) {
  return (
    <svg viewBox="-8 -8 16 16" className={cn("inline-block size-3.5 shrink-0", className)} aria-hidden="true" data-arrow-rotation={arrowRotationDeg(fromDeg)}>
      <path d="M0,-6.5L4.2,-0.8L1.1,-0.8L1.1,6.5L-1.1,6.5L-1.1,-0.8L-4.2,-0.8Z" fill="currentColor" transform={`rotate(${arrowRotationDeg(fromDeg)})`} />
    </svg>
  );
}

/** The quality class as a swatch plus its rank as rising bars (1 to 6 filled), so the class
 * is readable without telling the colours apart. */
export function QualityMarker({ label, className }: { label: string; className?: string }) {
  const q = asQuality(label);
  const rank = QUALITY_RANK[q];
  return (
    <span className={cn("inline-flex items-center gap-1", className)} aria-hidden="true" data-quality-marker={q}>
      <span className="size-3 rounded-[4px]" style={{ backgroundColor: QUALITY_COLORS[q] }} />
      <span className="flex items-end gap-[2px]">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <span key={i} className={cn("w-[3px] rounded-[1px]", i <= rank ? "bg-ink" : "bg-border")} style={{ height: 4 + i * 1.6 }} />
        ))}
      </span>
    </span>
  );
}

function Item({ label, children, className, testId }: { label: React.ReactNode; children: React.ReactNode; className?: string; testId?: string }) {
  return (
    <div className={cn("min-w-0", className)} data-testid={testId}>
      <dt className="text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">{label}</dt>
      <dd className="mt-0.5 text-sm text-ink">{children}</dd>
    </div>
  );
}

function SwellLines({ step, units }: { step: TimelineStep; units: HeightUnit }) {
  const drive = drivingPartition(step);
  const rest = step.partitions.filter((p) => p !== drive);
  if (!step.partitions.length) {
    return (
      <span className="text-muted-foreground">
        {step.offshore.hs_m != null ? `Total sea ${partitionText({ kind: "primary_swell", height_m: step.offshore.hs_m, period_s: step.offshore.tp_s, dir_from_deg: step.offshore.dir_from_deg, breaking_ft: null, drives_surf: false }, units)}` : "—"}
      </span>
    );
  }
  return (
    <ul className="space-y-0.5" data-testid="summary-partitions">
      {[...(drive ? [drive] : []), ...rest].map((p) => (
        <li key={p.kind} className={cn("flex flex-wrap items-center gap-x-1.5", p.drives_surf ? "font-semibold" : "text-muted-foreground")} data-partition={p.kind} data-drives={p.drives_surf ? "true" : undefined}>
          {p.dir_from_deg != null ? <ToArrow fromDeg={p.dir_from_deg} className={p.drives_surf ? "text-ink" : "text-muted-foreground"} /> : <span className="size-3.5" />}
          <span className="min-w-[7rem]">{PARTITION_LABEL[p.kind]}</span>
          <span className="tabular-nums">{partitionText(p, units)}</span>
          {p.drives_surf && <span className="rounded-full bg-[var(--chart-swell)] px-1.5 py-px text-[10px] font-semibold text-white">drives the surf</span>}
        </li>
      ))}
    </ul>
  );
}

/**
 * The selected time in words and numbers, above the charts: surf quality, our breaking-wave
 * estimate, the swell that drives it (with the other wave systems), wind, tide and forecast
 * confidence, with "Why this forecast?" from the scoring engine's own record. It re-renders
 * when the selection changes; the chart rows do not.
 */
export const SelectedTimeSummary = memo(function SelectedTimeSummary({
  model,
  timeline,
  units,
  windUnit,
  method,
}: {
  model: TimelineModel;
  timeline: SpotTimeline;
  units: HeightUnit;
  windUnit: WindUnit;
  method?: React.ReactNode;
}) {
  const selectedT = useTimelineSelection((s) => s.selectedT);
  const { select } = useTimelineActions();
  const i = selectedT == null ? -1 : nearestStepIndex(model.times, selectedT);
  const info = model.steps[i];
  if (!info) return null;
  const step = info.step;
  const q = asQuality(step.quality.label);
  const b = step.breaking;
  const w = step.wind;
  const tide = timeline.tide;
  const n = model.steps.length;
  const go = (j: number) => select(model.times[j] ?? null);
  return (
    <section className="rounded-2xl border bg-gradient-to-br from-secondary/60 via-card to-card p-3.5 sm:p-5" data-testid="selected-summary">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-display text-lg leading-tight font-semibold text-ink sm:text-xl" data-testid="summary-time">
            {selectedTimeTitle(info.t, model.tz)}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <span className="rounded-full border bg-card px-2 py-0.5" data-testid="summary-step-length">
              {stepLengthText(step.step_hours)}
            </span>
            {!step.is_daylight && (
              <span className="inline-flex items-center gap-1 rounded-full border bg-card px-2 py-0.5">
                <Moon className="size-3" aria-hidden="true" /> Night
              </span>
            )}
            {step.interpolated && <span className="rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-amber-900">Interpolated by the source</span>}
            {step.lead_hours != null && step.lead_hours >= 0 && <span>{Math.round(step.lead_hours)} h after the model run</span>}
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="Previous forecast step"
            onClick={() => go(prevStep(i))}
            disabled={i <= 0}
            className="rounded-lg border bg-card p-2 text-ink transition-colors hover:bg-muted disabled:opacity-40"
          >
            <ChevronLeft className="size-4" />
          </button>
          <button
            type="button"
            aria-label="Next forecast step"
            onClick={() => go(nextStep(n, i))}
            disabled={i >= n - 1}
            className="rounded-lg border bg-card p-2 text-ink transition-colors hover:bg-muted disabled:opacity-40"
          >
            <ChevronRight className="size-4" />
          </button>
        </div>
      </div>

      <div className="mt-3 grid gap-x-8 gap-y-4 lg:grid-cols-[minmax(14rem,0.8fr)_2.2fr]">
        <div className="grid min-w-0 grid-cols-2 gap-x-5 gap-y-3 lg:grid-cols-1">
          <div className="min-w-0" data-testid="summary-quality" data-label={q} data-score={step.quality.score}>
            <div className="text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">Surf quality</div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <QualityMarker label={q} />
              <span className="text-lg font-semibold text-ink sm:text-xl">{QUALITY_LABELS[q]}</span>
              <span className="text-lg font-semibold text-ink tabular-nums sm:text-xl">{score10(step.quality.score)}/10</span>
            </div>
            <div className="text-[11px] text-muted-foreground">our rating for this break</div>
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">
              Breaking waves <InfoPopover term="breakingHeight" />
            </div>
            <div className="flex flex-wrap items-baseline gap-x-2" data-testid="summary-breaking" data-typical={b.typical_ft} data-sets={b.sets_ft} data-central={b.central_ft}>
              <span className="font-display text-2xl font-semibold text-ink tabular-nums sm:text-3xl">{formatHeightRange(b.typical_ft, b.sets_ft, units)}</span>
              <span className="text-xs text-muted-foreground">typical–sets</span>
            </div>
            <div className="text-[11px] text-muted-foreground tabular-nums">
              our estimate · central {convertHeightFt(b.central_ft, units).toFixed(1)} {units}
              {b.p10_ft != null && b.p90_ft != null && <> · likely {formatHeightRange(b.p10_ft, b.p90_ft, units)} (unverified)</>}
            </div>
          </div>
        </div>

        <dl className="grid min-w-0 grid-cols-2 gap-x-5 gap-y-3 xl:grid-cols-[1.35fr_1fr_1fr]">
          <Item label="Swell · offshore model" className="col-span-2 xl:col-span-1 xl:row-span-2" testId="summary-swell">
            <SwellLines step={step} units={units} />
          </Item>
          <Item label="Wind" testId="summary-wind">
            {w && w.speed_kmh != null ? (
              <>
                <span className="inline-flex flex-wrap items-center gap-x-1.5 font-semibold" data-speed={w.speed_kmh}>
                  {w.dir_from_deg != null && <ToArrow fromDeg={w.dir_from_deg} />}
                  {formatWind(w.speed_kmh, windUnit)} from {directionText(w.dir_from_deg)}
                </span>
                <span className="block text-xs text-muted-foreground" data-testid="summary-wind-relation">
                  {WIND_RELATION_LABEL[w.relation] ?? w.relation}
                  {w.strength ? ` · ${STRENGTH_LABEL[w.strength] ?? w.strength}` : ""}
                  {w.favourability ? ` · ${FAVOURABILITY_LABEL[w.favourability] ?? w.favourability}` : ""}
                </span>
                <span className="block text-xs text-muted-foreground" data-testid="summary-gust">
                  {w.gust_kmh != null ? `gust ${formatWind(w.gust_kmh, windUnit)}` : "no gust data"}
                  {w.gust_below_mean ? " (below the mean wind in the source)" : ""}
                </span>
              </>
            ) : (
              <span className="text-muted-foreground">No wind data for this step</span>
            )}
          </Item>
          <Item label="Tide" testId="summary-tide">
            {step.tide ? (
              <>
                <span className="font-semibold tabular-nums">{tideHeightText(step.tide.height_m, units)}</span>
                {step.tide.rising != null && <span className="text-muted-foreground"> · {step.tide.rising ? "rising" : "falling"}</span>}
                <span className="block text-xs text-muted-foreground">
                  above {datumText(tide.datum)}
                  {tide.status === "demo" ? " · demo tide (synthetic)" : ""}
                </span>
              </>
            ) : (
              <span className="text-muted-foreground">{tide.status === "unavailable" ? TIDE_UNAVAILABLE : "No tide value at this step"}</span>
            )}
          </Item>
          <Item label="Confidence" className="col-span-2" testId="summary-confidence">
            <span className="flex flex-wrap items-center gap-1.5">
              <ConfidenceBadge value={step.quality.forecast_confidence_class} kind="forecast" data-testid="summary-forecast-confidence" />
              {step.quality.local_confidence_class !== step.quality.forecast_confidence_class && (
                <ConfidenceBadge value={step.quality.local_confidence_class} kind="local" data-testid="summary-local-confidence" />
              )}
              <InfoPopover term="forecastConfidence" />
            </span>
            {step.quality.forecast_confidence_reasons[0] && <span className="mt-1 block text-xs text-muted-foreground">{step.quality.forecast_confidence_reasons[0]}</span>}
          </Item>
        </dl>
      </div>
      <WhyThisForecast className="mt-3" step={step} method={method} />
    </section>
  );
});
