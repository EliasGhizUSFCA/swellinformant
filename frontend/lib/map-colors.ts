/**
 * Colour scales for the map's ocean layers, in one place. The Wave Height layer has its own
 * ramp with anchors in metres (deep navy for calm seas → blue → violet → magenta → pink for
 * the largest waves, WAVE_HEIGHT_RAMP); the other layers share one ramp (deep blue for calm →
 * cyan → green → yellow → orange → red → magenta for the largest values, RAMP) placed over
 * their display range. Values beyond a range saturate at the end colours.
 *
 * The scales are a visual aid for reading model output, not categories with a physical
 * meaning (unlike the spot-marker quality colours in lib/quality.ts).
 */

export type LayerId = "wind" | "hs" | "energy" | "tp";

export interface ColorStop {
  /** Position on the scale, 0..1 (after the layer's transform). */
  at: number;
  color: string;
}

/** The shared ramp (Wave Period, Wind Speed, Wave Energy). */
export const RAMP: ColorStop[] = [
  { at: 0, color: "#1c2a6e" },
  { at: 0.08, color: "#22489c" },
  { at: 0.16, color: "#1f6fb9" },
  { at: 0.25, color: "#1897c6" },
  { at: 0.33, color: "#1db4b0" },
  { at: 0.42, color: "#3dc27b" },
  { at: 0.5, color: "#8ccc3d" },
  { at: 0.58, color: "#d4cf2f" },
  { at: 0.66, color: "#f0aa2b" },
  { at: 0.75, color: "#e8662d" },
  { at: 0.85, color: "#d03049" },
  { at: 0.93, color: "#b12b8b" },
  { at: 1, color: "#9a3dc2" },
];

/** A colour anchored at a value in the layer's base unit (metres for the wave height). */
export interface ValueAnchor {
  value: number;
  color: string;
}

/**
 * Wave-height ramp, anchors in metres of offshore significant wave height, interpolated
 * linearly in metres (in sRGB) between anchors and saturating at the end colours (above
 * 10 m: #f9d0e0). Our own ramp in the spirit of the owner's reference (a smooth blue →
 * violet → magenta/pink field): CIE L* (sRGB, D65) rises strictly from 10.4 at 0 m to 87.3
 * at 10 m and neighbouring anchors differ by ΔE*ab ≥ 5.4, so a larger sea is always lighter
 * and the scale still reads in grey (tests/map-wave-ramp.test.ts).
 */
export const WAVE_HEIGHT_RAMP: readonly ValueAnchor[] = [
  { value: 0, color: "#0c1846" },
  { value: 0.5, color: "#132e78" },
  { value: 1, color: "#1b48a6" },
  { value: 1.5, color: "#2a5cc2" },
  { value: 2, color: "#4363cf" },
  { value: 2.5, color: "#5a62d6" },
  { value: 3, color: "#7360d6" },
  { value: 4, color: "#925ad0" },
  { value: 5, color: "#ad55c4" },
  { value: 6, color: "#c556b4" },
  { value: 7, color: "#d860a6" },
  { value: 8, color: "#e6729f" },
  { value: 9, color: "#f19ebd" },
  { value: 10, color: "#f9d0e0" },
];

export function rgbToHex([r, g, b]: readonly [number, number, number]): string {
  return `#${[r, g, b].map((c) => Math.round(Math.min(255, Math.max(0, c))).toString(16).padStart(2, "0")).join("")}`;
}

/** Colour of `value` on value-anchored stops (linear in the value and in sRGB; saturating). */
export function colorAtValue(anchors: readonly ValueAnchor[], value: number): [number, number, number] {
  const first = anchors[0]!;
  const last = anchors[anchors.length - 1]!;
  if (!(value > first.value)) return hexToRgb(first.color);
  if (value >= last.value) return hexToRgb(last.color);
  let i = 0;
  while (i < anchors.length - 2 && value > anchors[i + 1]!.value) i++;
  const lo = anchors[i]!;
  const hi = anchors[i + 1]!;
  const f = (value - lo.value) / (hi.value - lo.value);
  const a = hexToRgb(lo.color);
  const b = hexToRgb(hi.color);
  return [Math.round(a[0] + (b[0] - a[0]) * f), Math.round(a[1] + (b[1] - a[1]) * f), Math.round(a[2] + (b[2] - a[2]) * f)];
}

