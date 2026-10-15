"use client";

import { memo, useMemo } from "react";

import { formatCalendarDate } from "@/lib/format";
import { hourTicks, type Scale, type TimelineModel } from "@/lib/timeline";
import { cn } from "@/lib/utils";

/**
 * The frame every timeline row shares: a sticky label column (the gutter, var(--tl-gutter)
 * wide) and a plot on the one time scale, over the same night bands and day separators. Rows
 * have fixed heights (ROW_H), so the cursor layer can place marks in any row without
 * measuring the DOM.
 */

export interface RowProps {
  model: TimelineModel;
  scale: Scale;
  /** Plot width in pixels (the whole horizon). */
  width: number;
}

export const HEADER_H = 48;
export const QUALITY_H = 34;
export const WAVE_H = 136;
export const SWELL_LANE_H = 19;
export const WIND_ARROW_H = 22;
export const WIND_BAR_H = 58;
export const WIND_LABEL_H = 16;
export const WIND_H = WIND_ARROW_H + WIND_BAR_H + WIND_LABEL_H;
export const TIDE_H = 84;

export const swellHeight = (lanes: number) => Math.max(1, lanes) * SWELL_LANE_H + 8;

/** The separator above every row below the header (TimelineRow's top border). */
export const ROW_BORDER = 1;

/** Top of each row's plot (px from the top of the header, below the row's 1 px top border)
 * for a timeline with `lanes` swell lanes, and the total height of all rows. */
export function rowOffsets(lanes: number) {
  const quality = HEADER_H + ROW_BORDER;
  const waves = quality + QUALITY_H + ROW_BORDER;
  const swell = waves + WAVE_H + ROW_BORDER;
  const wind = swell + swellHeight(lanes) + ROW_BORDER;
  const tide = wind + WIND_H + ROW_BORDER;
  return { header: 0, quality, waves, swell, wind, tide, total: tide + TIDE_H };
}

export function TimelineRow({
  testId,
  height,
  width,
  gutter,
  children,
  className,
  label,
}: {
  testId: string;
  height: number;
  width: number;
  gutter: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  /** Accessible name of the row (read in the data table's place by screen readers). */
  label: string;
}) {
  // The top border is ROW_BORDER px, counted in rowOffsets (the header, first, has none).
  return (
    <div className={cn("flex border-t border-[var(--chart-grid)] first:border-t-0", className)} data-testid={testId} data-row>
      <div
        className="sticky left-0 z-20 flex shrink-0 flex-col justify-center border-r border-[var(--chart-grid)] bg-card pr-2 pl-0.5 text-[11px] leading-tight text-muted-foreground"
        style={{ width: "var(--tl-gutter)", height }}
      >
        {gutter}
      </div>
      <div className="relative shrink-0" style={{ width, height }}>
        {children}
        <span className="sr-only">{label}</span>
      </div>
    </div>
  );
}

/** Night bands (last light to the next first light) and local-midnight separators, drawn
 * the same way under every row so they line up vertically. */
export const RowBackground = memo(function RowBackground({ model, scale, height }: { model: TimelineModel; scale: Scale; height: number }) {
  const right = scale(model.end);
  return (
    <g aria-hidden="true">
      {model.nights.map(([a, b]) => {
        const x0 = Math.max(0, scale(a));
        const x1 = Math.min(right, scale(b));
        return x1 > x0 ? <rect key={a} x={x0} y={0} width={x1 - x0} height={height} fill="var(--chart-night)" data-night="" /> : null;
      })}
      {model.days.map((d) => {
        const x = Math.round(scale(d.start)) + 0.5;
        if (x <= 1 || x >= right) return null;
        return <line key={d.date} x1={x} x2={x} y1={0} y2={height} stroke="var(--chart-day)" strokeWidth={1} data-day-boundary={d.date} />;
      })}
    </g>
  );
});

/** Day names with local dates, hour ticks from the axis (not from the sample times) and the
 * night bands, in the spot's zone. Day names stick to the left edge while their day is in
 * view. */
export const TimelineHeader = memo(function TimelineHeader({ model, scale, width, zoneLabel }: RowProps & { zoneLabel: string }) {
  const ticks = useMemo(() => hourTicks(model.start, model.end, model.tz, scale.pxPerHour), [model, scale]);
  return (
    <TimelineRow
      testId="timeline-header"
      height={HEADER_H}
      width={width}
      label="Days and hours, spot local time"
      gutter={
        <>
          <span className="font-semibold text-ink">Local time</span>
          <span className="truncate" title={zoneLabel}>
            {zoneLabel}
          </span>
        </>
      }
    >
      <svg width={width} height={HEADER_H} className="absolute inset-0 block" aria-hidden="true">
        <RowBackground model={model} scale={scale} height={HEADER_H} />
        {ticks.map((tick) => {
          if (tick.midnight) return null;
          const x = Math.round(scale(tick.t)) + 0.5;
          return (
            <g key={tick.t} data-tick={tick.label} data-t={tick.t}>
              <line x1={x} x2={x} y1={HEADER_H - 7} y2={HEADER_H} stroke="var(--chart-tick)" />
              {x > 16 && x < width - 16 && (
                <text x={x} y={HEADER_H - 11} textAnchor="middle" className="fill-muted-foreground text-[10px] tabular-nums" data-tick-label="">
                  {tick.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {/* Day names: HTML so each label can stick to the left edge while its day is in view. */}
      <div className="absolute inset-x-0 top-0 h-[24px]" style={{ width }}>
        {model.days.map((d) => {
          const left = Math.max(0, scale(d.start));
          const right = Math.min(width, scale(d.end));
          if (right - left < 1) return null;
          return (
            <div key={d.date} className="absolute top-0 h-full overflow-clip" style={{ left, width: right - left }} data-day={d.date}>
              <span className="sticky left-[calc(var(--tl-gutter)+2px)] inline-flex items-baseline gap-1 px-2 pt-1.5 text-xs whitespace-nowrap">
                <span className="font-semibold text-ink">{formatCalendarDate(d.date, { weekday: "short", month: undefined, day: undefined })}</span>
                <span className="text-muted-foreground">{formatCalendarDate(d.date)}</span>
              </span>
            </div>
          );
        })}
      </div>
    </TimelineRow>
  );
});
