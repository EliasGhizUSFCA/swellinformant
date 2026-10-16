/**
 * Map display preferences, kept per browser in localStorage. Every access is wrapped in
 * try/catch: storage can be unavailable (private mode, blocked cookies) or hold anything,
 * and the map must still work with the defaults. Spot filters are deliberately not stored:
 * they are per visit, so a returning user never sees a silently filtered map.
 *
 * The wind unit is one preference for the whole site: it is stored under its own key,
 * `swell.windUnit` (the spot charts read and write the same key), and mirrored in the map's
 * settings object only for older pages that read it there.
 */
import type { Units } from "@/lib/format";
import { isLayerId, WIND_UNITS, type LayerId, type WindUnit } from "@/lib/map-layers";
import { isWaveSystem, type WaveSystem } from "@/lib/map-waves";

export type { WaveSystem } from "@/lib/map-waves";
export type Basemap = "dark" | "streets";

export interface MapSettings {
  layer: LayerId;
  pressure: boolean;
  /** null = follow the account's height units (or ft when signed out). */
  heightUnits: Units | null;
  windUnits: WindUnit;
  /** Animated flow: wind particles and wave crests (still arrows / still crests with reduced motion). */
  particles: boolean;
  basemap: Basemap;
  /** Wind particles over the wave layers too (the Wind layer always has them). */
  windOverlay: boolean;
  /**
   * Wave systems drawn as crests on the wave layers. Systems the model run does not have are
   * skipped; when none of them exists the combined sea is drawn instead (lib/map-waves
   * effectiveSystems), so the stored choice survives a run without partitions.
   */
  waveSystems: WaveSystem[];
}

export const DEFAULT_WAVE_SYSTEMS: WaveSystem[] = ["primary", "secondary"];

export const DEFAULT_SETTINGS: MapSettings = {
  layer: "hs",
  pressure: false,
  heightUnits: null,
  windUnits: "km/h",
  particles: true,
  basemap: "dark",
  windOverlay: false,
  waveSystems: DEFAULT_WAVE_SYSTEMS,
};

export const SETTINGS_KEY = "swell.map.settings.v1";
/** The site-wide wind unit (a bare unit string, e.g. "mph"). */
export const WIND_UNIT_KEY = "swell.windUnit";
/** Event the spot charts dispatch on window after writing WIND_UNIT_KEY in this tab. */
export const WIND_UNIT_EVENT = "swell:wind-unit";

const isWindUnit = (v: unknown): v is WindUnit => WIND_UNITS.includes(v as WindUnit);

/** A stored wave-system list: known values only, no duplicates, "combined" alone; else the default. */
export function parseWaveSystems(raw: unknown): WaveSystem[] {
  if (!Array.isArray(raw)) return DEFAULT_WAVE_SYSTEMS;
  const known = [...new Set(raw.filter(isWaveSystem))];
  if (!known.length) return DEFAULT_WAVE_SYSTEMS;
  return known.includes("combined") && known.length > 1 ? known.filter((s) => s !== "combined") : known;
}

/**
 * Keeps only valid fields from stored JSON; anything else falls back to the default.
 * `sharedWindUnit` (the WIND_UNIT_KEY value) wins over the map's own copy.
 */
export function parseSettings(raw: string | null | undefined, sharedWindUnit?: string | null): MapSettings {
  let data: Record<string, unknown> = {};
  if (raw) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) data = parsed as Record<string, unknown>;
    } catch {
      data = {};
    }
  }
  const windUnits = isWindUnit(sharedWindUnit) ? sharedWindUnit : isWindUnit(data.windUnits) ? data.windUnits : DEFAULT_SETTINGS.windUnits;
  return {
    layer: isLayerId(data.layer) ? data.layer : DEFAULT_SETTINGS.layer,
    pressure: typeof data.pressure === "boolean" ? data.pressure : DEFAULT_SETTINGS.pressure,
    heightUnits: data.heightUnits === "ft" || data.heightUnits === "m" ? data.heightUnits : null,
    windUnits,
    particles: typeof data.particles === "boolean" ? data.particles : DEFAULT_SETTINGS.particles,
    basemap: data.basemap === "streets" || data.basemap === "dark" ? data.basemap : DEFAULT_SETTINGS.basemap,
    windOverlay: typeof data.windOverlay === "boolean" ? data.windOverlay : DEFAULT_SETTINGS.windOverlay,
    waveSystems: data.waveSystems === undefined ? DEFAULT_WAVE_SYSTEMS : parseWaveSystems(data.waveSystems),
  };
}

export function readSettings(): MapSettings {
  let raw: string | null = null;
  let unit: string | null = null;
  try {
    raw = window.localStorage.getItem(SETTINGS_KEY);
  } catch {
    raw = null;
  }
  try {
    unit = window.localStorage.getItem(WIND_UNIT_KEY);
  } catch {
    unit = null;
  }
  return parseSettings(raw, unit);
}

export function writeSettings(s: MapSettings): void {
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  } catch {
    // Storage unavailable or full: the setting still applies for this visit.
  }
  try {
    if (window.localStorage.getItem(WIND_UNIT_KEY) !== s.windUnits) {
      window.localStorage.setItem(WIND_UNIT_KEY, s.windUnits);
      window.dispatchEvent(new Event(WIND_UNIT_EVENT));
    }
  } catch {
    // As above: the unit applies to this page only.
  }
}
