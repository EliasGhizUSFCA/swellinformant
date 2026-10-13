/**
 * Map display preferences, kept per browser in localStorage. Every access is wrapped in
 * try/catch: storage can be unavailable (private mode, blocked cookies) or hold anything,
 * and the map must still work with the defaults. Spot filters are deliberately not stored:
 * they are per visit, so a returning user never sees a silently filtered map.
 */
import type { Units } from "@/lib/format";
import { isLayerId, WIND_UNITS, type LayerId, type WindUnit } from "@/lib/map-layers";

export type Basemap = "dark" | "streets";

export interface MapSettings {
  layer: LayerId;
  pressure: boolean;
  /** null = follow the account's height units (or ft when signed out). */
  heightUnits: Units | null;
  windUnits: WindUnit;
  particles: boolean;
  basemap: Basemap;
}

export const DEFAULT_SETTINGS: MapSettings = {
  layer: "hs",
  pressure: false,
  heightUnits: null,
  windUnits: "km/h",
  particles: true,
  basemap: "dark",
};

export const SETTINGS_KEY = "swell.map.settings.v1";

/** Keeps only valid fields from stored JSON; anything else falls back to the default. */
export function parseSettings(raw: string | null | undefined): MapSettings {
  if (!raw) return DEFAULT_SETTINGS;
  let data: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return DEFAULT_SETTINGS;
    data = parsed as Record<string, unknown>;
  } catch {
    return DEFAULT_SETTINGS;
  }
  return {
    layer: isLayerId(data.layer) ? data.layer : DEFAULT_SETTINGS.layer,
    pressure: typeof data.pressure === "boolean" ? data.pressure : DEFAULT_SETTINGS.pressure,
    heightUnits: data.heightUnits === "ft" || data.heightUnits === "m" ? data.heightUnits : null,
    windUnits: WIND_UNITS.includes(data.windUnits as WindUnit) ? (data.windUnits as WindUnit) : DEFAULT_SETTINGS.windUnits,
    particles: typeof data.particles === "boolean" ? data.particles : DEFAULT_SETTINGS.particles,
    basemap: data.basemap === "streets" || data.basemap === "dark" ? data.basemap : DEFAULT_SETTINGS.basemap,
  };
}

export function readSettings(): MapSettings {
  try {
    return parseSettings(window.localStorage.getItem(SETTINGS_KEY));
  } catch {
    return DEFAULT_SETTINGS;
  }
}

export function writeSettings(s: MapSettings): void {
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  } catch {
    // Storage unavailable or full: the setting still applies for this visit.
  }
}
