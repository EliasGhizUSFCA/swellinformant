"use client";

import * as RadioGroupPrimitive from "@radix-ui/react-radio-group";
import { Settings } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { availableSystems, hasPartitions, useFieldIndex } from "@/hooks/map-fields";
import type { Units } from "@/lib/format";
import { WIND_UNITS, type WindUnit } from "@/lib/map-layers";
import type { Basemap, MapSettings } from "@/lib/map-settings";
import { FAMILY_STYLES, type WaveSystem } from "@/lib/map-waves";
import { cn } from "@/lib/utils";
import type { SpotSummary } from "@/types/api";

import { CrestSwatch } from "./legend-drawer";
import { ACTIVE_PILL, DARK_TOKENS, FOCUS, GLASS } from "./map-ui";
import { FilterControls, type Filters } from "./spot-filters";

const CHOOSER: WaveSystem[] = ["primary", "secondary", "windsea", "combined"];

/** The next system choice after pressing `system`: the partitions combine freely (at least one
 * stays on); the combined sea is an alternative to them. */
export function toggleSystem(chosen: readonly WaveSystem[], system: WaveSystem): WaveSystem[] {
  if (system === "combined") return ["combined"];
  const partitions = chosen.filter((s) => s !== "combined");
  if (!partitions.includes(system)) return [...partitions, system];
  const rest = partitions.filter((s) => s !== system);
  return rest.length ? rest : partitions;
}

/**
 * Which wave systems are drawn as crests. Shown only when the model run has partitions;
 * a system the run does not have is disabled ("not available for this model run").
 */
function WaveSystemChooser({ settings, onChange }: { settings: MapSettings; onChange: (patch: Partial<MapSettings>) => void }) {
  const { data: index } = useFieldIndex();
  if (!hasPartitions(index)) return null;
  const available = availableSystems(index);
  return (
    <fieldset className="space-y-1.5" data-testid="wave-systems">
      <legend className="mb-1.5 text-sm text-white/85">Wave crests</legend>
      <div className="flex flex-wrap gap-1.5">
        {CHOOSER.map((system) => {
          const ok = available.has(system);
          const on = ok && settings.waveSystems.includes(system);
          return (
            <button
              key={system}
              type="button"
              aria-pressed={on}
              disabled={!ok}
              title={ok ? undefined : "Not available for this model run"}
              onClick={() => onChange({ waveSystems: toggleSystem(settings.waveSystems, system) })}
              className={cn(
                "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-white/10",
                on ? "bg-white/15 text-white ring-white/40" : "text-white/75 hover:bg-white/10",
                !ok && "cursor-not-allowed opacity-45 hover:bg-transparent",
                FOCUS,
              )}
            >
              <CrestSwatch system={system} className="w-5" />
              {FAMILY_STYLES[system].label}
              {!ok && <span className="sr-only">: not available for this model run</span>}
            </button>
          );
        })}
      </div>
      {CHOOSER.some((s) => !available.has(s)) && (
        <p className="text-xs text-white/55">
          Not available for this model run: {CHOOSER.filter((s) => !available.has(s)).map((s) => FAMILY_STYLES[s].label).join(", ")}.
        </p>
      )}
    </fieldset>
  );
}

function Segmented<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: Array<{ value: T; label: string }>; onChange: (v: T) => void }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-sm text-white/85" id={`seg-${label}`}>
        {label}
      </span>
      <RadioGroupPrimitive.Root
        value={value}
        onValueChange={(v) => onChange(v as T)}
        aria-labelledby={`seg-${label}`}
        orientation="horizontal"
        loop
        className="flex rounded-full bg-white/8 p-0.5 ring-1 ring-white/10"
      >
        {options.map((o) => (
          <RadioGroupPrimitive.Item
            key={o.value}
            value={o.value}
            className={cn("rounded-full px-2.5 py-1 text-xs font-medium text-white hover:bg-white/10", value === o.value && ACTIVE_PILL, value === o.value && "hover:bg-[#c4ec4a]", FOCUS)}
          >
            {o.label}
          </RadioGroupPrimitive.Item>
        ))}
      </RadioGroupPrimitive.Root>
    </div>
  );
}

