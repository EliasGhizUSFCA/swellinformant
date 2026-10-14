/**
 * Wave physics and drawing styles for the map's wavefront layer (components/map/wavefronts.ts).
 * Pure functions, no DOM; every constant is documented in docs/map-rendering.md.
 *
 * Conventions: wave directions are nautical degrees FROM (0 = from the north, clockwise), as
 * the API serves them. Waves travel TOWARD the opposite bearing: a swell from the SW (225)
 * travels to the NE (45). Screen vectors are (x east, y down). Web Mercator is conformal, so a
 * bearing keeps its angle on screen at every latitude: the screen propagation vector of a
 * bearing `a` (TOWARD) is (sin a, -cos a), with no per-latitude correction.
 *
 * Deep-water linear theory (omega^2 = g k tanh(k h) with tanh -> 1 for h > L / 2):
 *   wavelength L = g T^2 / (2 pi), phase speed c = L / T = g T / (2 pi), group speed cg = c / 2.
 * Crest lines are lines of constant phase, so they move at c; wave energy (the group, the
 * packet envelope) moves at cg. The map draws STYLISED crests from the forecast model's
 * height, period and direction, not individual observed waves: the true spacing of 10 s
 * crests (156 m) is far below one pixel at a world view, so spacing is exaggerated by a
 * stated factor and the motion runs at a stated multiple of real time.
 */

/** Standard gravity, m/s^2. */
export const G = 9.80665;

/** Screen pixels per metre of wavelength (the spacing scale S). */
export const SPACING_PX_PER_M = 0.12;
/** Crest spacing limits on screen, px (very short and very long periods stay readable). */
export const MIN_SPACING_PX = 8;
export const MAX_SPACING_PX = 64;
/** Upper limit for the true-scale spacing used when the map is zoomed in beyond the scale S. */
export const MAX_TRUE_SPACING_PX = 256;

/**
 * Time scale tau: the animation runs this many times faster than real time, in crest
 * passages. A crest passes a fixed point of the map once every T / tau seconds (2.5 s for a
 * 10 s swell), whatever the zoom and whether or not the spacing is clamped.
 */
export const TIME_SCALE = 4;

/** Equatorial circumference of the EPSG:3857 sphere, m (Leaflet's Web Mercator). */
export const EARTH_CIRCUMFERENCE_M = 40075016.686;

/** Systems lower than this (significant height, m) are not drawn. */
export const MIN_DRAW_HS_M = 0.2;

const RAD = Math.PI / 180;

/** The four wave systems the map can draw. "combined" = the peak of the total sea state. */
export type WaveSystem = "primary" | "secondary" | "windsea" | "combined";
export const WAVE_SYSTEMS: readonly WaveSystem[] = ["primary", "secondary", "windsea", "combined"];
/** The partition systems (swell1, swell2, wind sea of the wave model). */
export const PARTITION_SYSTEMS: readonly Exclude<WaveSystem, "combined">[] = ["primary", "secondary", "windsea"];

export const isWaveSystem = (v: unknown): v is WaveSystem => typeof v === "string" && (WAVE_SYSTEMS as readonly string[]).includes(v);

const norm360 = (deg: number) => ((deg % 360) + 360) % 360;

/** Bearing (degrees TOWARD, [0, 360)) of waves coming FROM `fromDeg`; NaN stays NaN. */
export function toward(fromDeg: number): number {
  return Number.isFinite(fromDeg) ? norm360(fromDeg + 180) : NaN;
}

/**
 * Unit screen vector (x east, y down) of the direction waves from `fromDeg` travel:
 * (sin a, -cos a) for a = toward(from), i.e. (-sin F, cos F). Null without a direction.
 */
export function propagation(fromDeg: number): [number, number] | null {
  if (!Number.isFinite(fromDeg)) return null;
  const f = fromDeg * RAD;
  return [-Math.sin(f) + 0, Math.cos(f) + 0];
}

