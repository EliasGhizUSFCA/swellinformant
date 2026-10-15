"use client";

import { memo, useId, useMemo } from "react";

import { arrowPath } from "@/components/forecast/swell-partitions";
import { RowBackground, TimelineRow, WIND_ARROW_H, WIND_BAR_H, WIND_H, type RowProps } from "@/components/forecast/timeline-header";
import { arrowRotationDeg } from "@/lib/directions";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { niceTicks, RELATION_SHORT, relationRuns } from "@/lib/timeline";
import { convertWind, type WindUnit } from "@/lib/units";

const ARROW_ROOM = 13;
const ARROW_LEN = 12;
const BAR_TOP = WIND_ARROW_H;
const BAR_PAD = 4;

/** Favourability of the wind at this break (the API's, from its wind model) and its fill. */
export const FAVOURABILITY_FILL: Record<string, string> = {
  favourable: "var(--wind-favourable)",
  workable: "var(--wind-workable)",
  unfavourable: "var(--wind-unfavourable)",
};

export const FAVOURABILITY_LABEL: Record<string, string> = {
  favourable: "Favorable",
  workable: "Workable",
  unfavourable: "Unfavorable",
};

const textWidth = (s: string) => s.length * 5.6 + 6;

/**
 * Wind per forecast step: a bar as tall as the mean speed, a short dark tick at the gust (the
 * source's gust, labelled in the legend), an arrow that points where the wind blows to (FROM
 * + 180, the same convention as the map's particles) and the API's onshore/offshore relation
 * for this break written underneath. Bars are coloured by the API's favourability and carry
 * a pattern for "workable" (sparse hatching) and "unfavourable" (dense hatching), so the
 * class does not rely on colour; without a favourability (older runs) bars are neutral. A
 * step without wind data leaves a gap.
 */
