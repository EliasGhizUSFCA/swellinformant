"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { memo, Profiler, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ProfilerOnRenderCallback } from "react";

import { CursorLayer, HoverTooltip, NowMarker } from "@/components/forecast/cursor-layer";
import { QualityStrip } from "@/components/forecast/quality-strip";
import { SelectedTimeSummary } from "@/components/forecast/selected-summary";
import { SwellPartitions } from "@/components/forecast/swell-partitions";
import { datumText, TideRow, TIDE_UNAVAILABLE } from "@/components/forecast/tide-row";
import { HEADER_H, rowOffsets, TimelineHeader } from "@/components/forecast/timeline-header";
import { useTimelineSelection, useTimelineStore, useWindUnit, WIND_UNITS } from "@/components/forecast/timeline-selection";
import { WaveBars } from "@/components/forecast/wave-bars";
import { FAVOURABILITY_LABEL, WindRow } from "@/components/forecast/wind-row";
import { Disclosure } from "@/components/ui/disclosure";
import { ARROW_CONVENTION_TEXT } from "@/lib/directions";
import { formatCalendarDate } from "@/lib/format";
import { asQuality, QUALITY_COLORS, QUALITY_LABELS, QUALITY_SHORT, QUALITY_TEXT, WIND_RELATION_LABEL } from "@/lib/quality";
import { score10 } from "@/lib/rating";
import {
  directionText,
  drivingPartition,
  localDate,
  makeScale,
  nearestStepIndex,
  nextDay,
  nextStep,
  PARTITION_LABEL,
  partitionText,
  prevStep,
  pxPerHourFor,
  segmentsText,
  shortTime,
  stepLengthText,
  stepValueText,
  tideHeightText,
  timelineModel,
  zoneLabel,
  zoneName,
  type TimelineModel,
} from "@/lib/timeline";
import { formatHeightRange, formatWind, type HeightUnit, type WindUnit } from "@/lib/units";
import { cn } from "@/lib/utils";
import type { SpotTimeline } from "@/types/api";

/** Days in view: about three on wide screens, about one on a phone. */
const HOURS_IN_VIEW_WIDE = 72;
const HOURS_IN_VIEW_NARROW = 24;
const NARROW_PX = 640;

const reducedMotion = () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * The spot's forecast on one time axis: the selected-time summary, day navigation and the
 * chart rows (day and hour header, surf quality, breaking waves, swell, wind, tide), which all
 * share one scale and one label gutter, so day boundaries and the selected step line up
 * vertically in every row. The whole horizon is fetched once; selecting or hovering a step
 * never refetches and never re-renders the rows (only the cursor layer, the summary and the
 * day buttons follow the selection).
 *
 * Steps are the API's stored valid times, drawn as wide as the time they stand for (1, 3 or 6
 * hours); a selected time snaps to the nearest step. Nothing is interpolated.
 *
 * Must be rendered inside a TimelineSelectionProvider. ``onRowRender`` (tests) is called
 * whenever a row renders.
 */