/**
 * Colour stops (0..1 along a linear scale from `min` to `max`) for value-anchored colours:
 * the colour at `min`, every anchor strictly inside the range at its position, and the
 * colour at `max`. Over 0..10 m the wave-height stops are exactly its anchors; a wider range
 * (from the field index) keeps each anchor at its value and saturates beyond the last one.
 */
export function metresToStops(anchors: readonly ValueAnchor[], min: number, max: number): ColorStop[] {
  if (!(max > min)) return [{ at: 0, color: rgbToHex(colorAtValue(anchors, min)) }, { at: 1, color: rgbToHex(colorAtValue(anchors, min)) }];
  const at = (v: number) => (v - min) / (max - min);
  const inside = anchors.filter((a) => a.value > min && a.value < max).map((a) => ({ at: at(a.value), color: a.color }));
  return [{ at: 0, color: rgbToHex(colorAtValue(anchors, min)) }, ...inside, { at: 1, color: rgbToHex(colorAtValue(anchors, max)) }];
}

export interface LayerScale {
  /** Display range in the layer's base unit (m, s, m/s, kW/m). */
  min: number;
  max: number;
  /** "sqrt" spreads skewed quantities (wave power) so that common values are not all blue. */
  transform: "linear" | "sqrt";
  stops: ColorStop[];
}

/** Default display ranges (the field index may override hs and tp). */
export const LAYER_SCALES: Record<LayerId, LayerScale> = {
  hs: { min: 0, max: 10, transform: "linear", stops: metresToStops(WAVE_HEIGHT_RAMP, 0, 10) },
  tp: { min: 0, max: 20, transform: "linear", stops: RAMP },
  wind: { min: 0, max: 30, transform: "linear", stops: RAMP },
  energy: { min: 0, max: 150, transform: "sqrt", stops: RAMP },
};

/** 0..1 position of `value` on the scale (saturating; NaN stays NaN). */
export function scalePosition(scale: Pick<LayerScale, "min" | "max" | "transform">, value: number): number {
  if (value !== value) return NaN;
  const span = scale.max - scale.min;
  const t = span > 0 ? Math.min(1, Math.max(0, (value - scale.min) / span)) : 0;
  return scale.transform === "sqrt" ? Math.sqrt(t) : t;
}

export function hexToRgb(hex: string): [number, number, number] {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
  if (!m) throw new Error(`Not a #rrggbb colour: ${hex}`);
  return [parseInt(m[1]!, 16), parseInt(m[2]!, 16), parseInt(m[3]!, 16)];
}

/** Colour at position t (0..1) of a stop list, linear in RGB between stops. */
export function colorAt(stops: ColorStop[], t: number): [number, number, number] {
  const x = Math.min(1, Math.max(0, t));
  let lo = stops[0]!;
  let hi = stops[stops.length - 1]!;
  for (let i = 0; i < stops.length - 1; i++) {
    if (x >= stops[i]!.at && x <= stops[i + 1]!.at) {
      lo = stops[i]!;
      hi = stops[i + 1]!;
      break;
    }
  }
  const a = hexToRgb(lo.color);
  const b = hexToRgb(hi.color);
  const f = hi.at > lo.at ? (x - lo.at) / (hi.at - lo.at) : 0;
  return [Math.round(a[0] + (b[0] - a[0]) * f), Math.round(a[1] + (b[1] - a[1]) * f), Math.round(a[2] + (b[2] - a[2]) * f)];
}

export const LUT_SIZE = 256;

/** RGBA lookup table: entry i is the colour at position i / (size - 1). */
export function buildLut(stops: ColorStop[], size = LUT_SIZE): Uint8ClampedArray {
  const lut = new Uint8ClampedArray(size * 4);
  for (let i = 0; i < size; i++) {
    const [r, g, b] = colorAt(stops, i / (size - 1));
    lut[i * 4] = r;
    lut[i * 4 + 1] = g;
    lut[i * 4 + 2] = b;
    lut[i * 4 + 3] = 255;
  }
  return lut;
}

/** CSS linear-gradient for the legend bar, sampled from the same stops. */
export function cssGradient(stops: ColorStop[], direction = "to right"): string {
  return `linear-gradient(${direction}, ${stops.map((s) => `${s.color} ${Math.round(s.at * 1000) / 10}%`).join(", ")})`;
}
