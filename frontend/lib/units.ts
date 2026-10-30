/**
 * Display units. The API sends wind in km/h, breaking heights in feet and offshore heights
 * in metres; the UI converts here, never on the server. Exact factors: 1 kn = 1.852 km/h,
 * 1 mph = 1.609344 km/h, 1 m/s = 3.6 km/h, 1 ft = 0.3048 m.
 *
 * Also the site-wide wind-unit and height-unit preferences (the spot charts and the map
 * settings), kept in the browser under WIND_UNIT_KEY and HEIGHT_UNIT_KEY, so they work for
 * visitors who are not signed in.
 */
import { useSyncExternalStore } from "react";

export type WindUnit = "km/h" | "kn" | "mph" | "m/s";
export type HeightUnit = "ft" | "m";

const KMH_PER_UNIT: Record<WindUnit, number> = { "km/h": 1, kn: 1.852, mph: 1.609344, "m/s": 3.6 };
export const M_PER_FT = 0.3048;

/** A wind speed in km/h, in `unit`. */
export function convertWind(kmh: number, unit: WindUnit): number {
  return kmh / KMH_PER_UNIT[unit];
}

/** "14 km/h", "8 kn", "9 mph", "3.9 m/s" (or "—" without a value). */
export function formatWind(kmh: number | null | undefined, unit: WindUnit, digits?: number): string {
  if (kmh == null || !Number.isFinite(kmh)) return "—";
  const places = digits ?? (unit === "m/s" ? 1 : 0);
  return `${convertWind(kmh, unit).toFixed(places)} ${unit}`;
}

/** A height in feet, in `unit`. */
export function convertHeightFt(ft: number, unit: HeightUnit): number {
  return unit === "m" ? ft * M_PER_FT : ft;
}

const trim = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1).replace(/\.0$/, ""));

/** "5–7 ft" or "1.5–2.1 m" from a range in feet (to 0.5 ft or 0.1 m, as lib/format's
 * heightRange); one value when both ends round alike. */
export function formatHeightRange(minFt: number, maxFt: number, unit: HeightUnit): string {
  const round = (ft: number) => (unit === "m" ? Math.round(convertHeightFt(ft, "m") * 10) / 10 : Math.round(ft * 2) / 2);
  const lo = trim(round(minFt));
  const hi = trim(round(maxFt));
  return lo === hi ? `${lo} ${unit}` : `${lo}–${hi} ${unit}`;
}

// --- the site-wide wind-unit preference ---------------------------------------------------

/** The wind units offered on the spot charts and in the map settings, in this order. */
export const WIND_UNITS: readonly WindUnit[] = ["km/h", "mph", "kn", "m/s"];
export const DEFAULT_WIND_UNIT: WindUnit = "km/h";
/** localStorage key of the preference: a bare unit string, e.g. "mph". */
export const WIND_UNIT_KEY = "swell.windUnit";
/** Dispatched on window when the preference changes in this tab (a storage event reaches only
 * the other tabs). */
export const WIND_UNIT_EVENT = "swell:wind-unit";

export function isWindUnit(v: unknown): v is WindUnit {
  return typeof v === "string" && (WIND_UNITS as readonly string[]).includes(v);
}

// The choice for this page when storage refuses the write (private mode, blocked storage).
let memoryUnit: WindUnit | null = null;

/** The chosen wind unit, or null when none (or an unknown value) is stored. Never throws. */
export function storedWindUnit(): WindUnit | null {
  if (memoryUnit) return memoryUnit;
  try {
    const v = window.localStorage.getItem(WIND_UNIT_KEY);
    return isWindUnit(v) ? v : null;
  } catch {
    return null;
  }
}

/** The wind unit to display (the default when nothing valid is stored). */
export function readWindUnit(): WindUnit {
  return storedWindUnit() ?? DEFAULT_WIND_UNIT;
}

/** Stores the preference and, when it changed, tells this page (WIND_UNIT_EVENT). With storage
 * unavailable the choice lasts for this page only. */
export function writeWindUnit(unit: WindUnit): void {
  const before = readWindUnit();
  try {
    window.localStorage.setItem(WIND_UNIT_KEY, unit);
    memoryUnit = null;
  } catch {
    memoryUnit = unit;
  }
  if (before !== unit) window.dispatchEvent(new Event(WIND_UNIT_EVENT));
}

/** Calls `cb` when the preference changes in this tab or another one. */
function subscribeWindUnit(cb: () => void): () => void {
  const onStorage = (e: StorageEvent) => {
    if (e.key === WIND_UNIT_KEY || e.key === null) cb();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener(WIND_UNIT_EVENT, cb);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener(WIND_UNIT_EVENT, cb);
  };
}

