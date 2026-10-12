"use client";

import { X } from "lucide-react";
import { useMemo } from "react";

import { fromUnits, titleCase, type Units } from "@/lib/format";
import { QUALITY_OPTIONS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { MapSpot, SpotSummary } from "@/types/api";

import { FOCUS } from "./map-ui";

export interface Filters {
  region: string;
  country: string;
  breakType: string;
  minHeight: string; // in the user's units; "" = any
  minQuality: string; // QualityLabel or ""
  eventsOnly: boolean;
}

export const NO_FILTERS: Filters = { region: "All", country: "All", breakType: "any", minHeight: "", minQuality: "", eventsOnly: false };

export const isFiltered = (f: Filters) => JSON.stringify(f) !== JSON.stringify(NO_FILTERS);

/** Whether a spot passes the filters at the selected time (height = our breaking-height prediction). */
export function passes(s: SpotSummary, m: MapSpot | undefined, f: Filters, units: Units): boolean {
  if (f.region !== "All" && s.region_group !== f.region) return false;
  if (f.country !== "All" && s.country !== f.country) return false;
  if (f.breakType !== "any" && s.break_type !== f.breakType) return false;
  const c = m?.conditions;
  if (f.minHeight !== "" && Number(f.minHeight) > 0) {
    if (!c) return false;
    const mid = (c.breaking_height_min_ft + c.breaking_height_max_ft) / 2;
    if (mid < fromUnits(Number(f.minHeight), units)) return false;
  }
  if (f.minQuality) {
    const min = QUALITY_OPTIONS.find((q) => q.value === f.minQuality)?.min ?? 0;
    if (!c || c.score < min) return false;
  }
  if (f.eventsOnly && !m?.event_at_time) return false;
  return true;
}

const FIELD = cn("h-9 rounded-lg border border-white/15 bg-white/5 px-2 text-sm text-white [color-scheme:dark]", FOCUS);

/** The spot filters (same controls and labels as before the map redesign). */
export function FilterControls({
  spots,
  filters,
  setFilters,
  units,
}: {
  spots: SpotSummary[];
  filters: Filters;
  setFilters: (f: Filters) => void;
  units: Units;
}) {
  const regions = useMemo(() => ["All", ...[...new Set(spots.map((s) => s.region_group))].sort()], [spots]);
  const countries = useMemo(
    () => ["All", ...[...new Set(spots.filter((s) => filters.region === "All" || s.region_group === filters.region).map((s) => s.country))].sort()],
    [spots, filters.region],
  );
  const breakTypes = useMemo(() => [...new Set(spots.map((s) => s.break_type))].sort(), [spots]);
  const set = (patch: Partial<Filters>) => setFilters({ ...filters, ...patch });
  return (
    <div className="space-y-3" data-testid="map-filters">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Region">
        {regions.map((r) => (
          <button
            key={r}
            type="button"
            onClick={() => set({ region: r, country: "All" })}
            aria-pressed={filters.region === r}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs transition",
              filters.region === r ? "border-[#c4ec4a] bg-[#c4ec4a] font-semibold text-[#101418]" : "border-white/15 text-white hover:border-white/40",
              FOCUS,
            )}
          >
            {r}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-2 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-white/70">Country</span>
          <select className={FIELD} value={filters.country} onChange={(e) => set({ country: e.target.value })} aria-label="Country">
            {countries.map((c) => (
              <option key={c} value={c}>
                {c === "All" ? "All countries" : c}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-white/70">Break type</span>
          <select className={FIELD} value={filters.breakType} onChange={(e) => set({ breakType: e.target.value })} aria-label="Break type">
            <option value="any">Any break</option>
            {breakTypes.map((b) => (
              <option key={b} value={b}>
                {titleCase(b)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-white/70">Min. breaking height ({units})</span>
          <input
            type="number"
            min={0}
            step={units === "m" ? 0.5 : 1}
            inputMode="decimal"
            className={FIELD}
            value={filters.minHeight}
            onChange={(e) => set({ minHeight: e.target.value })}
            aria-label="Minimum breaking wave height"
            placeholder="Any"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-white/70">Min. quality</span>
          <select className={FIELD} value={filters.minQuality} onChange={(e) => set({ minQuality: e.target.value })} aria-label="Minimum quality">
            <option value="">Any quality</option>
            {QUALITY_OPTIONS.map((q) => (
              <option key={q.value} value={q.value}>
                {q.label} ({q.min}+)
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={filters.eventsOnly}
            onChange={(e) => set({ eventsOnly: e.target.checked })}
            className="size-4 accent-[#c4ec4a] [color-scheme:dark]"
          />
          Swell event at this time
        </label>
        {isFiltered(filters) && (
          <button
            type="button"
            onClick={() => setFilters(NO_FILTERS)}
            className={cn("inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium text-white hover:bg-white/10", FOCUS)}
          >
            <X className="size-3.5" /> Clear filters
          </button>
        )}
      </div>
    </div>
  );
}
