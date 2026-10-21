/**
 * Wave physics and drawing styles for the map's crest-dash layer (components/map/wavefronts.ts).
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
 * Crest lines are lines of constant phase, so they move at c. The map draws short STYLISED
 * crest dashes from the forecast model's height, period and direction, not individual
 * observed waves: each dash lies across the direction of travel on a crest line of the
 * stated spacing and moves at the phase speed. The true spacing of 10 s crests (156 m) is far
 * below one pixel at a world view, so spacing is exaggerated by a stated factor and the
 * motion runs at a stated multiple of real time.
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

/**
 * Crest-dash size and opacity from significant height (Hs, m). The D11 proposal (6 + 3 Hs px
 * long, 1.0 + 0.3 Hs px wide, one halo) drew long hairlines against the owner's reference;
 * visual QA on the real 2026-10-16 fields settled on shorter, wider, softer dashes on a
 * denser grid (docs/map-rendering.md section 4.6). Length: 5 px + 1.5 px per metre, 6..14 px,
 * before the system's length factor.
 */
export const DASH_BASE_PX = 5;
export const DASH_PX_PER_M = 1.5;
export const DASH_MIN_PX = 6;
export const DASH_MAX_PX = 14;
/** Core width of a dash: 1.8 px + 0.3 px per metre, 1.8..3.4 px. */
export const DASH_WIDTH_BASE_PX = 1.8;
export const DASH_WIDTH_PX_PER_M = 0.3;
export const DASH_WIDTH_MAX_PX = 3.4;
/** Dash opacity: 0.40 + 0.09 per metre, 0.40..0.90 (D11's formula). */
export const DASH_ALPHA_BASE = 0.4;
export const DASH_ALPHA_PER_M = 0.09;
export const DASH_ALPHA_MAX = 0.9;
/**
 * The strokes of one dash, bottom to top, relative to its core width and opacity: a soft halo
 * (1.7 x the width at 0.45 x the opacity) under the core gives it a soft edge. Both with round
 * caps. (The trail behind the dash softens it further; an outer halo, 2.6 x at 0.22 x, was
 * dropped with the trails: the largest stroke of a dash, it cost most of the raster time.)
 */
export const DASH_PASSES: ReadonlyArray<{ readonly width: number; readonly alpha: number }> = [
  { width: 1.7, alpha: 0.45 },
  { width: 1, alpha: 1 },
];
/**
 * Motion trail of a dash: behind it (against its travel) the band its crest swept over in the
 * last TRAIL_SECONDS of animation, as wide along the crest as the dash with its round caps and
 * fading backwards in TRAIL_STEPS (the whole band, then its front half, each at 0.25 x the
 * dash's opacity: the front half shows at 1 - (1 - 0.25)^2 = 0.44 x, the back half at 0.25 x).
 * It is how the dash moved, a display choice that carries no wave data of its own. Length v x
 * TRAIL_SECONDS (7.5 px for a 10 s swell at the default spacing), at most TRAIL_MAX_SHARE of
 * the crest spacing (rows of dashes stay apart) and TRAIL_MAX_PX (zoomed in, crests move fast).
 */
export const TRAIL_SECONDS = 1;
export const TRAIL_MAX_SHARE = 0.5;
export const TRAIL_MAX_PX = 16;
export const TRAIL_STEPS: ReadonlyArray<{ readonly length: number; readonly alpha: number }> = [
  { length: 1, alpha: 0.25 },
  { length: 0.5, alpha: 0.25 },
];

/** Trail length, px: min(v x TRAIL_SECONDS, TRAIL_MAX_SHARE x s, TRAIL_MAX_PX); 0 without a speed. */
export function trailLength(speedPxPerS: number, spacingPx: number): number {
  if (!(speedPxPerS > 0) || !(spacingPx > 0)) return 0;
  return Math.min(speedPxPerS * TRAIL_SECONDS, TRAIL_MAX_SHARE * spacingPx, TRAIL_MAX_PX);
}

/** A dash lives this many crest passages (3 T / tau seconds: 7.5 s for a 10 s swell). */
export const DASH_LIFE_PASSAGES = 3;
/** Fade-in over the first 20 % of the life, fade-out over the last 30 % (smoothstep): a display choice. */
export const DASH_FADE_IN = 0.2;
export const DASH_FADE_OUT_FROM = 0.7;
/** Dash cells, px (one dash per cell of a grid fixed in world pixels: the same density at every zoom). */
export const DASH_CELL_PX = { primary: 18, combined: 18, secondary: 18, windsea: 20 } as const satisfies Record<string, number>;
/** Most dashes per frame: desktop, and phones (views narrower than PHONE_WIDTH_PX). */
export const MAX_DASHES = 4000;
export const PHONE_MAX_DASHES = 1500;
export const PHONE_WIDTH_PX = 640;
/**
 * Most vertices per frame: 2 for each dash piece and 2 for each trail step, so 6 for a solid
 * dash with its trail and 4 for a split one (no trail). A frame stops placing dashes when the
 * next one could pass it (the same even, hashed cut as the dash budget). 9,000, under D11's
 * 12,000: about 1,500 trailed dashes, which keeps a busy desktop frame (the whole world at
 * 1440 x 900) within D11's 5 ms draw p95 (docs/map-rendering.md sections 4.8 and 7).
 */
export const MAX_FRAME_VERTICES = 9_000;
/** Reduced motion: an arrowhead of ARROW_PX on every ARROW_EVERY-th primary or combined dash. */
export const ARROW_EVERY = 6;
export const ARROW_PX = 5;

const clamp = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));

