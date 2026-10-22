"use client";

import { X } from "lucide-react";
import { useId, useMemo, type JSX } from "react";

import { DemoBadge } from "@/components/quality";
import { useMapFlowStatus } from "@/hooks/map-fields";
import { ARROW_CONVENTION_TEXT } from "@/lib/directions";
import { formatDateTime, formatSpotDateTime } from "@/lib/format";
import { colorAtValue, cssGradient, rgbToHex, WAVE_HEIGHT_RAMP, type LayerScale } from "@/lib/map-colors";
import type { FieldIndex } from "@/lib/map-fields";
import { layerDef, layerUnit, legendTicks, type DisplayUnits } from "@/lib/map-layers";
import type { MapSettings } from "@/lib/map-settings";
import { BLOB_MAX_DEPTH_SHARE, BLOB_PROFILE, dashTint, FAMILY_STYLES, scaleStatement, WAVE_SYSTEMS, type WaveSystem } from "@/lib/map-waves";
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

/** The sea colour the swatches are drawn on: the Wave Height ramp at 2.5 m, a typical sea. */
const SWATCH_SEA_M = 2.5;

/**
 * A few crest dashes in a wave system's shape and tint, as the map draws them (section 4.6 of
 * docs/map-rendering.md), on a patch of a typical sea: soft blobs with a bright leading edge
 * and a smear fading behind it (two for the primary swell and the combined sea, three short
 * ones for the wind sea), or two thin split bars for the secondary swell. Each is a lighter
 * tint of the sea under it, mixed with the system's own colour for the partitions. The
 * swatch's dashes lie along it and move up (their smears hang below them).
 */
export function CrestSwatch({ system, className }: { system: WaveSystem; className?: string }) {
  const id = useId().replace(/[^A-Za-z0-9_-]/g, ""); // a plain id for url(#...)
  const s = FAMILY_STYLES[system];
  const sea = colorAtValue(WAVE_HEIGHT_RAMP, SWATCH_SEA_M);
  const [r, g, b] = dashTint(sea, system);
  const tint = `rgb(${r}, ${g}, ${b})`;
  const count = s.lengthFactor < 0.8 ? 3 : 2;
  const length = Math.min(11, 8.5 * s.lengthFactor);
  const step = 26 / count;
  const front = 2.5;
  const gradient = `${id}-blob`;
  const shapes: JSX.Element[] = [];
  for (let i = 0; i < count; i++) {
    const a = 1 + i * step + (step - length) / 2;
    if (s.shape === "blob") {
      shapes.push(<rect key={i} data-blob={i} x={a} y={front} width={length} height={Math.min(length * BLOB_MAX_DEPTH_SHARE, 5.5) * s.depthFactor} fill={`url(#${gradient})`} />);
    } else {
      const piece = (length * (1 - s.gap)) / 2;
      for (const [k, x] of [a, a + length - piece].entries()) {
        shapes.push(<rect key={`${i}-${k}`} data-bar={i} x={x} y={front + 0.6} width={piece} height={1.6} fill={tint} />);
      }
    }
  }
  return (
    <svg viewBox="0 0 28 10" className={cn("h-2.5 w-7 shrink-0 rounded-[2px]", className)} aria-hidden data-swatch={system}>
      <defs>
        <linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1">
          {BLOB_PROFILE.map((p) => (
            <stop key={p.at} offset={p.at} stopColor={tint} stopOpacity={0.85 * p.alpha} />
          ))}
        </linearGradient>
      </defs>
      <rect width={28} height={10} rx={2} fill={rgbToHex(sea)} />
      {shapes}
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
                Short dashes are wave crests moving with the waves (a swell from the SW travels NE): the bright edge is the crest, the fading smear
                behind it where it just was. Bigger, brighter dashes are higher waves; each is a lighter tint of the colour under it. Rows are one
                crest spacing apart. <span data-testid="legend-scales">{scaleStatement(status.zoom, status.centreLat)}</span> Fades and smears are
                display choices. Stylised crests from the forecast model, not individual waves.
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
