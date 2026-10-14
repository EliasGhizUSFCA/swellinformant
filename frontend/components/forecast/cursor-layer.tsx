"use client";

import { useEffect, useMemo, useState } from "react";

import { HEADER_H, rowOffsets, type RowProps } from "@/components/forecast/timeline-header";
import { tideScale } from "@/components/forecast/tide-row";
import { useTimelineSelection } from "@/components/forecast/timeline-selection";
import { FAVOURABILITY_LABEL } from "@/components/forecast/wind-row";
import { asQuality, QUALITY_COLORS, QUALITY_LABELS, WIND_RELATION_LABEL } from "@/lib/quality";
import { score10 } from "@/lib/rating";
import { clockTime, directionText, drivingPartition, nearestStepIndex, PARTITION_LABEL, partitionText, selectedTimeTitle, stepLengthText, tideHeightText } from "@/lib/timeline";
import { formatHeightRange, formatWind, type HeightUnit, type WindUnit } from "@/lib/units";
import type { TimelineStep } from "@/types/api";

/**
 * Everything on the timeline that follows the pointer or the selection, in one overlay above
 * the rows, so moving the pointer or selecting a step re-renders this layer (and the summary)
 * only, never the rows underneath: the selected step's column and line (one x for every row),
 * its time in the header, the tide height at that step, and the hover line.
 */
export function CursorLayer({ model, scale, width, units }: RowProps & { units: HeightUnit }) {
  const now = useNow();
  const selectedT = useTimelineSelection((s) => s.selectedT);
  const hoverT = useTimelineSelection((s) => s.hoverT);
  const offsets = rowOffsets(model.kinds.length);
  const tideY = useMemo(() => (model.tide ? tideScale(model, units).y : null), [model, units]);
  const sel = selectedT == null ? -1 : nearestStepIndex(model.times, selectedT);
  const hov = hoverT == null ? -1 : nearestStepIndex(model.times, hoverT);
  const s = model.steps[sel];
  const h = hov !== sel ? model.steps[hov] : undefined;
  const total = offsets.total;
  return (
    <svg
      className="pointer-events-none absolute top-0 z-10 block"
      style={{ left: "var(--tl-gutter)" }}
      width={width}
      height={total}
      aria-hidden="true"
      data-testid="timeline-cursor"
    >
      {h && (
        <line
          x1={scale(h.t)}
          x2={scale(h.t)}
          y1={HEADER_H}
          y2={total}
          stroke="var(--chart-hover)"
          strokeWidth={1}
          strokeDasharray="3 3"
          data-cursor="hover"
          data-t={h.t}
        />
      )}
      {now >= model.start && now <= model.end && !(s && s.x0 <= now && now < s.x1) && (
        <NowLabel x={scale(now)} width={width} hidden={s != null && Math.abs(scale(now) - scale(s.t)) < 46} />
      )}
      {s && (
        <g data-cursor="selected" data-step={s.i} data-t={s.t} data-x={scale(s.t)}>
          <rect x={scale(s.x0)} y={HEADER_H} width={Math.max(1, scale(s.x1) - scale(s.x0))} height={total - HEADER_H} fill="var(--chart-select-band)" />
          <line x1={scale(s.t)} x2={scale(s.t)} y1={HEADER_H - 2} y2={total} stroke="var(--chart-select)" strokeWidth={1.5} />
          <SelectedPill x={scale(s.t)} width={width} text={`${s.x0 <= now && now < s.x1 ? "Now · " : ""}${clockTime(s.t, model.tz)}`} />
          {tideY && s.step.tide && (
            <circle
              cx={scale(s.t)}
              cy={offsets.tide + tideY(s.step.tide.height_m)}
              r={4}
              fill="var(--card)"
              stroke="var(--chart-select)"
              strokeWidth={2}
              data-tide-marker=""
              data-height={s.step.tide.height_m}
            />
          )}
        </g>
      )}
    </svg>
  );
}

/** Epoch ms, refreshed every minute. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

function NowLabel({ x, width, hidden }: { x: number; width: number; hidden: boolean }) {
  if (hidden) return null;
  const w = 32;
  const left = Math.max(1, Math.min(width - w - 1, x - w / 2));
  return (
    <g data-now-label="">
      <rect x={left} y={HEADER_H - 19} width={w} height={16} rx={8} fill="var(--card)" stroke="var(--chart-now)" />
      <text x={left + w / 2} y={HEADER_H - 7.5} textAnchor="middle" className="text-[9.5px] font-semibold tracking-wide uppercase" fill="var(--chart-now)">
        Now
      </text>
    </g>
  );
}

function SelectedPill({ x, width, text }: { x: number; width: number; text: string }) {
  const w = text.length * 6.2 + 12;
  const left = Math.max(1, Math.min(width - w - 1, x - w / 2));
  return (
    <g data-selected-pill="">
      <rect x={left} y={HEADER_H - 19} width={w} height={16} rx={8} fill="var(--chart-select)" />
      <text x={left + w / 2} y={HEADER_H - 7.5} textAnchor="middle" className="text-[10px] font-semibold tabular-nums" fill="#fff">
        {text}
      </text>
    </g>
  );
}

/** The current time across every row (updated each minute), when it lies on the axis; its
 * "Now" label is drawn by the cursor layer, which can merge it with the selected time. */