/**
 * Settings: units, animation, the wind overlay, the wave systems drawn, basemap and every spot
 * filter with the matching count. Display preferences persist per browser (see
 * lib/map-settings.ts; the wind unit is shared with the spot charts); filters do not.
 */
export function SettingsPopover({
  settings,
  onChange,
  heightUnits,
  reducedMotion,
  spots,
  filters,
  setFilters,
  matching,
}: {
  settings: MapSettings;
  onChange: (patch: Partial<MapSettings>) => void;
  heightUnits: Units;
  reducedMotion: boolean;
  spots: SpotSummary[];
  filters: Filters;
  setFilters: (f: Filters) => void;
  matching: number;
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Settings"
          className={cn("flex h-10 items-center gap-2 rounded-full px-3.5 text-sm font-medium hover:bg-[#23292f]/90", GLASS, FOCUS)}
        >
          <Settings className="size-4" aria-hidden />
          <span className="hidden sm:inline">Settings</span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        collisionPadding={12}
        className="max-h-[var(--radix-popover-content-available-height)] w-[min(360px,calc(100vw-1.5rem))] overflow-y-auto border-white/10 bg-[#15191d]/95 p-4 text-white backdrop-blur-md"
        style={DARK_TOKENS}
        data-testid="map-settings"
      >
        <h2 className="mb-3 text-sm font-semibold">Map settings</h2>
        <div className="space-y-3">
          <Segmented<Units>
            label="Wave height"
            value={heightUnits}
            options={[
              { value: "ft", label: "ft" },
              { value: "m", label: "m" },
            ]}
            onChange={(v) => onChange({ heightUnits: v })}
          />
          <Segmented<WindUnit> label="Wind" value={settings.windUnits} options={WIND_UNITS.map((u) => ({ value: u, label: u }))} onChange={(v) => onChange({ windUnits: v })} />
          <Segmented<Basemap>
            label="Basemap"
            value={settings.basemap}
            options={[
              { value: "dark", label: "Dark" },
              { value: "streets", label: "Streets" },
            ]}
            onChange={(v) => onChange({ basemap: v })}
          />
          <div className="flex items-start justify-between gap-3">
            <label htmlFor="map-particles" className="text-sm text-white/85">
              Animated flow
              <span className="block text-xs text-white/60">
                {reducedMotion && settings.particles
                  ? "Your device asks for reduced motion, so wind is drawn as still arrows and waves as still crests."
                  : "Wind particles and wave crests."}
              </span>
            </label>
            <Switch id="map-particles" checked={settings.particles} onCheckedChange={(on) => onChange({ particles: on })} />
          </div>
          <div className="flex items-start justify-between gap-3">
            <label htmlFor="map-wind-overlay" className="text-sm text-white/85">
              Wind on wave layers
              <span className="block text-xs text-white/60">Wind particles over the wave crests.</span>
            </label>
            <Switch id="map-wind-overlay" checked={settings.windOverlay} onCheckedChange={(on) => onChange({ windOverlay: on })} />
          </div>
          <WaveSystemChooser settings={settings} onChange={onChange} />
        </div>
        <div className="mt-4 border-t border-white/10 pt-3">
          <div className="mb-2 flex items-baseline justify-between gap-2">
            <h3 className="text-sm font-semibold">Spots</h3>
            <span className="text-xs text-white/70" data-testid="filter-count">
              {matching} of {spots.length} spots match
            </span>
          </div>
          <FilterControls spots={spots} filters={filters} setFilters={setFilters} units={heightUnits} />
        </div>
      </PopoverContent>
    </Popover>
  );
}