export function SpotForecastTimeline({
  timeline,
  units,
  method,
  onRowRender,
}: {
  timeline: SpotTimeline;
  units: HeightUnit;
  /** Method line for "Why this forecast?" (breaking-height method and version). */
  method?: React.ReactNode;
  onRowRender?: ProfilerOnRenderCallback;
}) {
  const model = useMemo(() => timelineModel(timeline), [timeline]);
  const [windUnit, setWindUnit] = useWindUnit();
  const [showOffshore, setShowOffshore] = useState(false);
  const [legendOpen, setLegendOpen] = useState<boolean | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [viewWidth, setViewWidth] = useState(0);
  const store = useTimelineStore();
  const { select, hover } = store;

  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const measure = () => setViewWidth(el.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const narrow = viewWidth > 0 && viewWidth < NARROW_PX;
  const gutter = narrow ? 66 : 92;
  const scale = useMemo(() => {
    const view = Math.max(200, (viewWidth || 1000) - gutter);
    return makeScale(model.start, pxPerHourFor(view, narrow ? HOURS_IN_VIEW_NARROW : HOURS_IN_VIEW_WIDE));
  }, [model.start, viewWidth, gutter, narrow]);
  const width = Math.ceil(scale(model.end));
  const zone = zoneLabel(model.steps[0]?.t ?? model.start, model.tz);

  // --- scrolling -------------------------------------------------------------------------
  const scrollPlotTo = useCallback((x: number) => {
    const el = scrollerRef.current;
    if (!el) return;
    const left = Math.max(0, Math.min(x, el.scrollWidth - el.clientWidth));
    if (typeof el.scrollTo === "function") el.scrollTo({ left, behavior: reducedMotion() ? "auto" : "smooth" });
    else el.scrollLeft = left;
  }, []);

  // Open with the selected step in view (a little in from the left edge), as soon as the
  // frame is measured and a step is selected, whichever comes last.
  const opened = useRef(false);
  const openAt = useCallback(
    (t: number | null) => {
      const el = scrollerRef.current;
      if (opened.current || !el || viewWidth === 0 || t == null) return;
      opened.current = true;
      el.scrollLeft = Math.max(0, scale(t) - (viewWidth - gutter) * 0.2);
    },
    [viewWidth, scale, gutter],
  );
  useLayoutEffect(() => openAt(store.getState().selectedT), [openAt, store]);

  // Follow the selection without re-rendering anything: mark the selected step's marks in
  // every row (data-selected) and scroll as little as needed to keep it in view, however it
  // was chosen (keys, buttons, a deep link). The day buttons scroll on their own.
  const skipScroll = useRef(false);
  useEffect(() => {
    let last: number | null = null;
    const follow = () => {
      const t = store.getState().selectedT;
      if (t === last) return;
      last = t;
      const root = contentRef.current;
      if (root) {
        for (const el of root.querySelectorAll("[data-selected]")) el.removeAttribute("data-selected");
        if (t != null) for (const el of root.querySelectorAll(`[data-step="${nearestStepIndex(model.times, t)}"]`)) el.setAttribute("data-selected", "");
      }
      const el = scrollerRef.current;
      if (!opened.current) openAt(t);
      else if (skipScroll.current) skipScroll.current = false;
      else if (el && t != null) {
        const x = scale(t);
        const view = el.clientWidth - gutter;
        const margin = Math.min(48, view / 4);
        if (x < el.scrollLeft + margin) scrollPlotTo(x - margin);
        else if (x > el.scrollLeft + view - margin) scrollPlotTo(x - view + margin);
      }
    };
    follow();
    return store.subscribe(follow);
  }, [store, model, scale, gutter, scrollPlotTo, width, openAt]);

  // --- pointer -------------------------------------------------------------------------
  const pointerT = (clientX: number): { t: number; x: number } | null => {
    const el = scrollerRef.current;
    const content = contentRef.current;
    if (!el || !content || !model.times.length) return null;
    const frame = el.getBoundingClientRect();
    if (clientX < frame.left + gutter || clientX > frame.right) return null; // over the label column
    const x = clientX - content.getBoundingClientRect().left - gutter;
    const i = nearestStepIndex(model.times, scale.invert(x));
    return { t: model.times[i]!, x: clientX - frame.left };
  };
  const dragging = useRef(false);
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.pointerType !== "mouse" || e.button !== 0) return;
    const hit = pointerT(e.clientX);
    if (!hit) return;
    dragging.current = true;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    select(hit.t);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    const hit = pointerT(e.clientX);
    if (!hit) return hover(null);
    hover(hit.t, hit.x);
    if (dragging.current) select(hit.t);
  };
  const onPointerUp = () => {
    dragging.current = false;
  };
  // Taps (touch and pen) select on click, so a swipe that scrolls never selects.
  const onClick = (e: React.MouseEvent) => {
    const hit = pointerT(e.clientX);
    if (hit) select(hit.t);
  };

  // --- keyboard ------------------------------------------------------------------------
  const onKeyDown = (e: React.KeyboardEvent) => {
    const n = model.times.length;
    const t = store.getState().selectedT;
    if (!n) return;
    const i = t == null ? 0 : nearestStepIndex(model.times, t);
    let j: number | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") j = nextStep(n, i);
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown") j = prevStep(i);
    else if (e.key === "PageDown") j = nextDay(model.times, i, 1);
    else if (e.key === "PageUp") j = nextDay(model.times, i, -1);
    else if (e.key === "Home" || e.key === "End") {
      // Home / End: first / last step of the selected local day.
      const day = localDate(model.times[i]!, model.tz);
      const same = model.times.flatMap((x, k) => (localDate(x, model.tz) === day ? [k] : []));
      j = (e.key === "Home" ? same[0] : same[same.length - 1]) ?? i;
    }
    if (j == null) return;
    e.preventDefault();
    select(model.times[j]!);
    hover(null);
  };

  const onDay = useCallback(
    (date: string) => {
      const d = model.days.find((x) => x.date === date);
      if (!d) return;
      const inDay = model.steps.filter((s) => s.t >= d.start && s.t < d.end);
      const target = d.day.best_time ? Date.parse(d.day.best_time) : (inDay.find((s) => s.step.is_daylight)?.t ?? inDay[0]?.t);
      if (target == null) return;
      skipScroll.current = true;
      select(model.times[nearestStepIndex(model.times, target)]!);
      scrollPlotTo(scale(Math.max(model.start, d.start)));
    },
    [model, scale, select, scrollPlotTo],
  );

  const offsets = rowOffsets(model.kinds.length);
  const row = (id: string, node: React.ReactNode) =>
    onRowRender ? (
      <Profiler id={id} onRender={onRowRender}>
        {node}
      </Profiler>
    ) : (
      node
    );
  // The rows, created once per layout: the slider below re-renders with the selection, but
  // these elements stay the same objects, so React does not re-render them.
  const rows = useMemo(
    () => (
      <>
        {row("timeline-header", <TimelineHeader model={model} scale={scale} width={width} zoneLabel={narrow ? zoneName(model.steps[0]?.t ?? model.start, model.tz) : zone} />)}
        {row("quality-strip", <QualityStrip model={model} scale={scale} width={width} />)}
        {row("wave-bars", <WaveBars model={model} scale={scale} width={width} units={units} showOffshore={showOffshore} observations={timeline.observations} />)}
        {row("swell-partitions", <SwellPartitions model={model} scale={scale} width={width} />)}
        {row("wind-row", <WindRow model={model} scale={scale} width={width} unit={windUnit} />)}
        {row("tide-row", <TideRow model={model} scale={scale} width={width} tide={timeline.tide} units={units} />)}
        <NowMarker model={model} scale={scale} width={width} />
        <CursorLayer model={model} scale={scale} width={width} units={units} />
      </>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `row` only reads onRowRender
    [model, scale, width, zone, narrow, units, showOffshore, windUnit, timeline, onRowRender],
  );

  return (
    <div className="tl-root space-y-3" data-testid="quality-chart" style={{ "--tl-gutter": `${gutter}px` } as React.CSSProperties}>
      <SelectedTimeSummary model={model} timeline={timeline} units={units} windUnit={windUnit} method={method} />
      <DayNav model={model} onDay={onDay} />

      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-xs text-muted-foreground">
        <span data-testid="timeline-zone">
          Times in {zone} · {segmentsText(timeline.steps_meta.segments, model.tz)} · not interpolated
        </span>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            aria-pressed={showOffshore}
            onClick={() => setShowOffshore((v) => !v)}
            className={cn(
              "rounded-full border px-2.5 py-1 font-medium transition-colors",
              showOffshore ? "border-[var(--chart-offshore)] bg-[var(--chart-offshore)]/10 text-ink" : "bg-card hover:bg-muted",
            )}
            data-testid="toggle-offshore"
          >
            Offshore Hs (model)
          </button>
          <div className="flex items-center rounded-full border bg-card p-0.5" role="group" aria-label="Wind unit">
            {WIND_UNITS.map((u) => (
              <button
                key={u}
                type="button"
                aria-pressed={windUnit === u}
                onClick={() => setWindUnit(u)}
                className={cn("rounded-full px-2 py-0.5 font-medium transition-colors", windUnit === u ? "bg-ink text-white" : "text-muted-foreground hover:text-ink")}
              >
                {u}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="relative" data-testid="timeline-frame">
        <TimelineSlider model={model} units={units} scrollerRef={scrollerRef} onKeyDown={onKeyDown} onLeave={() => hover(null)}>
          <div
            ref={contentRef}
            className="relative cursor-crosshair select-none"
            style={{ width: gutter + width, height: offsets.total }}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onPointerLeave={() => hover(null)}
            onClick={onClick}
          >
            {rows}
          </div>
        </TimelineSlider>
        <HoverTooltip model={model} units={units} windUnit={windUnit} frameWidth={viewWidth} top={HEADER_H + 6} />
        <p id="timeline-keys" className="sr-only">
          Left and right arrow keys move one forecast step, Page Up and Page Down one day, Home and End to the first and last step of the day.
        </p>
      </div>

      {narrow ? (
        <Disclosure summary="How to read these charts" open={legendOpen ?? false} onOpenChange={setLegendOpen}>
          <TimelineLegend timeline={timeline} model={model} showOffshore={showOffshore} />
        </Disclosure>
      ) : (
        <TimelineLegend timeline={timeline} model={model} showOffshore={showOffshore} />
      )}
      <TimelineTable model={model} units={units} windUnit={windUnit} tide={timeline.tide} />
    </div>
  );
}

/**
 * The scrolling frame, exposed to assistive technology as a slider over the forecast steps
 * (value: the step index; value text: "Thu 7:00 AM, Very Good, 5–7 ft"). It re-renders with
 * the selection; its children (the rows) are passed in unchanged and are not re-rendered.
 */
function TimelineSlider({
  model,
  units,
  scrollerRef,
  onKeyDown,
  onLeave,
  children,
}: {
  model: TimelineModel;
  units: HeightUnit;
  scrollerRef: React.RefObject<HTMLDivElement | null>;
  onKeyDown: (e: React.KeyboardEvent) => void;
  onLeave: () => void;
  children: React.ReactNode;
}) {
  const index = useTimelineSelection((s) => (s.selectedT == null ? -1 : nearestStepIndex(model.times, s.selectedT)));
  const step = model.steps[index]?.step;
  return (
    <div
      ref={scrollerRef}
      className="tl-scroller scrollbar-thin relative overflow-x-auto overflow-y-hidden overscroll-x-contain rounded-xl border bg-card outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40"
      role="slider"
      tabIndex={0}
      aria-label="Forecast time"
      aria-valuemin={0}
      aria-valuemax={Math.max(0, model.times.length - 1)}
      aria-valuenow={Math.max(0, index)}
      aria-valuetext={step ? stepValueText(step, model.tz, formatHeightRange(step.breaking.typical_ft, step.breaking.sets_ft, units)) : "No forecast step selected"}
      aria-describedby="timeline-keys"
      onKeyDown={onKeyDown}
      onBlur={onLeave}
      onScroll={onLeave}
      data-testid="timeline-scroller"
    >
      {children}
    </div>
  );
}

/**
 * One button per local day (weekday, date and the day's best daylight rating); the pressed
 * one is the selected step's day. A button selects the day's best daylight step and scrolls
 * the timeline to the day's start.
 */
const DayNav = memo(function DayNav({ model, onDay }: { model: TimelineModel; onDay: (date: string) => void }) {
  const selectedDay = useTimelineSelection((s) => (s.selectedT == null || !model.times.length ? null : localDate(model.times[nearestStepIndex(model.times, s.selectedT)]!, model.tz)));
  const days = model.days.filter((d) => model.steps.some((s) => s.t >= d.start && s.t < d.end));
  const at = days.findIndex((d) => d.date === selectedDay);
  const listRef = useRef<HTMLDivElement>(null);
  // Keep the pressed day visible in the (horizontally scrolling) list. Only the list scrolls:
  // scrollIntoView would also scroll the page to it.
  useEffect(() => {
    const list = listRef.current;
    const btn = list?.querySelector<HTMLElement>('[aria-pressed="true"]');
    if (!list || !btn) return;
    const l = list.getBoundingClientRect();
    const b = btn.getBoundingClientRect();
    const delta = b.left < l.left ? b.left - l.left - 8 : b.right > l.right ? b.right - l.right + 8 : 0;
    if (!delta) return;
    if (typeof list.scrollBy === "function") list.scrollBy({ left: delta, behavior: reducedMotion() ? "auto" : "smooth" });
    else list.scrollLeft += delta;
  }, [selectedDay]);
  const nav = "shrink-0 rounded-lg border bg-card p-2 text-ink transition-colors hover:bg-muted disabled:opacity-40";
  return (
    <nav className="flex items-stretch gap-1.5" aria-label="Forecast days" data-testid="day-nav">
      <button type="button" className={nav} aria-label="Previous day" disabled={at <= 0} onClick={() => days[at - 1] && onDay(days[at - 1]!.date)}>
        <ChevronLeft className="size-4" />
      </button>
      <div ref={listRef} className="scrollbar-thin flex min-w-0 flex-1 gap-1.5 overflow-x-auto pb-0.5">
        {days.map((d) => {
          const q = d.day.best_label ? asQuality(d.day.best_label) : null;
          const pressed = d.date === selectedDay;
          return (
            <button
              key={d.date}
              type="button"
              aria-pressed={pressed}
              onClick={() => onDay(d.date)}
              className={cn(
                "flex min-w-[7rem] flex-1 flex-col items-start gap-1 rounded-xl border bg-card px-2.5 py-2 text-left transition-colors hover:border-ocean/60",
                pressed && "border-ink bg-secondary/60 ring-1 ring-ink",
              )}
              data-testid={`day-${d.date}`}
            >
              <span className="flex w-full items-baseline justify-between gap-1 text-xs">
                <span className="font-semibold text-ink">{formatCalendarDate(d.date, { weekday: "short", month: undefined, day: undefined })}</span>
                <span className="text-muted-foreground">{formatCalendarDate(d.date)}</span>
              </span>
              {q ? (
                <span
                  className="w-full truncate rounded-md px-1.5 py-0.5 text-center text-[11px] font-semibold"
                  style={{ backgroundColor: QUALITY_COLORS[q], color: QUALITY_TEXT[q] }}
                  title={`Best daylight rating: ${QUALITY_LABELS[q]}`}
                >
                  {QUALITY_LABELS[q]}
                </span>
              ) : (
                <span className="w-full rounded-md border border-dashed px-1.5 py-0.5 text-center text-[11px] text-muted-foreground">No daylight data</span>
              )}
              <span className="flex w-full justify-between gap-1 text-[11px] whitespace-nowrap text-muted-foreground tabular-nums">
                <span>{d.day.best_score != null ? `${score10(d.day.best_score)}/10` : "—"}</span>
                {d.day.good_hours > 0 && <span>{Math.round(d.day.good_hours * 10) / 10} h good</span>}
              </span>
            </button>
          );
        })}
      </div>
      <button type="button" className={nav} aria-label="Next day" disabled={at < 0 || at >= days.length - 1} onClick={() => days[at + 1] && onDay(days[at + 1]!.date)}>
        <ChevronRight className="size-4" />
      </button>
    </nav>
  );
});

function Swatch({ className, style }: { className?: string; style?: React.CSSProperties }) {
  return <span className={cn("inline-block h-2.5 w-4 shrink-0 rounded-[3px]", className)} style={style} aria-hidden="true" />;
}

/** What each mark means, the arrow convention, the tide's source and datum, and the spot's
 * tide preference. */
const TimelineLegend = memo(function TimelineLegend({ timeline, model, showOffshore }: { timeline: SpotTimeline; model: TimelineModel; showOffshore: boolean }) {
  const tide = timeline.tide;
  const rated = model.steps.some((s) => s.step.wind?.favourability);
  return (
    <div className="grid gap-x-8 gap-y-2.5 rounded-xl border bg-muted/30 p-3 text-xs text-muted-foreground lg:grid-cols-2" data-testid="quality-chart-legend">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="font-semibold text-ink">Surf quality</span>
        {(Object.keys(QUALITY_LABELS) as Array<keyof typeof QUALITY_LABELS>).map((k) => (
          <span key={k} className="inline-flex items-center gap-1">
            <Swatch style={{ backgroundColor: QUALITY_COLORS[k] }} />
            {QUALITY_LABELS[k]}
            <span className="sr-only"> ({QUALITY_SHORT[k]})</span>
          </span>
        ))}
        <span>deeper blue = better · faded = night</span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="font-semibold text-ink">Surf</span>
        <span className="inline-flex items-center gap-1">
          <Swatch className="bg-[var(--chart-wave)]" /> breaking height, central estimate
        </span>
        <span className="inline-flex items-center gap-1">
          <Swatch className="bg-[var(--chart-wave-band)]" /> likely range (p10–p90 of an assumed error model, not yet verified)
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-0.5 w-3 rounded bg-ink" aria-hidden="true" /> sets
        </span>
        {showOffshore && (
          <span className="inline-flex items-center gap-1">
            <span className="w-4 border-t-2 border-dashed border-[var(--chart-offshore)]" aria-hidden="true" /> offshore Hs (model), not surf height
          </span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="font-semibold text-ink">Swell</span>
        <span className="inline-flex items-center gap-1">
          <ArrowGlyph solid /> drives the surf
        </span>
        <span className="inline-flex items-center gap-1">
          <ArrowGlyph /> other systems (size = height)
        </span>
        <span>{ARROW_CONVENTION_TEXT}</span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="font-semibold text-ink">Wind</span>
        {rated ? (
          (["favourable", "workable", "unfavourable"] as const).map((k) => (
            <span key={k} className={cn("inline-flex items-center gap-1")}>
              <Swatch className={cn(`tl-fav-${k}`)} /> {FAVOURABILITY_LABEL[k]}
            </span>
          ))
        ) : (
          <span className="inline-flex items-center gap-1">
            <Swatch className="bg-[var(--wind-unrated)]" /> speed (favourability not rated for this run)
          </span>
        )}
        <span className="inline-flex items-center gap-1">
          <span className="h-0.5 w-3 rounded bg-ink" aria-hidden="true" /> gust
        </span>
        <span>Labels: relation to this break ({WIND_RELATION_LABEL.offshore}, {WIND_RELATION_LABEL["cross-shore"]}, …)</span>
        <span className="inline-flex items-center gap-1" data-testid="compass-legend">
          <CompassGlyph /> e.g. an arrow pointing east is a wind from the west
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 lg:col-span-2" data-testid="tide-caption">
        <span className="font-semibold text-ink">Tide</span>
        {tide.status === "unavailable" ? (
          <span>
            {TIDE_UNAVAILABLE}
            {tide.reason ? ` (${tide.reason})` : ""}
          </span>
        ) : (
          <span>
            {tide.status === "demo" ? "Demo tide (synthetic) · " : ""}
            {tide.station ? `${tide.station.name} (${Math.round(tide.station.distance_km)} km away) · ` : ""}
            {tide.source ? `${tide.source} · ` : ""}heights above {datumText(tide.datum)}
            {tide.attribution ? ` · ${tide.attribution}` : ""}
          </span>
        )}
        <span data-testid="tide-preference">
          Preferred tide here: {tide.preference.text}
        </span>
      </div>
      <ObservationsNote timeline={timeline} />
      <div className="lg:col-span-2">
        Shaded: night (last light to first light). Marks are centred on each forecast step&apos;s valid time and as wide as the time the step stands for (1, 3 or 6 hours).
      </div>
    </div>
  );
});

/** The latest instrument measurement near the spot, kept apart from the forecast: station,
 * distance, local time and value, marked stale when the provider's data is old. Nothing is
 * shown while the observations block is unavailable. */
function ObservationsNote({ timeline }: { timeline: SpotTimeline }) {
  const obs = timeline.observations;
  const latest = obs.items
    .filter((o) => o.kind === "instrument" && o.hs_m != null)
    .reduce<SpotTimeline["observations"]["items"][number] | null>((a, o) => (!a || Date.parse(o.t) > Date.parse(a.t) ? o : a), null);
  if (obs.status === "unavailable" || !latest) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 lg:col-span-2" data-testid="observations-note">
      <span className="font-semibold text-ink">Measured</span>
      <span className="inline-flex items-center gap-1">
        <svg viewBox="-6 -6 12 12" className="size-3" aria-hidden="true">
          <path d="M0,-5L5,0L0,5L-5,0Z" fill="var(--card)" stroke="var(--chart-offshore)" strokeWidth={1.5} />
        </svg>
        {latest.station_name} ({Math.round(latest.distance_km)} km away, {latest.provider}): Hs {latest.hs_m!.toFixed(1)} m at {shortTime(Date.parse(latest.t), timeline.timezone)}
        {latest.stale || obs.status === "stale" ? " (stale)" : ""} · an instrument measurement offshore, shown with “Offshore Hs (model)”
      </span>
    </div>
  );
}

/** A small compass with an example arrow pointing east (a westerly wind, travelling east). */
function CompassGlyph() {
  return (
    <svg viewBox="-12 -12 24 24" className="size-6" aria-hidden="true">
      <circle r={9} fill="none" stroke="var(--border)" />
      <text y={-5.5} textAnchor="middle" className="fill-muted-foreground text-[5.5px] font-semibold">
        N
      </text>
      <path d="M-5,0H3" stroke="var(--chart-swell)" strokeWidth={1.4} />
      <path d="M6,0L2,-2.6L2,2.6Z" fill="var(--chart-swell)" />
    </svg>
  );
}

function ArrowGlyph({ solid = false }: { solid?: boolean }) {
  return (
    <svg viewBox="-7 -7 14 14" className="size-3.5" aria-hidden="true">
      <path d="M0,-6L3.6,-0.8L0.9,-0.8L0.9,6L-0.9,6L-0.9,-0.8L-3.6,-0.8Z" fill={solid ? "var(--chart-swell)" : "var(--card)"} stroke="var(--chart-swell)" strokeWidth={solid ? 0.6 : 1.1} transform="rotate(45)" />
    </svg>
  );
}

/** Every step as a table (for screen readers and anyone who prefers numbers); mounted when
 * opened. */
const TimelineTable = memo(function TimelineTable({ model, units, windUnit, tide }: { model: TimelineModel; units: HeightUnit; windUnit: WindUnit; tide: SpotTimeline["tide"] }) {
  return (
    <Disclosure summary="Table of every forecast step" lazy data-testid="timeline-table">
      <div className="max-h-[28rem] overflow-auto rounded-lg border">
        <table className="w-full min-w-[56rem] text-left text-xs">
          <caption className="sr-only">Forecast steps in the spot&apos;s local time; heights in {units}, wind in {windUnit}.</caption>
          <thead className="sticky top-0 bg-muted text-muted-foreground">
            <tr>
              {["Time", "Step", "Surf quality", `Breaking (${units})`, "Swell driving the surf", "Wind", "Gust", "Relation", "Tide"].map((h) => (
                <th key={h} scope="col" className="px-2 py-1.5 font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {model.steps.map((s) => {
              const st = s.step;
              const drive = drivingPartition(st);
              const w = st.wind;
              return (
                <tr key={s.i} className="border-t">
                  <th scope="row" className="px-2 py-1 font-medium whitespace-nowrap text-ink">
                    {shortTime(s.t, model.tz)}
                  </th>
                  <td className="px-2 py-1">{stepLengthText(st.step_hours)}</td>
                  <td className="px-2 py-1">
                    {QUALITY_LABELS[asQuality(st.quality.label)]} {score10(st.quality.score)}
                    {st.is_daylight ? "" : " (night)"}
                  </td>
                  <td className="px-2 py-1 tabular-nums">{formatHeightRange(st.breaking.typical_ft, st.breaking.sets_ft, units)}</td>
                  <td className="px-2 py-1">{drive ? `${PARTITION_LABEL[drive.kind]} ${partitionText(drive, units)}` : "—"}</td>
                  <td className="px-2 py-1">{w?.speed_kmh != null ? `${formatWind(w.speed_kmh, windUnit)} from ${directionText(w.dir_from_deg)}` : "—"}</td>
                  <td className="px-2 py-1">{w?.gust_kmh != null ? formatWind(w.gust_kmh, windUnit) : "—"}</td>
                  <td className="px-2 py-1">{w ? (WIND_RELATION_LABEL[w.relation] ?? w.relation) : "—"}</td>
                  <td className="px-2 py-1 tabular-nums">{st.tide ? tideHeightText(st.tide.height_m, units) : tide.status === "unavailable" ? "unavailable" : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Disclosure>
  );
});
