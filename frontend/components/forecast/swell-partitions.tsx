"use client";

import { memo, useMemo } from "react";

import { RowBackground, SWELL_LANE_H, swellHeight, TimelineRow, type RowProps } from "@/components/forecast/timeline-header";
import { arrowRotationDeg } from "@/lib/directions";
import { PARTITION_LABEL, PARTITION_SHORT } from "@/lib/timeline";

/** Smallest horizontal room (px) an arrow needs; narrower steps show every n-th arrow. */
const ARROW_ROOM = 13;
const MIN_LEN = 7;
const MAX_LEN = SWELL_LANE_H - 3;

/** An arrow of length `len` pointing up (north) around (0, 0): head and shaft. */
export function arrowPath(len: number): string {
  const a = len / 2;
  const hw = Math.max(2.6, len * 0.3);
  const hh = Math.max(3.2, len * 0.42);
  const sw = Math.max(0.7, len * 0.08);
  const f = (v: number) => Math.round(v * 100) / 100;
  return `M0,${f(-a)}L${f(hw)},${f(-a + hh)}L${f(sw)},${f(-a + hh)}L${f(sw)},${f(a)}L${f(-sw)},${f(a)}L${f(-sw)},${f(-a + hh)}L${f(-hw)},${f(-a + hh)}Z`;
}

/**
 * The wave systems of the wave model per forecast step, one lane per system the API sent
 * (primary, secondary, third swell, wind sea): an arrow per step that points where the waves
 * travel (direction FROM + 180, as on the map), sized by the system's offshore height. The
 * system that drives the surf (the API's ``drives_surf``) is drawn solid, the others in
 * outline, so the emphasis does not rely on colour. A step without that system leaves a gap;
 * nothing is filled in. Heights, periods and directions in numbers are in the selected-time
 * summary.
 */
export const SwellPartitions = memo(function SwellPartitions({ model, scale, width }: RowProps) {
  const kinds = model.kinds;
  const height = swellHeight(kinds.length);
  const geo = useMemo(() => {
    let max = 0;
    for (const s of model.steps) for (const p of s.step.partitions) max = Math.max(max, p.height_m);
    // sqrt: arrow area grows with height, so a 4 m swell is not drawn 8x as long as a 0.5 m one.
    const len = (h: number) => (max > 0 ? MIN_LEN + (MAX_LEN - MIN_LEN) * Math.sqrt(Math.min(1, h / max)) : MIN_LEN);
    return { len };
  }, [model]);
  return (
    <TimelineRow
      testId="swell-partitions"
      height={height}
      width={width}
      label="Swell and wind-sea direction per forecast step; arrows point where the waves travel"
      gutter={
        <div className="relative h-full w-full">
          <span className="absolute left-0 -translate-y-1/2 font-semibold text-ink max-sm:hidden" style={{ top: 4 + SWELL_LANE_H / 2 }}>
            Swell
          </span>
          {kinds.map((k, lane) => (
            <span key={k} className="absolute right-0 -translate-y-1/2 text-[10px]" style={{ top: 4 + lane * SWELL_LANE_H + SWELL_LANE_H / 2 }} title={PARTITION_LABEL[k]}>
              {PARTITION_SHORT[k]}
            </span>
          ))}
          {kinds.length === 0 && <span className="absolute right-0 bottom-1">—</span>}
        </div>
      }
    >
      <svg width={width} height={height} className="block" aria-hidden="true">
        <RowBackground model={model} scale={scale} height={height} />
        {kinds.slice(1).map((k, i) => (
          <line key={k} x1={0} x2={width} y1={4 + (i + 1) * SWELL_LANE_H} y2={4 + (i + 1) * SWELL_LANE_H} stroke="var(--chart-grid)" strokeDasharray="1 3" />
        ))}
        {kinds.length === 0 && (
          <text x={8} y={height / 2 + 4} className="fill-muted-foreground text-[11px]">
            The wave model sent no swell partitions for this run.
          </text>
        )}
        {model.steps.map((s) => {
          const w = scale(s.x1) - scale(s.x0);
          const every = Math.max(1, Math.ceil(ARROW_ROOM / Math.max(1, w)));
          const shown = s.i % every === 0;
          const cx = scale(s.t);
          return (
            <g key={s.i} className="tl-mark tl-swell" data-step={s.i} data-t={s.t} opacity={s.step.is_daylight ? 1 : 0.6}>
              {shown &&
                s.step.partitions.map((p) => {
                  const lane = kinds.indexOf(p.kind);
                  if (lane < 0 || p.dir_from_deg == null) return null;
                  const cy = 4 + lane * SWELL_LANE_H + SWELL_LANE_H / 2;
                  return (
                    <path
                      key={p.kind}
                      d={arrowPath(geo.len(p.height_m))}
                      transform={`translate(${Math.round(cx * 10) / 10},${cy}) rotate(${arrowRotationDeg(p.dir_from_deg)})`}
                      className={p.drives_surf ? "tl-arrow-drive" : "tl-arrow"}
                      fill={p.drives_surf ? "var(--chart-swell)" : "var(--card)"}
                      stroke="var(--chart-swell)"
                      strokeWidth={p.drives_surf ? 0.6 : 1.1}
                      strokeLinejoin="round"
                      data-partition={p.kind}
                      data-dir-from={p.dir_from_deg}
                      data-height={p.height_m}
                      data-drives={p.drives_surf ? "true" : undefined}
                    />
                  );
                })}
            </g>
          );
        })}
      </svg>
    </TimelineRow>
  );
});