/** Dash length, px: clamp(5 + 1.5 Hs, 6, 14) x the system's length factor (1, 1.4 secondary, 0.55 wind sea). */
export function dashLength(hsM: number, system: WaveSystem): number {
  return clamp(DASH_BASE_PX + DASH_PX_PER_M * hsM, DASH_MIN_PX, DASH_MAX_PX) * FAMILY_STYLES[system].lengthFactor;
}

/** Dash core width, px: clamp(1.8 + 0.3 Hs, 1.8, 3.4) x the system's width factor (0.8 secondary, 0.7 wind sea). */
export function dashWidth(hsM: number, system: WaveSystem = "primary"): number {
  return clamp(DASH_WIDTH_BASE_PX + DASH_WIDTH_PX_PER_M * hsM, DASH_WIDTH_BASE_PX, DASH_WIDTH_MAX_PX) * FAMILY_STYLES[system].widthFactor;
}

/** Dash opacity at full life: clamp(0.40 + 0.09 Hs, 0.40, 0.90) (D11). */
export function dashAlpha(hsM: number): number {
  return clamp(DASH_ALPHA_BASE + DASH_ALPHA_PER_M * hsM, DASH_ALPHA_BASE, DASH_ALPHA_MAX);
}

/** Life of one dash, s of animation: 3 crest passages, 3 T / tau (NaN without a period). */
export function dashLifeSeconds(periodS: number): number {
  return periodS > 0 ? (DASH_LIFE_PASSAGES * periodS) / TIME_SCALE : NaN;
}

/** 0 -> 0, 1 -> 1, smooth in between (clamped). */
export const smoothstep = (x: number): number => {
  const c = clamp(x, 0, 1);
  return c * c * (3 - 2 * c);
};

/** Opacity factor over a dash's life (age 0..1): smoothstep in over 0-20 %, out over 70-100 %. */
export function dashFade(age: number): number {
  return smoothstep(age / DASH_FADE_IN) * smoothstep((1 - age) / (1 - DASH_FADE_OUT_FROM));
}

/** Most dashes per frame for a view `widthPx` CSS px wide. */
export const maxDashes = (widthPx: number): number => (widthPx < PHONE_WIDTH_PX ? PHONE_MAX_DASHES : MAX_DASHES);

/** Whether a system of this height is drawn at all (missing or below MIN_DRAW_HS_M: no). */
export const drawnHeight = (hsM: number): boolean => Number.isFinite(hsM) && hsM >= MIN_DRAW_HS_M;

export interface FamilyStyle {
  label: string;
  /** Short label for compact keys. */
  short: string;
  /** Dash colour (core and halo). */
  color: string;
  /** Dash length relative to dashLength's base (the short wind-sea dashes: 0.55). */
  lengthFactor: number;
  /** Core width relative to dashWidth's base (thinner dashes for the secondary swell and the wind sea). */
  widthFactor: number;
  /** Gap in the middle of each dash, share of its length (0: one solid dash). */
  gap: number;
  /** Whether the dashes draw their motion trail (the secondary swell's split dashes do not, so that their shape stays distinct under the primary's). */
  trail: boolean;
  /** Dash cell size, px (DASH_CELL_PX). */
  cellPx: number;
  /** Short "how it looks" text for the legend key. */
  look: string;
}

/**
 * Per-system dash styles, told apart by SHAPE and colour (never colour alone): the primary
 * swell is a solid white dash with its trail, the secondary swell a long split dash (1.4 x the
 * length, a gap of 0.2 of it in the middle, 0.8 x the width) in warm amber without a trail, the
 * wind sea a short thin dash (0.55 x the length, 0.7 x the width) in pale cyan with its trail.
 * The split and short dashes stay at least
 * twice as long as wide, so their orientation still reads (D11 proposed 0.85 x and a 0.3 gap
 * for the secondary, whose halves came out as round dots at 1-2 m). The combined sea (the
 * default, and what is drawn when the run has no partitions) is a solid white dash too and is
 * never called "primary swell"; it is never drawn together with the partitions.
 */
export const FAMILY_STYLES: Record<WaveSystem, FamilyStyle> = {
  primary: { label: "Primary swell", short: "Primary", color: "#ffffff", lengthFactor: 1, widthFactor: 1, gap: 0, trail: true, cellPx: DASH_CELL_PX.primary, look: "white dashes" },
  secondary: { label: "Secondary swell", short: "Secondary", color: "#ffd59a", lengthFactor: 1.4, widthFactor: 0.8, gap: 0.2, trail: false, cellPx: DASH_CELL_PX.secondary, look: "split amber dashes" },
  windsea: { label: "Wind sea", short: "Wind sea", color: "#b8f0ff", lengthFactor: 0.55, widthFactor: 0.7, gap: 0, trail: true, cellPx: DASH_CELL_PX.windsea, look: "short cyan dashes" },
  combined: { label: "Combined sea (peak)", short: "Combined", color: "#ffffff", lengthFactor: 1, widthFactor: 1, gap: 0, trail: true, cellPx: DASH_CELL_PX.combined, look: "white dashes" },
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
  const spacing = e > 1 ? `Crest spacing ×${formatFactor(e)} at the map centre` : "Crest spacing to true scale at the map centre";
  return `${spacing}; time runs ${TIME_SCALE}× faster than real.`;
}

/** 4696 -> "4,700", 73.4 -> "73", 1.6 -> "1.6". */
export function formatFactor(x: number): string {
  if (!Number.isFinite(x)) return "—";
  if (x >= 1000) return (Math.round(x / 100) * 100).toLocaleString("en-US");
  if (x >= 10) return String(Math.round(x));
  return String(Math.round(x * 10) / 10);
}
