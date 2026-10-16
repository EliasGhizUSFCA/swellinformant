/**
 * The map's ocean layers: which model fields each needs, how values convert to the user's
 * units, legend ticks and the hover readout text.
 *
 * Every number shown here is OFFSHORE model output (open-ocean sea state and wind). Surf
 * (breaking) height at a beach is a different quantity, predicted per spot elsewhere.
 */
import { compass, type Units } from "@/lib/format";
import { LAYER_SCALES, scalePosition, type LayerId, type LayerScale } from "@/lib/map-colors";
import type { FieldIndex, FieldVariableName } from "@/lib/map-fields";
import { FAMILY_STYLES, type WaveSystem } from "@/lib/map-waves";
import { convertWind, type WindUnit } from "@/lib/units";

/** The wind units offered on the map: the one list, shared with the spot charts (lib/units). */
export { WIND_UNITS } from "@/lib/units";

export type { LayerId } from "@/lib/map-colors";
export type { WindUnit } from "@/lib/units";

const KMH_PER_MPS = 3.6;
const FT_PER_M = 3.28084;

export interface LayerDef {
  id: LayerId;
  label: string;
  /** What the colour layer shows, for screen readers ("Offshore significant wave height"). */
  longName: string;
  /** Legend title above the colour scale; says "offshore" wherever the numbers are sea state. */
  scaleTitle: string;
  /** Fields to download for this layer at one time step. */
  needs: FieldVariableName[];
  /** Plain sentence for the legend. */
  about: string;
}

export const LAYERS: LayerDef[] = [
  {
    id: "wind",
    label: "Wind Speed",
    longName: "Wind speed",
    scaleTitle: "Wind speed",
    needs: ["wind_u", "wind_v"],
    about: "10 m wind speed from the weather model. Particles drift with the wind; the readout says where the wind blows from.",
  },
  {
    id: "hs",
    label: "Wave Height",
    longName: "Offshore significant wave height",
    scaleTitle: "Offshore wave height, Hs",
    needs: ["hs", "tp", "wave_dir"],
    about:
      "Offshore significant wave height (swell and wind sea combined) from the wave model. Surf height at a beach is different; tap a spot for our surf prediction.",
  },
  {
    id: "energy",
    label: "Wave Energy",
    longName: "Offshore wave power",
    scaleTitle: "Offshore wave power",
    needs: ["hs", "tp", "wave_dir"],
    about:
      "Offshore wave power per metre of wave crest, estimated as P ≈ 0.49 × Hs² × Te kW/m with the energy period Te ≈ 0.9 × peak period (deep-water approximation). It is open-ocean model energy, not surf at a beach.",
  },
  {
    id: "tp",
    label: "Wave Period",
    longName: "Offshore peak wave period",
    scaleTitle: "Offshore peak period",
    needs: ["tp", "hs", "wave_dir"],
    about:
      "Offshore peak wave period: the period of the most energetic waves. Longer-period swell travels further and usually breaks with more power.",
  },
];

export const layerDef = (id: LayerId): LayerDef => LAYERS.find((l) => l.id === id) ?? LAYERS[1]!;

export const isLayerId = (v: unknown): v is LayerId => typeof v === "string" && LAYERS.some((l) => l.id === v);

/** The layer's scale, with the display range from the field index where it provides one. */
export function layerScale(id: LayerId, index?: FieldIndex | null): LayerScale {
  const base = LAYER_SCALES[id];
  const name = id === "hs" ? "hs" : id === "tp" ? "tp" : null;
  const v = name ? index?.variables?.find((x) => x.name === name) : undefined;
  if (v && Number.isFinite(v.display_min) && Number.isFinite(v.display_max) && v.display_max > v.display_min) {
    return { ...base, min: v.display_min, max: v.display_max };
  }
  return base;
}

export interface DisplayUnits {
  height: Units;
  wind: WindUnit;
}

/** Unit label for a layer in the user's units. */
export function layerUnit(id: LayerId, u: DisplayUnits): string {
  if (id === "hs") return u.height;
  if (id === "wind") return u.wind;
  if (id === "tp") return "s";
  return "kW/m";
}

/** Base value (m, m/s, s, kW/m) → the user's units. */
export function toDisplay(id: LayerId, value: number, u: DisplayUnits): number {
  if (id === "hs") return u.height === "ft" ? value * FT_PER_M : value;
  if (id === "wind") return convertWind(value * KMH_PER_MPS, u.wind);
  return value;
}

/** The user's units → base value. */
export function fromDisplay(id: LayerId, value: number, u: DisplayUnits): number {
  if (id === "hs") return u.height === "ft" ? value / FT_PER_M : value;
  if (id === "wind") return value / convertWind(KMH_PER_MPS, u.wind);
  return value;
}

