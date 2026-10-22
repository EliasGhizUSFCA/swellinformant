"use client";

import { X } from "lucide-react";
import { useMemo } from "react";

import { DemoBadge } from "@/components/quality";
import { useMapFlowStatus } from "@/hooks/map-fields";
import { ARROW_CONVENTION_TEXT } from "@/lib/directions";
import { formatDateTime, formatSpotDateTime } from "@/lib/format";
import { cssGradient, type LayerScale } from "@/lib/map-colors";
import type { FieldIndex } from "@/lib/map-fields";
import { layerDef, layerUnit, legendTicks, type DisplayUnits } from "@/lib/map-layers";
import type { MapSettings } from "@/lib/map-settings";
import { DASH_PASSES, FAMILY_STYLES, scaleStatement, TRAIL_STEPS, WAVE_SYSTEMS, type WaveSystem } from "@/lib/map-waves";
import { NO_FORECAST_COLOR, QUALITY_COLORS, QUALITY_COLORS_DARK, QUALITY_LABELS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { QualityLabel } from "@/types/api";

import { FOCUS, GLASS } from "./map-ui";
import type { ParticleMode } from "./map-canvas-layers";

/** The marker key in the ramp the markers use: the dark-surface ramp on the dark basemap. */
export function markerLegend(basemap: MapSettings["basemap"]): Array<{ label: string; color: string }> {
  const ramp = basemap === "dark" ? QUALITY_COLORS_DARK : QUALITY_COLORS;
  return [{ label: "No usable forecast", color: NO_FORECAST_COLOR }, ...(Object.keys(ramp) as QualityLabel[]).map((q) => ({ label: QUALITY_LABELS[q], color: ramp[q] }))];
}

/**
 * A few crest dashes in a wave system's colour and shape, as the map draws them (section 4.6
 * of docs/map-rendering.md): a soft halo under a round-capped core and, for the systems that
 * have one, the trail of where the dash just was behind it (the whole band, then its front
 * half). Two solid dashes for the primary swell and the combined sea, two split dashes for the
 * secondary swell (no trail), three short dashes for the wind sea. The swatch's dashes lie
 * along it and move up (their trails hang below them).
 */
export function CrestSwatch({ system, className }: { system: WaveSystem; className?: string }) {
  const s = FAMILY_STYLES[system];
  const core = 2 * s.widthFactor;
  const count = s.lengthFactor < 0.7 ? 3 : 2;
  const length = Math.min(11, 8 * s.lengthFactor);
  const step = 26 / count;
  const dashes: Array<[number, number]> = [];
  const pieces: Array<[number, number]> = [];
  for (let i = 0; i < count; i++) {
    const a = 1 + i * step + (step - length) / 2;
    const b = a + length;
    dashes.push([a, b]);
    if (s.gap > 0) {
      const mid = (a + b) / 2;
      const g = (s.gap * length) / 2 + core / 2; // the core's gap between its round caps
      pieces.push([a, mid - g], [mid + g, b]);
    } else {
      pieces.push([a, b]);
    }
  }
  const front = 2.4;
  const trail = 8 - front;
  return (
    <svg viewBox="0 0 28 8" className={cn("h-2 w-7 shrink-0", className)} aria-hidden data-swatch={system}>
      {s.trail &&
        TRAIL_STEPS.map((t, k) =>
          dashes.map(([a, b], i) => (
            <rect key={`trail-${k}-${i}`} data-trail={k} x={a - core / 2} y={front} width={b - a + core} height={trail * t.length} fill={s.color} fillOpacity={0.95 * t.alpha} />
          )),
        )}
      {DASH_PASSES.map((pass) =>
        pieces.map(([a, b], i) => (
          <line
            key={`${pass.width}-${i}`}
            x1={a}
            y1={front}
            x2={b}
            y2={front}
            stroke={s.color}
            strokeOpacity={0.95 * pass.alpha}
            strokeWidth={pass.width * core}
            strokeLinecap="round"
          />
        )),
      )}
    </svg>
  );
}

/** "+30 h" after the model cycle, or "analysis" at the cycle time itself. */
export function leadText(validTime: string | null | undefined, cycleTime: string | null | undefined): string | null {
  if (!validTime || !cycleTime) return null;
  const h = Math.round((Date.parse(validTime) - Date.parse(cycleTime)) / 3_600_000);
  if (!Number.isFinite(h)) return null;
  if (h === 0) return "analysis";
  return h > 0 ? `+${h} h` : `${h} h`;
}

/** Vertical "Legend" tab on the left edge. */
export function LegendTab({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      aria-controls="map-legend-drawer"
      className={cn("rounded-r-xl py-3 pr-1.5 pl-1 text-xs font-semibold tracking-wide [writing-mode:vertical-rl] hover:bg-[#23292f]/90", GLASS, FOCUS)}
      style={{ transform: "rotate(180deg)" }}
    >
      Legend
    </button>
  );
}

export function LegendDrawer({
  settings,
  scale,
  units,
  index,
  particles,
  onClose,
}: {
  /** Display settings: the active layer, the pressure overlay, ... */
  settings: MapSettings;
  scale: LayerScale;
  units: DisplayUnits;
  index: FieldIndex | null | undefined;
  particles: ParticleMode;
  onClose: () => void;
}) {
  const { layer, pressure } = settings;
  const def = layerDef(layer);
  const ticks = legendTicks(layer, scale, units);
  const available = !!index?.available;
  const status = useMapFlowStatus();
  const tz = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const windShown = layer === "wind" || settings.windOverlay;
  const crests = layer !== "wind" ? WAVE_SYSTEMS.filter((s) => status.systems.includes(s)) : [];
  const validTime = status.validTime;
  const lead = leadText(validTime, index?.cycle_time);
  const flowOn = particles !== "off" && available;
  return (
    <aside
      id="map-legend-drawer"
      aria-label="Legend"
      data-testid="legend-drawer"
      // On phones it stays clear of Leaflet's zoom buttons at the right edge.
      className={cn("flex max-h-full w-[min(320px,calc(100vw-6rem))] flex-col overflow-y-auto rounded-2xl p-4 text-sm", GLASS)}
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">Legend</h2>
        <button type="button" onClick={onClose} aria-label="Close legend" className={cn("rounded-full p-1 hover:bg-white/10", FOCUS)}>
          <X className="size-4" />
        </button>
      </div>

      <section aria-label={`${def.label} colour scale`}>
        <div className="mb-1.5 text-xs font-semibold text-white" data-testid="legend-title">
          {def.scaleTitle} ({layerUnit(layer, units)})
        </div>
        <div className="h-3 rounded-full ring-1 ring-white/15" style={{ background: cssGradient(scale.stops) }} data-testid="legend-scale" />
        <div className="relative mt-1 h-4 text-[11px] text-white/80 tabular-nums" data-testid="legend-ticks">
          {ticks.map((t, i) => (
            <span
              key={t.label}
              className="absolute"
              style={{ left: `${t.pos * 100}%`, transform: `translateX(${i === 0 && t.pos < 0.03 ? 0 : i === ticks.length - 1 && t.pos > 0.97 ? -100 : -50}%)` }}
            >
              {t.label}
            </span>
          ))}
        </div>
        <p className="mt-2 text-xs leading-relaxed text-white/80" data-testid="legend-about">
          {def.about}
        </p>
        {pressure && (
          <p className="mt-1.5 text-xs leading-relaxed text-white/70">Isobars: lines of equal air pressure every 4 hPa, labelled in hPa.</p>
        )}
      </section>

      {flowOn && (crests.length > 0 || windShown) && (
        <section className="mt-3 space-y-2 border-t border-white/10 pt-3 text-xs leading-relaxed text-white/75" data-testid="legend-flow" aria-label="Animation">
          {crests.length > 0 && (
            <div data-testid="legend-crests">
              <div className="mb-1 font-semibold text-white">Wave crests</div>
              <ul className="mb-1.5 space-y-1" aria-label="Wave systems drawn">
                {crests.map((s) => (
                  <li key={s} className="flex items-center gap-2 text-white/90">
                    <CrestSwatch system={s} />
                    <span>
                      {FAMILY_STYLES[s].label} <span className="text-white/55">({FAMILY_STYLES[s].look})</span>
                    </span>
                  </li>
                ))}
              </ul>
              <p>
                Short dashes are wave crests moving with the waves (a swell from the SW travels NE); the faint trail behind a white or cyan dash is
                where it just was. Longer, brighter dashes are higher waves. Rows of dashes are one crest spacing apart. <span data-testid="legend-scales">{scaleStatement(status.zoom, status.centreLat)}</span> Each dash
                fades in and out over three crest passages; the fades and the trails are display choices. Stylised crests from the forecast model, not individual waves.
              </p>
              {crests.includes("combined") && (
                <p className="mt-1 text-white/60">
                  One combined sea: the model&apos;s peak period and direction of all waves together. Swell partitions can be chosen in Settings when the run has them.
                </p>
              )}
              {particles === "static" && <p className="mt-1">Shown as still dashes with arrowheads because your device asks for reduced motion.</p>}
            </div>
          )}
          {windShown && (
            <p data-testid="legend-wind">
              White streaks drift with the wind, at a visual speed (not ground speed).
              {particles === "static" && " Shown as still arrows because your device asks for reduced motion."}
            </p>
          )}
          <p className="text-white/60" data-testid="legend-convention">
            {ARROW_CONVENTION_TEXT} Readouts say where the wind or waves come from.
          </p>
        </section>
      )}

      <section className="mt-4 border-t border-white/10 pt-3 text-xs text-white/75" data-testid="legend-source">
        {available && index?.source ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-white">{index.source.name}</span>
              {index.source.is_demo && <DemoBadge />}
            </div>
            <div className="mt-0.5">{index.source.attribution}</div>
            {index.cycle_time && <div className="mt-0.5">Model cycle {formatDateTime(index.cycle_time, "UTC")} UTC</div>}
            {index.stale && (
              <div className="mt-0.5 font-medium text-amber-200" data-testid="legend-stale">
                Out of date: this model run is {index.age_hours != null ? `${Math.round(index.age_hours)} h` : "more than " + Math.round(index.stale_after_hours) + " h"} old.
              </div>
            )}
            {validTime && (
              <div className="mt-0.5" data-testid="legend-valid">
                Valid {formatSpotDateTime(validTime, tz, { zone: true })}
                {/* The cycle's own time is the model's analysis, not a forecast (and not an observation). */}
                {lead === "analysis" ? " · model analysis (not observed)" : `${lead ? ` · ${lead}` : ""} (forecast, not observed)`}
              </div>
            )}
          </>
        ) : (
          <div>{index?.message ?? "Ocean layers are not available."}</div>
        )}
      </section>

      <section className="mt-4 border-t border-white/10 pt-3">
        <div className="mb-1.5 text-xs font-semibold text-white">Spot markers</div>
        <p className="mb-2 text-xs leading-relaxed text-white/75">
          Our predicted surf quality at each break for the selected time (not the offshore wave height above).
        </p>
        <ul className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs text-white/90" aria-label="Spot marker colours" data-testid="map-legend">
          {markerLegend(settings.basemap).map((l) => (
            <li key={l.label} className="flex items-center gap-1.5">
              <span className="size-2.5 shrink-0 rounded-full ring-1 ring-white/60" style={{ backgroundColor: l.color }} />
              {l.label}
            </li>
          ))}
        </ul>
      </section>
    </aside>
  );
}
