"use client";

import { memo, useMemo } from "react";

import { RowBackground, TimelineRow, WAVE_H, type RowProps } from "@/components/forecast/timeline-header";
import { niceTicks } from "@/lib/timeline";
import { convertHeightFt, type HeightUnit } from "@/lib/units";
import type { ObservationsBlock } from "@/types/api";

const TOP = 16;
const BOTTOM = 4;
const PLOT_H = WAVE_H - TOP - BOTTOM;
const M_PER_FT = 0.3048;
/** Share of a step's width the bar fills (the rest shows the range behind it). */
const BAR_SHARE = 0.68;

const axisText = (v: number, unit: HeightUnit) => (unit === "m" ? v.toFixed(v < 10 ? 1 : 0) : String(Math.round(v * 2) / 2));
const valueText = (v: number) => (v < 10 ? v.toFixed(1) : String(Math.round(v)));

/**
 * Our breaking-wave prediction per forecast step. The bar is the central estimate
 * (breaking.central_ft), centred on the step's valid time and as wide as the step (so 3-hour
 * steps are three times as wide as hourly ones); the pale band behind it, one step wide, is
 * the likely range (p10-p90 of an assumed error model until verified), clipped with a small
 * chevron where it reaches above the axis; the dark tick is the sets height. Each day's largest daylight value is written above its bar. Optionally the raw
 * offshore significant height of the wave model (offshore.hs_m) is drawn per step as a dashed
 * level, labelled as model data, with measured buoy heights (observations, instrument only)
 * as hollow diamonds: both are offshore sea-state heights, not breaking heights.
 */