export const WindRow = memo(function WindRow({ model, scale, width, unit }: RowProps & { unit: WindUnit }) {
  const uid = useId().replace(/:/g, "");
  const geo = useMemo(() => {
    let max = 0;
    for (const s of model.steps) {
      const w = s.step.wind;
      if (!w) continue;
      if (w.speed_kmh != null) max = Math.max(max, convertWind(w.speed_kmh, unit));
      if (w.gust_kmh != null) max = Math.max(max, convertWind(w.gust_kmh, unit));
    }
    const ticks = niceTicks(max * 1.02, 3);
    const top = ticks[ticks.length - 1] ?? 1;
    const y = (v: number) => BAR_TOP + BAR_PAD + (WIND_BAR_H - BAR_PAD) * (1 - Math.min(v, top) / top);
    return { ticks, y, runs: relationRuns(model.steps) };
  }, [model, unit]);
  const { ticks, y, runs } = geo;
  const base = y(0);
  const pattern = (fav: string | null | undefined) => (fav === "workable" ? `url(#${uid}-sparse)` : fav === "unfavourable" ? `url(#${uid}-dense)` : null);
  return (
    <TimelineRow
      testId="wind-row"
      height={WIND_H}
      width={width}
      label={`Wind speed in ${unit}, gusts, direction and relation to the break per forecast step`}
      gutter={
        <div className="relative h-full w-full">
          <span className="absolute left-0 -translate-y-1/2 font-semibold text-ink" style={{ top: WIND_ARROW_H / 2 }}>
            Wind
          </span>
          <span className="absolute left-0 -translate-y-1/2" style={{ top: WIND_ARROW_H / 2 + 13 }}>
            {unit}
          </span>
          {ticks.map((v) => (
            <span key={v} className="absolute right-0 -translate-y-1/2 tabular-nums" style={{ top: y(v) }} data-axis-tick={v}>
              {v}
            </span>
          ))}
        </div>
      }
    >
      {/* wind-chart: the test id of the chart this row replaced. */}
      <svg width={width} height={WIND_H} className="block" aria-hidden="true" data-testid="wind-chart">
        <defs>
          <pattern id={`${uid}-sparse`} width={5} height={5} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1={0} y1={0} x2={0} y2={5} stroke="var(--card)" strokeWidth={1.4} strokeOpacity={0.75} />
          </pattern>
          <pattern id={`${uid}-dense`} width={3.2} height={3.2} patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
            <line x1={0} y1={0} x2={0} y2={3.2} stroke="var(--card)" strokeWidth={1.2} strokeOpacity={0.8} />
          </pattern>
        </defs>
        <RowBackground model={model} scale={scale} height={WIND_H} />
        {ticks.slice(1).map((v) => (
          <line key={v} x1={0} x2={width} y1={y(v)} y2={y(v)} stroke="var(--chart-grid)" strokeDasharray="2 3" />
        ))}
        <line x1={0} x2={width} y1={base} y2={base} stroke="var(--border)" />
        {model.steps.map((s) => {
          const w = s.step.wind;
          if (!w) return null;
          const x0 = scale(s.x0) + 0.5;
          const bw = Math.max(1.5, scale(s.x1) - scale(s.x0) - 1);
          const cx = scale(s.t);
          const every = Math.max(1, Math.ceil(ARROW_ROOM / Math.max(1, bw + 1)));
          const fill = (w.favourability && FAVOURABILITY_FILL[w.favourability]) || "var(--wind-unrated)";
          const hatch = pattern(w.favourability);
          const speed = w.speed_kmh != null ? convertWind(w.speed_kmh, unit) : null;
          const gust = w.gust_kmh != null ? convertWind(w.gust_kmh, unit) : null;
          const top = speed != null ? y(speed) : base;
          const half = Math.max(2, Math.min(bw * 0.36, 9));
          return (
            <g
              key={s.i}
              className="tl-mark tl-wind"
              data-step={s.i}
              data-t={s.t}
              data-speed={w.speed_kmh ?? undefined}
              data-gust={w.gust_kmh ?? undefined}
              data-dir-from={w.dir_from_deg ?? undefined}
              data-relation={w.relation}
              data-favourability={w.favourability ?? undefined}
              opacity={s.step.is_daylight ? 1 : 0.6}
            >
              {speed != null && (
                <>
                  <rect className="tl-bar" x={x0} y={top} width={bw} height={Math.max(0, base - top)} fill={fill} />
                  {hatch && <rect x={x0} y={top} width={bw} height={Math.max(0, base - top)} fill={hatch} />}
                </>
              )}
              {gust != null && (
                <line x1={cx - half} x2={cx + half} y1={y(gust)} y2={y(gust)} stroke="var(--ink)" strokeWidth={1.75} strokeLinecap="round" data-gust-tick="" />
              )}
              {w.dir_from_deg != null && s.i % every === 0 && (
                <path
                  className="tl-wind-arrow"
                  d={arrowPath(ARROW_LEN)}
                  transform={`translate(${Math.round(cx * 10) / 10},${WIND_ARROW_H / 2}) rotate(${arrowRotationDeg(w.dir_from_deg)})`}
                  fill="var(--chart-swell)"
                  fillOpacity={0.85}
                  data-arrow-rotation={arrowRotationDeg(w.dir_from_deg)}
                />
              )}
            </g>
          );
        })}
        {runs.map((r) => {
          const x0 = scale(r.from);
          const w = scale(r.to) - x0;
          const full = WIND_RELATION_LABEL[r.relation] ?? r.relation;
          const short = RELATION_SHORT[r.relation] ?? r.relation;
          const text = w >= textWidth(full) ? full : w >= textWidth(short) ? short : null;
          return (
            <g key={r.first} data-relation-run={r.relation}>
              <line x1={x0 + 1.5} x2={x0 + w - 1.5} y1={WIND_H - 15.5} y2={WIND_H - 15.5} stroke="var(--chart-tick)" />
              {text && (
                <text x={x0 + w / 2} y={WIND_H - 4} textAnchor="middle" className="fill-muted-foreground text-[10px]">
                  {text}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </TimelineRow>
  );
});
