"use client";

import { memo, useMemo } from "react";

import { RowBackground, TIDE_H, TimelineRow, type RowProps } from "@/components/forecast/timeline-header";
import { tideFitRuns, type TideFit } from "@/lib/best-windows";
import { safeExternalUrl } from "@/lib/confidence";
import { clockTime, smoothPath } from "@/lib/timeline";
import { convertHeightFt, M_PER_FT, type HeightUnit } from "@/lib/units";
import { cn } from "@/lib/utils";
import type { TideBlock } from "@/types/api";

const TOP = 22;
const BOTTOM = 8;

export const TIDE_UNAVAILABLE = "Tide data unavailable";

/** "MLLW", "MSL", "LAT" or "model mean sea level". */
export function datumText(datum: TideBlock["datum"]): string {
  if (!datum) return "datum not stated";
  return datum === "model_msl" ? "model mean sea level" : datum;
}

// The block's `source` (the backend's source code) as words for the chart legend.
const TIDE_SOURCE_LABEL: Record<string, string> = {
  noaa_coops: "NOAA CO-OPS predictions",
  eot20: "EOT20 tide model",
  fes2022: "FES2022 tide model",
  demo: "synthetic",
};

/** "NOAA CO-OPS predictions" for "noaa_coops", "EOT20 tide model" for "eot20"; an unknown
 * code is shown as it is. */
export function tideSourceText(source: string): string {
  return TIDE_SOURCE_LABEL[source] ?? source;
}

/** "EOT20 model point near Cloudbreak, 9 km away": where the tide curve is computed. */
function tideStationText(tide: TideBlock): string | null {
  const st = tide.station;
  if (!st) return null;
  return `${st.name}, ${Math.round(st.distance_km)} km away`;
}

/**
 * Under the tide row, always visible while real predictions are shown: where they come from
 * (station or model point and its distance), the source's attribution (EOT20's CC BY 4.0
 * credit "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE" with its change statement, or NOAA
 * CO-OPS) with the licence and dataset links the API sends (`attribution_links`), and the API's
 * caveat (`reason` on an available block: model mean sea level is not a chart datum, the
 * catalog's local caveat, or the CO-OPS datum and interpolation note).
 */
export function TideSourceNote({ tide, className }: { tide: TideBlock; className?: string }) {
  if (tide.status !== "available") return null;
  const where = tideStationText(tide);
  return (
    <div className={cn("space-y-0.5 text-[11px] text-muted-foreground", className)} data-testid="tide-source">
      <p>
        <span className="font-medium text-ink">Tide:</span> {where ? `${where} · ` : ""}
        {tide.attribution ? <span data-testid="tide-attribution">{tide.attribution}</span> : "source not stated"}
        {tide.attribution_links?.map((l) => {
          const href = safeExternalUrl(l.href);
          return (
            <span key={l.href} data-testid="tide-attribution-link">
              {" · "}
              {href ? (
                <a href={href} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2 hover:text-ink">
                  {l.label}
                </a>
              ) : (
                l.label
              )}
            </span>
          );
        })}
      </p>
      {tide.reason && <p data-testid="tide-caveat">{tide.reason}</p>}
    </div>
  );
}

/** Tint (behind the curve) and strip (at the bottom) of each tide suitability class: the
 * wind row's favourability colours, so "favourable / workable / unfavourable" reads alike. */
export const TIDE_FIT_STYLE: Record<TideFit, { fill: string; label: string }> = {
  favourable: { fill: "var(--wind-favourable)", label: "Favorable tide" },
  workable: { fill: "var(--wind-workable)", label: "Workable tide" },
  unfavourable: { fill: "var(--wind-unfavourable)", label: "Unfavorable tide" },
};
const FIT_STRIP_H = 4;

/** The y scale of the tide row (shared with the cursor layer's marker). */
export function tideScale(model: RowProps["model"], unit: HeightUnit) {
  const conv = (m: number) => convertHeightFt(m / M_PER_FT, unit);
  const hs = model.tide?.curve.map((p) => conv(p.h)) ?? [];
  const lo = hs.length ? Math.min(...hs) : 0;
  const hi = hs.length ? Math.max(...hs) : 1;
  const pad = (hi - lo) * 0.08 || 0.1;
  const min = lo - pad;
  const max = hi + pad;
  const y = (m: number) => TOP + (TIDE_H - TOP - BOTTOM) * (1 - (conv(m) - min) / (max - min || 1));
  return { y, conv, min, max };
}