export const WaveBars = memo(function WaveBars({
  model,
  scale,
  width,
  units,
  showOffshore,
  observations,
}: RowProps & { units: HeightUnit; showOffshore: boolean; observations: ObservationsBlock | null }) {
  const geo = useMemo(() => {
    const conv = (ft: number) => convertHeightFt(ft, units);
    const offshore = (m: number) => convertHeightFt(m / M_PER_FT, units);
    const measured = (observations?.items ?? []).filter((o) => o.kind === "instrument" && o.hs_m != null);
    let max = 0;
    for (const s of model.steps) {
      const b = s.step.breaking;
      // The axis fits the bars and set heights; a likely range reaching above it is clipped
      // and marked (its value is in the tooltip, the summary and the table).
      max = Math.max(max, conv(b.central_ft), conv(b.sets_ft));
      if (showOffshore && s.step.offshore.hs_m != null) max = Math.max(max, offshore(s.step.offshore.hs_m));
    }
    if (showOffshore) for (const o of measured) max = Math.max(max, offshore(o.hs_m!));
    const ticks = niceTicks(max * 1.06);
    const top = ticks[ticks.length - 1] ?? 1;
    const y = (v: number) => TOP + PLOT_H * (1 - Math.min(v, top) / top);
    // The day's largest central value (daylight steps), labelled above its bar.
    const peaks = new Set<number>();
    for (const d of model.days) {
      let best = -1;
      for (const s of model.steps) {
        if (s.t < d.start || s.t >= d.end || !s.step.is_daylight) continue;
        if (best < 0 || s.step.breaking.central_ft > model.steps[best]!.step.breaking.central_ft) best = s.i;
      }
      if (best >= 0) peaks.add(best);
    }
    return { conv, offshore, ticks, top, y, peaks, measured };
  }, [model, units, showOffshore, observations]);

  const { conv, offshore, ticks, top: axisTop, y, peaks, measured } = geo;
  const base = y(0);
  return (
    <TimelineRow
      testId="wave-bars"
      height={WAVE_H}
      width={width}
      label={`Predicted breaking wave height per step, in ${units}`}
      gutter={
        <div className="relative h-full w-full">
          <span className="absolute top-1 left-0 font-semibold text-ink">Surf</span>
          <span className="absolute top-[16px] left-0">{units}</span>
          {ticks.map((v) => (
            <span key={v} className="absolute right-0 -translate-y-1/2 tabular-nums" style={{ top: y(v) }} data-axis-tick={v}>
              {axisText(v, units)}
            </span>
          ))}
        </div>
      }
    >
      <svg width={width} height={WAVE_H} className="block" aria-hidden="true">
        <RowBackground model={model} scale={scale} height={WAVE_H} />
        {ticks.slice(1).map((v) => (
          <line key={v} x1={0} x2={width} y1={y(v)} y2={y(v)} stroke="var(--chart-grid)" strokeDasharray="2 3" />
        ))}
        {model.steps.map((s) => {
          const b = s.step.breaking;
          const x0 = scale(s.x0);
          const sw = scale(s.x1) - x0;
          const cx = scale(s.t);
          const bw = Math.max(2, sw * BAR_SHARE);
          const top = y(conv(b.central_ft));
          const r = Math.min(2.5, bw / 2, base - top);
          const half = bw / 2;
          const lo = b.p10_ft != null ? y(conv(b.p10_ft)) : null;
          const hi = b.p90_ft != null ? y(conv(b.p90_ft)) : null;
          return (
            <g
              key={s.i}
              className="tl-mark"
              data-step={s.i}
              data-t={s.t}
              data-central={b.central_ft}
              data-typical={b.typical_ft}
              data-sets={b.sets_ft}
              data-p10={b.p10_ft ?? undefined}
              data-p90={b.p90_ft ?? undefined}
              opacity={s.step.is_daylight ? 1 : 0.5}
            >
              {lo != null && hi != null && (
                <rect x={x0} y={hi} width={Math.max(1, sw)} height={Math.max(1, lo - hi)} fill="var(--chart-wave-band)" data-range="" />
              )}
              {b.p90_ft != null && conv(b.p90_ft) > axisTop && (
                <path d={`M${cx - 3},${TOP + 1}L${cx},${TOP - 3}L${cx + 3},${TOP + 1}`} fill="none" stroke="var(--chart-wave-range)" strokeWidth={1.2} data-range-clipped="" />
              )}
              <path
                className="tl-bar"
                d={`M${cx - half},${base}V${top + r}Q${cx - half},${top} ${cx - half + r},${top}H${cx + half - r}Q${cx + half},${top} ${cx + half},${top + r}V${base}Z`}
                fill="var(--chart-wave)"
              />
              <line
                x1={cx - half}
                x2={cx + half}
                y1={y(conv(b.sets_ft))}
                y2={y(conv(b.sets_ft))}
                stroke="var(--chart-wave-range)"
                strokeWidth={1.75}
                strokeLinecap="round"
                data-sets-tick=""
              />
              {peaks.has(s.i) && (
                <text x={cx} y={Math.max(11, Math.min(hi ?? top, y(conv(b.sets_ft))) - 4)} textAnchor="middle" className="fill-ink text-[10px] font-semibold tabular-nums">
                  {valueText(conv(b.central_ft))}
                </text>
              )}
              {showOffshore && s.step.offshore.hs_m != null && (
                <line
                  x1={x0 + 0.5}
                  x2={x0 + sw - 0.5}
                  y1={y(offshore(s.step.offshore.hs_m))}
                  y2={y(offshore(s.step.offshore.hs_m))}
                  stroke="var(--chart-offshore)"
                  strokeWidth={2}
                  strokeDasharray="3 2"
                  data-offshore-hs={s.step.offshore.hs_m}
                />
              )}
            </g>
          );
        })}
        <line x1={0} x2={width} y1={base + 0.5} y2={base + 0.5} stroke="var(--border)" />
        {showOffshore &&
          measured.map((o) => {
            const t = Date.parse(o.t);
            if (t < model.start || t > model.end) return null;
            const cx = scale(t);
            const cy = y(offshore(o.hs_m!));
            return (
              <path
                key={`${o.station_code}-${o.t}`}
                d={`M${cx},${cy - 5}L${cx + 5},${cy}L${cx},${cy + 5}L${cx - 5},${cy}Z`}
                fill="var(--card)"
                stroke="var(--chart-offshore)"
                strokeWidth={1.5}
                data-observation={o.station_code}
              />
            );
          })}
      </svg>
    </TimelineRow>
  );
});
