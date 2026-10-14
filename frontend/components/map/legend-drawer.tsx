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
import { FAMILY_STYLES, scaleStatement, WAVE_SYSTEMS, type WaveSystem } from "@/lib/map-waves";
import { NO_FORECAST_COLOR, QUALITY_COLORS, QUALITY_LABELS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { QualityLabel } from "@/types/api";

import { FOCUS, GLASS } from "./map-ui";
import type { ParticleMode } from "./map-canvas-layers";

const MARKER_LEGEND: Array<{ label: string; color: string }> = [
  { label: "No usable forecast", color: NO_FORECAST_COLOR },
  ...(Object.keys(QUALITY_COLORS) as QualityLabel[]).map((q) => ({ label: QUALITY_LABELS[q], color: QUALITY_COLORS[q] })),
];

/** A short line in a wave system's colour and dash pattern (the key of the crest styles). */
export function CrestSwatch({ system, className }: { system: WaveSystem; className?: string }) {
  const s = FAMILY_STYLES[system];
  return (
    <svg viewBox="0 0 28 8" className={cn("h-2 w-7 shrink-0", className)} aria-hidden>
      <line
        x1="1"
        y1="4"
        x2="27"
        y2="4"
        stroke={s.color}
        strokeWidth={2}
        strokeDasharray={s.dash.length ? s.dash.join(" ") : undefined}
        strokeLinecap={s.look === "dotted" ? "round" : "butt"}
      />
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
              style={{ left: `${t.pos * 100}%`, transform: `translateX(${i === 0 ? 0 : i === ticks.length - 1 ? -100 : -50}%)` }}
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
                Crests travel the way the waves go (a swell from the SW travels NE); thicker, brighter lines are higher waves.{" "}
                <span data-testid="legend-scales">{scaleStatement(status.zoom, status.centreLat)}</span> Stylised crests from the forecast model, not
                individual waves.
              </p>
              {crests.includes("combined") && (
                <p className="mt-1 text-white/60">One combined sea: the model&apos;s peak period and direction of all waves together.</p>
              )}
              {particles === "static" && <p className="mt-1">Shown as still crests with arrowheads because your device asks for reduced motion.</p>}
            </div>
          )}
          {windShown && (
            <p data-testid="legend-wind">
              White streaks drift with the wind, at a visual speed (not ground speed).
              {particles === "static" && " Shown as still arrows because your device asks for reduced motion."}
            </p>
          )}
          <p className="text-white/60" data-testid="legend-convention">
            {ARROW_CONVENTION_TEXT} Streaks and crests move the same way; readouts say where the wind or waves come from.
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
            {validTime && (
              <div className="mt-0.5" data-testid="legend-valid">
                Valid {formatSpotDateTime(validTime, tz, { zone: true })}
                {lead && ` · ${lead}`} (forecast, not observed)
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
          {MARKER_LEGEND.map((l) => (
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