/**
 * The predicted tide (astronomical tide from the tide block, never model sea level): a smooth
 * curve through the block's points with its high and low waters marked and timed in the
 * spot's zone, heights relative to the stated datum. Without a tide source the row says so
 * and draws nothing. The curve is drawn between the block's own points only (monotone, so it
 * never overshoots them); the summary reads each step's tide height from the API. Where the
 * tide is part of the rating, each step is shaded by its tide suitability (the score's own
 * tide fit, lib/best-windows tideFit); elsewhere nothing is shaded.
 */
export const TideRow = memo(function TideRow({ model, scale, width, tide, units }: RowProps & { tide: TideBlock; units: HeightUnit }) {
  const fits = useMemo(() => tideFitRuns(model.steps), [model]);
  const geo = useMemo(() => {
    if (!model.tide) return null;
    const ts = tideScale(model, units);
    const pts = model.tide.curve.map((p) => [scale(p.t), ts.y(p.h)] as const);
    const line = smoothPath(pts);
    const first = pts[0];
    const last = pts[pts.length - 1];
    const area = first && last ? `${line}L${last[0]},${TIDE_H}L${first[0]},${TIDE_H}Z` : "";
    return { ...ts, line, area };
  }, [model, scale, units]);
  const unavailable = !geo;
  const label = tide.status === "demo" ? "Demo tide (synthetic)" : unavailable ? TIDE_UNAVAILABLE : `Tide, ${units} above ${datumText(tide.datum)}`;
  return (
    <TimelineRow
      testId="tide-row"
      height={TIDE_H}
      width={width}
      label={label}
      gutter={
        <div className="relative h-full w-full">
          <span className="absolute top-1.5 left-0 font-semibold text-ink">Tide</span>
          {!unavailable && (
            <span className="absolute top-[19px] left-0 truncate" title={tide.status === "demo" ? `${units}, demo tide (synthetic)` : `${units} above ${datumText(tide.datum)}`}>
              {units}, {tide.status === "demo" ? "demo" : tide.datum === "model_msl" ? "model MSL" : (tide.datum ?? "datum ?")}
            </span>
          )}
        </div>
      }
    >
      <svg width={width} height={TIDE_H} className="block" aria-hidden="true" data-tide-status={tide.status}>
        <RowBackground model={model} scale={scale} height={TIDE_H} />
        {fits.map((r) => {
          const x = scale(r.from) + 0.5;
          const w = Math.max(1, scale(r.to) - scale(r.from) - 1);
          const st = TIDE_FIT_STYLE[r.fit];
          return (
            <g key={r.first} data-tide-fit={r.fit} data-first={r.first} data-last={r.last}>
              <rect x={x} y={0} width={w} height={TIDE_H} fill={st.fill} opacity={0.07} />
              <rect x={x} y={TIDE_H - FIT_STRIP_H} width={w} height={FIT_STRIP_H} fill={st.fill} opacity={0.85} />
            </g>
          );
        })}
        {geo && (
          <>
            <path d={geo.area} fill="var(--tide-fill)" />
            <path d={geo.line} fill="none" stroke="var(--tide-line)" strokeWidth={1.75} data-tide-curve="" />
            {model.tide!.events.map((e) => {
              const x = scale(e.t);
              const cy = geo.y(e.h);
              const high = e.kind === "high";
              const ty = high ? cy - 7 : cy + 13;
              return (
                <g key={e.t} data-tide-event={e.kind} data-t={e.t}>
                  <circle cx={x} cy={cy} r={2.75} fill={high ? "var(--tide-line)" : "var(--card)"} stroke="var(--tide-line)" strokeWidth={1.5} />
                  <text x={x} y={Math.max(9, Math.min(TIDE_H - 2, ty))} textAnchor="middle" className="fill-ink text-[9.5px] tabular-nums">
                    <tspan className="font-semibold">{high ? "H" : "L"}</tspan> {clockTime(e.t, model.tz)}
                  </text>
                </g>
              );
            })}
          </>
        )}
      </svg>
      {unavailable && (
        <div className="pointer-events-none absolute inset-0 flex items-center">
          <p className="sticky left-[calc(var(--tl-gutter)+10px)] z-20 max-w-[min(520px,70vw)] rounded-md bg-card/90 px-2 py-1 text-xs text-muted-foreground" data-testid="tide-unavailable">
            <span className="font-medium text-ink">{TIDE_UNAVAILABLE}</span>
            {tide.reason ? ` · ${tide.reason}` : ""}
          </p>
        </div>
      )}
    </TimelineRow>
  );
});
