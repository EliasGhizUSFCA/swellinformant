/**
 * Display units. The API sends wind in km/h, breaking heights in feet and offshore heights
 * in metres; the UI converts here, never on the server. Exact factors: 1 kn = 1.852 km/h,
 * 1 mph = 1.609344 km/h, 1 m/s = 3.6 km/h, 1 ft = 0.3048 m.
 */

export type WindUnit = "km/h" | "kn" | "mph" | "m/s";
export type HeightUnit = "ft" | "m";

const KMH_PER_UNIT: Record<WindUnit, number> = { "km/h": 1, kn: 1.852, mph: 1.609344, "m/s": 3.6 };
const M_PER_FT = 0.3048;

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