/**
 * Unit screen vector along a crest line: perpendicular to `propagation` (a quarter turn
 * clockwise of it on screen). Waves from the west (270) have vertical crests (0, 1); from
 * the SW (225) the crests run NW-SE, along (0.707, 0.707).
 */
export function crestTangent(fromDeg: number): [number, number] | null {
  const p = propagation(fromDeg);
  return p ? [-p[1] + 0, p[0] + 0] : null;
}

/** Deep-water wavelength L = g T^2 / (2 pi), m (156.1 m at 10 s). */
export function wavelengthM(periodS: number): number {
  return (G * periodS * periodS) / (2 * Math.PI);
}

/** Deep-water phase speed c = g T / (2 pi), m/s: how fast crests move. */
export function phaseSpeed(periodS: number): number {
  return (G * periodS) / (2 * Math.PI);
}

/** Deep-water group speed cg = c / 2, m/s: how fast the wave energy (a packet) moves. */
export function groupSpeed(periodS: number): number {
  return phaseSpeed(periodS) / 2;
}

/** Ground metres per screen pixel at `zoom` and latitude (256 px Web Mercator tiles). */
export function metresPerPixel(zoom: number, latDeg: number): number {
  return (EARTH_CIRCUMFERENCE_M * Math.cos(latDeg * RAD)) / (256 * 2 ** zoom);
}

/**
 * Spacing exaggeration of the stylised crests at a zoom and latitude: drawn spacing over the
 * true wavelength at that map scale, S x (metres per pixel). 4,696x at zoom 2 on the equator,
 * 73.4x at zoom 8. At least 1: zoomed in beyond 1/S metres per pixel the map draws true scale.
 */
export function exaggeration(zoom: number, latDeg: number): number {
  return Math.max(1, SPACING_PX_PER_M * metresPerPixel(zoom, latDeg));
}

/**
 * Crest spacing on screen, px: S x L clamped to [8, 64] (12.0 px at 8 s, 18.7 at 10 s, 48.0
 * at 16 s). With `metresPerPx` given and the view zoomed in so far that the true wavelength
 * is wider than S x L, the true wavelength (up to MAX_TRUE_SPACING_PX) is drawn instead.
 */
export function crestSpacingPx(periodS: number, metresPerPx?: number): number {
  if (!(periodS > 0)) return NaN;
  const l = wavelengthM(periodS);
  if (metresPerPx != null && metresPerPx > 0 && SPACING_PX_PER_M * metresPerPx < 1) {
    return Math.min(MAX_TRUE_SPACING_PX, Math.max(MIN_SPACING_PX, l / metresPerPx));
  }
  return Math.min(MAX_SPACING_PX, Math.max(MIN_SPACING_PX, SPACING_PX_PER_M * l));
}

/**
 * Crest speed on screen, px per second of animation: spacing x tau / T. A crest then passes
 * any point once every T / tau seconds; unclamped this equals S x c x tau (7.5 px/s at 10 s).
 */
export function crestSpeedPxPerS(periodS: number, spacingPx: number): number {
  return periodS > 0 ? (spacingPx * TIME_SCALE) / periodS : NaN;
}

/** Packet (envelope) speed on screen: half the crest speed, as cg = c / 2 in deep water. */
export function envelopeSpeedPxPerS(periodS: number, spacingPx: number): number {
  return crestSpeedPxPerS(periodS, spacingPx) / 2;
}

/** Crest line width from significant height: 0.75 px + 0.55 px per metre, 0.75..3 px. */
export function lineWidth(hsM: number): number {
  return Math.min(3, Math.max(0.75, 0.75 + 0.55 * hsM));
}

/** Crest opacity from significant height: 0.30 + 0.12 per metre, 0.30..0.90. */
export function crestAlpha(hsM: number): number {
  return Math.min(0.9, Math.max(0.3, 0.3 + 0.12 * hsM));
}

/** Whether a system of this height is drawn at all (missing or below MIN_DRAW_HS_M: no). */
export const drawnHeight = (hsM: number): boolean => Number.isFinite(hsM) && hsM >= MIN_DRAW_HS_M;