export function NowMarker({ model, scale, width }: RowProps) {
  const now = useNow();
  if (now < model.start || now > model.end) return null;
  const x = scale(now);
  const total = rowOffsets(model.kinds.length).total;
  return (
    <svg
      className="pointer-events-none absolute top-0 z-[5] block"
      style={{ left: "var(--tl-gutter)" }}
      width={width}
      height={total}
      aria-hidden="true"
      data-testid="now-marker"
      data-t={now}
    >
      <line x1={x} x2={x} y1={HEADER_H - 3} y2={total} stroke="var(--chart-now)" strokeWidth={1.25} strokeDasharray="2 3" />
    </svg>
  );
}

/** The values of one step in short lines: the hover tooltip and the keyboard readout. */
export function StepTooltipContent({ step, tz, units, windUnit }: { step: TimelineStep; tz: string; units: HeightUnit; windUnit: WindUnit }) {
  const q = asQuality(step.quality.label);
  const b = step.breaking;
  const drive = drivingPartition(step);
  const w = step.wind;
  return (
    <>
      <div className="font-semibold text-ink">{selectedTimeTitle(Date.parse(step.t), tz)}</div>
      <div className="text-muted-foreground">
        {stepLengthText(step.step_hours)}
        {step.is_daylight ? "" : " · night"}
      </div>
      <div className="mt-1 flex items-center gap-1.5">
        <span className="size-2.5 shrink-0 rounded-[3px]" style={{ backgroundColor: QUALITY_COLORS[q] }} aria-hidden="true" />
        <span className="font-medium text-ink">{QUALITY_LABELS[q]}</span>
        <span className="tabular-nums">{score10(step.quality.score)}/10</span>
      </div>
      <div>
        Surf {formatHeightRange(b.typical_ft, b.sets_ft, units)} <span className="text-muted-foreground">(central {formatHeightRange(b.central_ft, b.central_ft, units)})</span>
      </div>
      {drive && (
        <div>
          {PARTITION_LABEL[drive.kind]} {partitionText(drive, units)}
        </div>
      )}
      <div>
        Wind {w?.speed_kmh != null ? `${formatWind(w.speed_kmh, windUnit)} from ${directionText(w.dir_from_deg)}` : "—"}
        {w ? ` · ${WIND_RELATION_LABEL[w.relation] ?? w.relation}` : ""}
        {w?.favourability ? ` · ${FAVOURABILITY_LABEL[w.favourability] ?? w.favourability}` : ""}
      </div>
      {w?.gust_kmh != null && <div data-tooltip-gust="">gust {formatWind(w.gust_kmh, windUnit)}</div>}
      <div>Tide {step.tide ? `${tideHeightText(step.tide.height_m, units)}${step.tide.rising == null ? "" : step.tide.rising ? ", rising" : ", falling"}` : "—"}</div>
    </>
  );
}

/** The hover tooltip, beside the pointer and kept inside the timeline frame. */
export function HoverTooltip({
  model,
  units,
  windUnit,
  frameWidth,
  top,
}: {
  model: RowProps["model"];
  units: HeightUnit;
  windUnit: WindUnit;
  frameWidth: number;
  top: number;
}) {
  const hoverT = useTimelineSelection((s) => s.hoverT);
  const hoverX = useTimelineSelection((s) => s.hoverX);
  if (hoverT == null || hoverX == null) return null;
  const i = nearestStepIndex(model.times, hoverT);
  const s = model.steps[i];
  if (!s) return null;
  const W = 232;
  const right = hoverX + 14 + W <= frameWidth;
  const left = right ? hoverX + 14 : Math.max(0, hoverX - 14 - W);
  return (
    <div
      role="tooltip"
      id="timeline-tooltip"
      className="pointer-events-none absolute z-30 rounded-xl border bg-card/95 px-3 py-2 text-xs leading-relaxed text-muted-foreground shadow-lg backdrop-blur-sm"
      style={{ left, top, width: W }}
      data-testid="timeline-tooltip"
      data-step={i}
    >
      <StepTooltipContent step={s.step} tz={model.tz} units={units} windUnit={windUnit} />
    </div>
  );
}