/** "2.4 m", "8 ft", "36 km/h", "22 mph", "3.9 m/s", "12 s", "45 kW/m" (or "—"). */
export function formatLayerValue(id: LayerId, value: number, u: DisplayUnits): string {
  if (!Number.isFinite(value)) return "—";
  const v = toDisplay(id, value, u);
  const unit = layerUnit(id, u);
  if (id === "hs") return `${u.height === "m" || v < 10 ? v.toFixed(1) : Math.round(v)} ${unit}`;
  if (id === "energy") return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${unit}`;
  if (id === "wind" && u.wind === "m/s") return `${v.toFixed(1)} ${unit}`;
  return `${Math.round(v)} ${unit}`;
}

function niceStep(span: number, maxTicks: number): number {
  for (const s of [0.5, 1, 2, 2.5, 4, 5, 10, 20, 25, 50, 100]) if (span / s <= maxTicks - 1) return s;
  return Math.ceil(span / (maxTicks - 1));
}

export interface LegendTick {
  label: string;
  /** 0..1 along the colour bar. */
  pos: number;
}

/** Legend ticks in the user's units, positioned on the (possibly non-linear) scale. */
export function legendTicks(id: LayerId, scale: LayerScale, u: DisplayUnits, maxTicks = 7): LegendTick[] {
  if (id === "energy") {
    return [0, 10, 25, 50, 100, 150]
      .filter((v) => v >= scale.min && v <= scale.max)
      .map((v) => ({ label: String(v), pos: scalePosition(scale, v) }));
  }
  const lo = toDisplay(id, scale.min, u);
  const hi = toDisplay(id, scale.max, u);
  const step = niceStep(hi - lo, maxTicks);
  const out: LegendTick[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
    out.push({ label: String(Math.round(v * 10) / 10), pos: scalePosition(scale, fromDisplay(id, v, u)) });
  }
  return out;
}

/** One wave system under the cursor (a partition of the wave model, base units). */
export interface PartitionSample {
  system: Exclude<WaveSystem, "combined">;
  hs: number;
  tp: number;
  dir: number;
}

/** Values under the cursor (base units; NaN where the model has no data). */
export interface PointSample {
  hs?: number;
  tp?: number;
  waveDir?: number;
  energy?: number;
  windSpeed?: number;
  windDir?: number;
  pressure?: number;
  /** Wave partitions on screen at this point, primary first (only systems with data). */
  partitions?: PartitionSample[];
}

const fromText = (deg: number | undefined) => (deg != null && Number.isFinite(deg) ? `from ${compass(deg)}` : null);

/** "Primary 1.9 m 14 s from S (195°)" (null when the system has no height here). */
export function partitionText(p: PartitionSample, u: DisplayUnits): string | null {
  if (!Number.isFinite(p.hs)) return null;
  const parts = [FAMILY_STYLES[p.system].short, formatLayerValue("hs", p.hs, u)];
  if (Number.isFinite(p.tp)) parts.push(formatLayerValue("tp", p.tp, u));
  if (Number.isFinite(p.dir)) parts.push(`from ${compass(p.dir)} (${Math.round(p.dir) % 360}°)`);
  return parts.join(" ");
}

/**
 * Readout for the active layer, e.g. title "Offshore waves (Hs)" and text
 * "2.4 m · 12 s · from WSW". Null when the main value is missing (land, no data).
 */
export function readout(id: LayerId, s: PointSample, u: DisplayUnits): { title: string; text: string } | null {
  const parts: Array<string | null> = [];
  let title: string;
  const ok = (v: number | undefined): v is number => v != null && Number.isFinite(v);
  if (id === "wind") {
    if (!ok(s.windSpeed)) return null;
    title = "Wind";
    parts.push(formatLayerValue("wind", s.windSpeed, u), fromText(s.windDir));
  } else if (id === "hs") {
    if (!ok(s.hs)) return null;
    title = "Offshore waves (Hs)";
    parts.push(formatLayerValue("hs", s.hs, u), ok(s.tp) ? formatLayerValue("tp", s.tp, u) : null, fromText(s.waveDir));
  } else if (id === "tp") {
    if (!ok(s.tp)) return null;
    title = "Offshore peak period";
    parts.push(formatLayerValue("tp", s.tp, u), ok(s.hs) ? `Hs ${formatLayerValue("hs", s.hs, u)}` : null, fromText(s.waveDir));
  } else {
    if (!ok(s.energy)) return null;
    title = "Offshore wave power";
    parts.push(formatLayerValue("energy", s.energy, u), fromText(s.waveDir));
  }
  if (id !== "wind") for (const p of s.partitions ?? []) parts.push(partitionText(p, u));
  if (ok(s.pressure)) parts.push(`${Math.round(s.pressure)} hPa`);
  return { title, text: parts.filter(Boolean).join(" · ") };
}
