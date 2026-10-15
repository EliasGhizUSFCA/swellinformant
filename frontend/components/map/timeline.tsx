"use client";

import { ChevronLeft, ChevronRight, Loader2, Pause, Play } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { bubbleLabel, longTimeLabel, stepHours, tickIndices, timelineDays, weekdaysRepeat } from "@/lib/map-time";
import { cn } from "@/lib/utils";

import { ACTIVE_PILL, FOCUS, GLASS } from "./map-ui";

/**
 * Forecast timeline: day labels under hour ticks, a draggable thumb with a bubble showing
 * the selected time in the viewer's zone, with the zone's name ("8am Sun PDT"), play/pause and
 * "Now". `aside` sits beside the bubble (the model run and lead time). The track is a slider:
 * arrows move one step, Page Up/Down one day of steps, Home/End to the ends.
 */
export function Timeline({
  steps,
  index,
  nowIndex,
  onChange,
  playing,
  onPlaying,
  tz,
  busy,
  aside,
}: {
  steps: string[];
  index: number;
  nowIndex: number;
  onChange: (i: number) => void;
  playing: boolean;
  onPlaying: (on: boolean) => void;
  tz: string;
  busy?: boolean;
  aside?: ReactNode;
}) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const observer = useRef<ResizeObserver | null>(null);
  const [width, setWidth] = useState(0);
  // A callback ref measures the track whenever it mounts (it is absent while there are no
  // steps), so the day labels never stay hidden behind a stale zero width.
  const setTrack = useCallback((el: HTMLDivElement | null) => {
    trackRef.current = el;
    observer.current?.disconnect();
    observer.current = null;
    if (!el) return;
    setWidth(el.getBoundingClientRect().width);
    observer.current = new ResizeObserver(([entry]) => setWidth(entry?.contentRect.width ?? 0));
    observer.current.observe(el);
  }, []);
  const dragging = useRef(false);
  const n = steps.length;
  const last = Math.max(0, n - 1);
  const pos = last > 0 ? index / last : 0;
  const days = useMemo(() => timelineDays(steps, tz), [steps, tz]);
  const ticks = useMemo(() => tickIndices(steps), [steps]);
  const current = steps[index];
  const dayStarts = useMemo(() => new Set(days.map((d) => d.first)), [days]);

  useEffect(() => () => observer.current?.disconnect(), []);

  const indexAt = (clientX: number) => {
    const r = trackRef.current?.getBoundingClientRect();
    if (!r || r.width <= 0) return index;
    return Math.round(Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * last);
  };
  const go = (i: number) => {
    const next = Math.min(last, Math.max(0, i));
    if (next !== index) onChange(next);
  };
  const perDay = Math.max(1, Math.round(24 / Math.max(0.25, stepHours(steps))));

  if (!n || !current) {
    return (
      <div className={cn("flex h-14 items-center justify-center rounded-2xl px-4 text-sm text-white/80", GLASS)} data-testid="time-control">
        No forecast times available yet.
      </div>
    );
  }

  return (
    <div className={cn("flex items-center gap-1.5 rounded-2xl px-2 pt-7 pb-1.5 sm:gap-2 sm:px-3", GLASS)} data-testid="time-control">
      <button
        type="button"
        onClick={() => onPlaying(!playing)}
        disabled={index >= last && !playing}
        aria-label={playing ? "Pause" : "Play forecast"}
        className={cn("flex size-10 shrink-0 items-center justify-center rounded-full bg-white/10 hover:bg-white/20 disabled:opacity-40", FOCUS)}
      >
        {playing ? <Pause className="size-4" /> : <Play className="size-4 translate-x-px" />}
      </button>
      <button
        type="button"
        aria-label="Earlier"
        disabled={index <= 0}
        onClick={() => go(index - 1)}
        className={cn("hidden size-8 shrink-0 items-center justify-center rounded-full hover:bg-white/10 disabled:opacity-30 sm:flex", FOCUS)}
      >
        <ChevronLeft className="size-4" />
      </button>
      <div className="relative min-w-0 flex-1 select-none">
        {/* Bubble over the thumb; slides from left- to right-aligned so it never overflows. */}
        <div
          className="pointer-events-none absolute -top-[26px] z-10 whitespace-nowrap"
          style={{ left: `${pos * 100}%`, transform: `translateX(-${pos * 100}%)` }}
        >
          <span className={cn("inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold shadow", ACTIVE_PILL)} data-testid="time-label">
            {bubbleLabel(current, tz, weekdaysRepeat(days))}
            {busy && <Loader2 className="size-3 motion-safe:animate-spin" aria-hidden />}
          </span>
          {/* Beside the bubble on the side with more room, so the bubble stays over the thumb. */}
          {aside && <span className={cn("absolute top-1/2 w-max -translate-y-1/2", pos < 0.5 ? "left-full ml-1.5" : "right-full mr-1.5")}>{aside}</span>}
        </div>
        <div
          ref={setTrack}
          role="slider"
          tabIndex={0}
          aria-label="Forecast time"
          aria-valuemin={0}
          aria-valuemax={last}
          aria-valuenow={index}
          aria-valuetext={longTimeLabel(current, tz)}
          data-testid="timeline-track"
          className={cn("relative h-11 cursor-pointer touch-none rounded-lg", FOCUS)}
          onPointerDown={(e) => {
            dragging.current = true;
            e.currentTarget.setPointerCapture(e.pointerId);
            onPlaying(false);
            go(indexAt(e.clientX));
          }}
          onPointerMove={(e) => {
            if (dragging.current) go(indexAt(e.clientX));
          }}
          onPointerUp={(e) => {
            dragging.current = false;
            e.currentTarget.releasePointerCapture(e.pointerId);
          }}
          onPointerCancel={() => {
            dragging.current = false;
          }}
          onKeyDown={(e) => {
            const moves: Record<string, number> = {
              ArrowLeft: index - 1,
              ArrowDown: index - 1,
              ArrowRight: index + 1,
              ArrowUp: index + 1,
              PageDown: index - perDay,
              PageUp: index + perDay,
              Home: 0,
              End: last,
            };
            if (e.key in moves) {
              e.preventDefault();
              onPlaying(false);
              go(moves[e.key]!);
            }
          }}
        >
          {/* rail */}
          <div className="absolute inset-x-0 top-[9px] h-1 rounded-full bg-white/20" />
          <div className="absolute top-[9px] left-0 h-1 rounded-full bg-white/55" style={{ width: `${pos * 100}%` }} />
          {/* "now" marker */}
          {last > 0 && (
            <div className="absolute top-[5px] h-3 w-0.5 rounded bg-[#c4ec4a]/80" style={{ left: `${(nowIndex / last) * 100}%` }} title="Now" />
          )}
          {/* hour ticks; taller at the start of each local day */}
          {ticks.map((i) => (
            <div
              key={i}
              aria-hidden
              className={cn("absolute top-[16px] w-px bg-white/35", dayStarts.has(i) ? "h-2.5 bg-white/60" : "h-1.5")}
              style={{ left: `${last > 0 ? (i / last) * 100 : 0}%` }}
            />
          ))}
          {/* day labels, centred on each day's span */}
          {days.map((d) => {
            const start = last > 0 ? d.first / last : 0;
            const end = last > 0 ? Math.min(1, (d.last + 1) / last) : 1;
            const px = (end - start) * width;
            const active = index >= d.first && index <= d.last;
            const text = active && px >= 74 ? d.label : px >= 26 ? d.weekday : px >= 12 ? d.weekday.charAt(0) : "";
            return (
              <div
                key={d.key}
                aria-hidden
                className={cn("absolute top-[26px] truncate text-center text-[11px] leading-4", active ? "font-semibold text-white" : "text-white/70")}
                style={{ left: `${start * 100}%`, width: `${(end - start) * 100}%` }}
              >
                {text}
              </div>
            );
          })}
          {/* thumb */}
          <div
            aria-hidden
            className="absolute top-[4px] size-3.5 -translate-x-1/2 rounded-full border-2 border-[#c4ec4a] bg-white shadow"
            style={{ left: `${pos * 100}%` }}
          />
        </div>
      </div>
      <button
        type="button"
        aria-label="Later"
        disabled={index >= last}
        onClick={() => go(index + 1)}
        className={cn("hidden size-8 shrink-0 items-center justify-center rounded-full hover:bg-white/10 disabled:opacity-30 sm:flex", FOCUS)}
      >
        <ChevronRight className="size-4" />
      </button>
      <button
        type="button"
        onClick={() => {
          onPlaying(false);
          onChange(nowIndex);
        }}
        disabled={index === nowIndex}
        className={cn("shrink-0 rounded-full bg-white/10 px-3 py-1.5 text-xs font-semibold hover:bg-white/20 disabled:opacity-40", FOCUS)}
      >
        Now
      </button>
    </div>
  );
}
