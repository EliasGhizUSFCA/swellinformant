"use client";

import { memo, useMemo } from "react";

import { QUALITY_H, RowBackground, TimelineRow, type RowProps } from "@/components/forecast/timeline-header";
import { asQuality, QUALITY_COLORS, QUALITY_LABELS, QUALITY_SHORT, QUALITY_TEXT } from "@/lib/quality";
import { qualityRuns } from "@/lib/timeline";

const BAR_Y = 6;
const BAR_H = 22;
// Night steps keep their class colour (the ramp's lightness carries the order, so fading it
// would make a night "Exceptional" read lighter than a daylight "Poor") and are drawn as an
// inset, shorter bar over the night band instead.
const NIGHT_INSET = 5;

/** Rough text width at 11px Inter semibold (to decide whether a label fits). */
const textWidth = (s: string) => s.length * 6.4 + 8;

/**
 * Our surf-quality rating per forecast step: one segment per API step (as wide as the step),
 * coloured on the ordinal quality ramp and labelled with the class in text wherever it fits
 * (the full name, or its short form in narrow runs), so the meaning never rests on colour
 * alone. Night steps keep their colour and are drawn as a shorter, inset bar (never faded,
 * which would break the ramp's order). The rating is the API's spot-specific score, never
 * derived from wave height here.
 */
export const QualityStrip = memo(function QualityStrip({ model, scale, width }: RowProps) {
  const runs = useMemo(() => qualityRuns(model.steps), [model]);
  return (
    <TimelineRow
      testId="quality-strip"
      height={QUALITY_H}
      width={width}
      label="Surf quality rating per forecast step"
      gutter={<span className="font-semibold text-ink">Surf quality</span>}
    >
      <svg width={width} height={QUALITY_H} className="block" aria-hidden="true">
        <RowBackground model={model} scale={scale} height={QUALITY_H} />
        {model.steps.map((s) => {
          const label = asQuality(s.step.quality.label);
          const x = scale(s.x0) + 0.5;
          const w = Math.max(1, scale(s.x1) - scale(s.x0) - 1);
          const inset = s.step.is_daylight ? 0 : NIGHT_INSET;
          return (
            <rect
              key={s.i}
              className="tl-mark tl-quality"
              x={x}
              y={BAR_Y + inset}
              width={w}
              height={BAR_H - 2 * inset}
              rx={1.5}
              fill={QUALITY_COLORS[label]}
              data-night={s.step.is_daylight ? undefined : ""}
              data-step={s.i}
              data-t={s.t}
              data-label={label}
              data-score={s.step.quality.score}
            />
          );
        })}
        {runs.map((r) => {
          const w = scale(r.to) - scale(r.from) - 1;
          const full = QUALITY_LABELS[r.label];
          const text = w >= textWidth(full) ? full : w >= textWidth(QUALITY_SHORT[r.label]) ? QUALITY_SHORT[r.label] : null;
          if (!text) return null;
          // Night bars are shorter: their label is a size smaller, in the class's own text colour.
          const night = !r.daylight;
          return (
            <text
              key={r.first}
              x={scale(r.from) + 0.5 + w / 2}
              y={BAR_Y + BAR_H / 2 + (night ? 3.5 : 4)}
              textAnchor="middle"
              className={night ? "pointer-events-none text-[10px] font-semibold" : "pointer-events-none text-[11px] font-semibold"}
              fill={QUALITY_TEXT[r.label]}
              data-quality-label={r.label}
            >
              {text}
            </text>
          );
        })}
      </svg>
    </TimelineRow>
  );
});