export interface FamilyStyle {
  label: string;
  /** Short label for compact keys. */
  short: string;
  color: string;
  /** Canvas line dash in px ([] = solid). */
  dash: number[];
  /** Crest half-length along the crest line, in crest spacings. */
  halfLength: number;
  /** Crests in one packet (a "set"): the envelope is crests x spacing long. */
  crests: number;
  /** Packet cell size in crest spacings (one packet per cell of a jittered world-pixel grid). */
  cellSpacings: number;
  /** A soft glow along each crest and a faint dark trough line between crests (solid systems). */
  glow: boolean;
  /** Short "how it looks" text for the legend key. */
  look: string;
}

/**
 * Per-system styles, distinguished by dash pattern AND colour (never colour alone), plus
 * crest length: long solid lines for the primary swell, dashed amber for the secondary
 * swell, short dotted cyan for the wind sea. The combined sea (used when the model run has
 * no partitions) is solid white and is never called "primary swell".
 */
export const FAMILY_STYLES: Record<WaveSystem, FamilyStyle> = {
  primary: { label: "Primary swell", short: "Primary", color: "#f4f7fb", dash: [], halfLength: 1.8, crests: 6, cellSpacings: 6, glow: true, look: "solid" },
  secondary: { label: "Secondary swell", short: "Secondary", color: "#ffc46b", dash: [7, 5], halfLength: 1.5, crests: 5, cellSpacings: 5.5, glow: false, look: "dashed" },
  windsea: { label: "Wind sea", short: "Wind sea", color: "#7fe3ff", dash: [1.5, 3.5], halfLength: 1.4, crests: 4, cellSpacings: 4.5, glow: false, look: "dotted" },
  combined: { label: "Combined sea (peak)", short: "Combined", color: "#ffffff", dash: [], halfLength: 1.8, crests: 6, cellSpacings: 6, glow: true, look: "solid" },
};

/** Model variables of each system: significant height (m), period (s), direction FROM (deg). */
export const SYSTEM_VARIABLES = {
  primary: { hs: "swell1_hs", tp: "swell1_tp", dir: "swell1_dir" },
  secondary: { hs: "swell2_hs", tp: "swell2_tp", dir: "swell2_dir" },
  windsea: { hs: "windsea_hs", tp: "windsea_tp", dir: "windsea_dir" },
  combined: { hs: "hs", tp: "tp", dir: "wave_dir" },
} as const satisfies Record<WaveSystem, { hs: string; tp: string; dir: string }>;

/**
 * The systems to draw: the chosen ones the model run has, in drawing order (wind sea first,
 * primary on top). When none of the chosen systems exists (no partitions in this run) the
 * combined sea is used, if it exists. Missing systems are never invented.
 */
export function effectiveSystems(chosen: readonly WaveSystem[], available: ReadonlySet<WaveSystem>): WaveSystem[] {
  const order: WaveSystem[] = ["windsea", "secondary", "primary", "combined"];
  const picked = order.filter((s) => chosen.includes(s) && available.has(s));
  if (picked.length) return picked;
  return available.has("combined") ? ["combined"] : [];
}

/** Legend sentence for the stated scales at a zoom and latitude. */
export function scaleStatement(zoom: number, latDeg: number): string {
  const e = exaggeration(zoom, latDeg);
  const spacing = e > 1 ? `Crest spacing ×${formatFactor(e)} at this zoom` : "Crest spacing to true scale at this zoom";
  return `${spacing}; time runs ${TIME_SCALE}× faster than real.`;
}

/** 4696 -> "4,700", 73.4 -> "73", 1.6 -> "1.6". */
export function formatFactor(x: number): string {
  if (!Number.isFinite(x)) return "—";
  if (x >= 1000) return (Math.round(x / 100) * 100).toLocaleString("en-US");
  if (x >= 10) return String(Math.round(x));
  return String(Math.round(x * 10) / 10);
}
