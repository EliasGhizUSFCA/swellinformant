"use client";

import * as RadioGroupPrimitive from "@radix-ui/react-radio-group";
import { Settings } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import type { Units } from "@/lib/format";
import { WIND_UNITS, type WindUnit } from "@/lib/map-layers";
import type { Basemap, MapSettings } from "@/lib/map-settings";
import { cn } from "@/lib/utils";
import type { SpotSummary } from "@/types/api";

import { ACTIVE_PILL, DARK_TOKENS, FOCUS, GLASS } from "./map-ui";
import { FilterControls, type Filters } from "./spot-filters";

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
 * Settings: units, particles, basemap and every spot filter with the matching count.
 * Display preferences persist per browser (see lib/map-settings.ts); filters do not.
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
              Animated particles
              {reducedMotion && settings.particles && (
                <span className="block text-xs text-white/60">Your device asks for reduced motion, so the flow is drawn as still arrows.</span>
              )}
            </label>
            <Switch id="map-particles" checked={settings.particles} onCheckedChange={(on) => onChange({ particles: on })} />
          </div>
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