/** [unit, setUnit]: the shared preference (server render: the default). */
export function useWindUnit(): [WindUnit, (u: WindUnit) => void] {
  const unit = useSyncExternalStore(subscribeWindUnit, readWindUnit, () => DEFAULT_WIND_UNIT);
  return [unit, writeWindUnit];
}

// --- the site-wide height-unit preference -------------------------------------------------
//
// Feet or metres for every surf and swell height (spot charts, spot summary, map). The choice
// made on this browser wins; without one the account's units apply (signed in), else feet.
// Stored like the wind unit, so it works for visitors who are not signed in.

export const HEIGHT_UNITS: readonly HeightUnit[] = ["ft", "m"];
/** localStorage key of the height preference: "ft" or "m". */
export const HEIGHT_UNIT_KEY = "swell.heightUnit";
/** Dispatched on window when the height preference changes in this tab. */
export const HEIGHT_UNIT_EVENT = "swell:height-unit";

export function isHeightUnit(v: unknown): v is HeightUnit {
  return v === "ft" || v === "m";
}

// The choice for this page when storage refuses the write (private mode, blocked storage).
let memoryHeightUnit: HeightUnit | null = null;

/** The height unit chosen on this browser, or null when none (or an unknown value) is
 * stored. Never throws. */
export function storedHeightUnit(): HeightUnit | null {
  if (memoryHeightUnit) return memoryHeightUnit;
  try {
    const v = window.localStorage.getItem(HEIGHT_UNIT_KEY);
    return isHeightUnit(v) ? v : null;
  } catch {
    return null;
  }
}

/** Stores the height preference and, when it changed, tells this page (HEIGHT_UNIT_EVENT).
 * With storage unavailable the choice lasts for this page only. */
export function writeHeightUnit(unit: HeightUnit): void {
  const before = storedHeightUnit();
  try {
    window.localStorage.setItem(HEIGHT_UNIT_KEY, unit);
    memoryHeightUnit = null;
  } catch {
    memoryHeightUnit = unit;
  }
  if (before !== unit) window.dispatchEvent(new Event(HEIGHT_UNIT_EVENT));
}

function subscribeHeightUnit(cb: () => void): () => void {
  const onStorage = (e: StorageEvent) => {
    if (e.key === HEIGHT_UNIT_KEY || e.key === null) cb();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener(HEIGHT_UNIT_EVENT, cb);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener(HEIGHT_UNIT_EVENT, cb);
  };
}

/**
 * [unit, setUnit]: the height unit to display. The browser's stored choice wins; otherwise
 * `fallback` (pass the signed-in account's units; feet when signed out). Server render: the
 * fallback.
 */
export function useHeightUnit(fallback: HeightUnit = "ft"): [HeightUnit, (u: HeightUnit) => void] {
  const stored = useSyncExternalStore(subscribeHeightUnit, storedHeightUnit, () => null);
  return [stored ?? fallback, writeHeightUnit];
}

/** Test hook: forget the in-memory fallback choice. */
export function resetHeightUnitMemory(): void {
  memoryHeightUnit = null;
}

/**
 * The scoring engine's explanation sentence (the backend writes offshore heights in metres,
 * faces in feet, wind in km/h and British spelling) in the viewer's units and the UI's
 * spelling, so one screen never mixes "7.7 ft" with "2.4 m" or "11 mph" with "17 km/h":
 * "2.4 m @ 13 s" -> "7.9 ft @ 13 s", "17 km/h" -> "11 mph", "4–6 ft faces" -> "1.2–1.8 m
 * faces", "favourable" -> "favorable". Only these patterns of our own sentence change.
 */
export function explanationText(text: string, height: HeightUnit, wind: WindUnit): string {
  let out = text;
  if (height === "ft") out = out.replace(/(\d+(?:\.\d+)?) m(?= @)/g, (_, m: string) => `${(Number(m) / M_PER_FT).toFixed(1)} ft`);
  else
    out = out.replace(/(\d+(?:\.\d+)?)–(\d+(?:\.\d+)?) ft\b/g, (_, a: string, b: string) => `${(Number(a) * M_PER_FT).toFixed(1)}–${(Number(b) * M_PER_FT).toFixed(1)} m`);
  if (wind !== "km/h") out = out.replace(/(\d+(?:\.\d+)?) km\/h/g, (_, v: string) => formatWind(Number(v), wind));
  return out.replace(/\b([Uu]n)?([Ff])avourable\b/g, (_, un: string | undefined, f: string) => `${un ?? ""}${f}avorable`);
}
