"use client";

import { X } from "lucide-react";

import { DemoBadge } from "@/components/quality";
import { formatDateTime } from "@/lib/format";
import { cssGradient, type LayerScale } from "@/lib/map-colors";
import type { FieldIndex } from "@/lib/map-fields";
import { layerDef, layerUnit, legendTicks, type DisplayUnits, type LayerId } from "@/lib/map-layers";
import { NO_FORECAST_COLOR, QUALITY_COLORS, QUALITY_LABELS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { QualityLabel } from "@/types/api";

import { FOCUS, GLASS } from "./map-ui";
import type { ParticleMode } from "./forecast-map";

const MARKER_LEGEND: Array<{ label: string; color: string }> = [
  { label: "No usable forecast", color: NO_FORECAST_COLOR },
  ...(Object.keys(QUALITY_COLORS) as QualityLabel[]).map((q) => ({ label: QUALITY_LABELS[q], color: QUALITY_COLORS[q] })),
];

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
  layer,
  scale,
  units,
  pressure,
  index,
  particles,
  onClose,
}: {
  layer: LayerId;
  scale: LayerScale;
  units: DisplayUnits;
  pressure: boolean;
  index: FieldIndex | null | undefined;
  particles: ParticleMode;
  onClose: () => void;
}) {
  const def = layerDef(layer);
  const ticks = legendTicks(layer, scale, units);
  const available = !!index?.available;
  return (
    <aside
      id="map-legend-drawer"
      aria-label="Legend"
      data-testid="legend-drawer"
      className={cn("flex max-h-full w-[min(320px,calc(100vw-2rem))] flex-col overflow-y-auto rounded-2xl p-4 text-sm", GLASS)}
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">Legend</h2>
        <button type="button" onClick={onClose} aria-label="Close legend" className={cn("rounded-full p-1 hover:bg-white/10", FOCUS)}>
          <X className="size-4" />
        </button>
      </div>

      <section aria-label={`${def.label} colour scale`}>
        <div className="mb-1.5 text-xs font-semibold text-white" data-testid="legend-title">
          {def.label} ({layerUnit(layer, units)})
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
        {particles !== "off" && available && (
          <p className="mt-1.5 text-xs leading-relaxed text-white/70">
            {layer === "wind"
              ? "White streaks drift with the wind."
              : "White streaks move the way the waves travel, faster for longer periods."}
            {particles === "static" && " Shown as still arrows because your device asks for reduced motion."}
          </p>
        )}
        {pressure && (
          <p className="mt-1.5 text-xs leading-relaxed text-white/70">Isobars: lines of equal air pressure every 4 hPa, labelled in hPa.</p>
        )}
      </section>

      <section className="mt-4 border-t border-white/10 pt-3 text-xs text-white/75" data-testid="legend-source">
        {available && index?.source ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-white">{index.source.name}</span>
              {index.source.is_demo && <DemoBadge />}
            </div>
            <div className="mt-0.5">{index.source.attribution}</div>
            {index.cycle_time && <div className="mt-0.5">Model cycle {formatDateTime(index.cycle_time, "UTC")} UTC</div>}
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
