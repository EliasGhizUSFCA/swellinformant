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

import { hexToRgb } from "@/lib/map-colors";

/** Standard gravity, m/s^2. */
const G = 9.80665;

/** Screen pixels per metre of wavelength (the spacing scale S). */
const SPACING_PX_PER_M = 0.12;
/** Crest spacing limits on screen, px (very short and very long periods stay readable). */
export const MIN_SPACING_PX = 8;
export const MAX_SPACING_PX = 64;
/** Upper limit for the true-scale spacing used when the map is zoomed in beyond the scale S. */
const MAX_TRUE_SPACING_PX = 256;

/**
 * Time scale tau: the animation runs this many times faster than real time, in crest
 * passages. A crest passes a fixed point of the map once every T / tau seconds (2.5 s for a
 * 10 s swell), whatever the zoom and whether or not the spacing is clamped.
 */
export const TIME_SCALE = 4;

/** Equatorial circumference of the EPSG:3857 sphere, m (Leaflet's Web Mercator). */
const EARTH_CIRCUMFERENCE_M = 40075016.686;

/** Systems lower than this (significant height, m) are not drawn. */
export const MIN_DRAW_HS_M = 0.2;

const RAD = Math.PI / 180;

/** The four wave systems the map can draw. "combined" = the peak of the total sea state. */
export type WaveSystem = "primary" | "secondary" | "windsea" | "combined";
export const WAVE_SYSTEMS: readonly WaveSystem[] = ["primary", "secondary", "windsea", "combined"];
/** The partition systems (swell1, swell2, wind sea of the wave model). */
export const PARTITION_SYSTEMS: readonly Exclude<WaveSystem, "combined">[] = ["primary", "secondary", "windsea"];

export const isWaveSystem = (v: unknown): v is WaveSystem => typeof v === "string" && (WAVE_SYSTEMS as readonly string[]).includes(v);

/** Deep-water wavelength L = g T^2 / (2 pi), m (156.1 m at 10 s). */
export function wavelengthM(periodS: number): number {
  return (G * periodS * periodS) / (2 * Math.PI);
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

/**
 * Crest-dash size and opacity from significant height (Hs, m). Each dash of the combined sea,
 * the primary swell and the wind sea is one soft BLOB: a short piece of a crest line (its
 * bright leading edge lies on the crest) smeared back along the direction it came from, like
 * a motion blur of the moving crest. Its extent along the crest is the dash length below; its
 * depth across the crest (along its travel) is blobDepth. As in the owner's reference (about
 * 5 px along the crest by 7 along the travel, measured), a blob of a typical sea is a short
 * crest piece streaking forward, deeper than it is long, not a tile lying across the swell.
 * The secondary swell keeps thin split bars, so its shape stays distinct
 * (docs/map-rendering.md section 4.6). Length along the crest: 4 px + 0.7 px per metre,
 * 4.5..7.2 px (5 px at 1.5 m, the reference's), before the system's length factor.
 */
const DASH_BASE_PX = 4;
const DASH_PX_PER_M = 0.7;
const DASH_MIN_PX = 4.5;
export const DASH_MAX_PX = 7.2;
/** Width of a bar (the secondary swell's split dashes): 1.8 px + 0.3 px per metre, 1.8..3.4 px, times its depth factor. */
const DASH_WIDTH_BASE_PX = 1.8;
const DASH_WIDTH_PX_PER_M = 0.3;
const DASH_WIDTH_MAX_PX = 3.4;
/**
 * Dash opacity: 0.40 + 0.14 per metre, 0.40..0.95. (D11's 0.40 + 0.09 Hs, 0.40..0.90, made
 * steeper at the final review with the smaller blobs, whose depth no longer grows with the
 * height, and the even tint below: a 4 m sea still draws more than twice the ink of a 1 m sea
 * on screen, D11's rule; e2e "higher waves draw more ink" measured 2.14 to 2.22 times. The
 * review's 0.45 + 0.13 Hs with blobs of 4.2 + 0.55 Hs px measured 1.97 and 2.02, on the edge.)
 */
const DASH_ALPHA_BASE = 0.4;
const DASH_ALPHA_PER_M = 0.14;
export const DASH_ALPHA_MAX = 0.95;
/**
 * Depth of a blob across its crest (along its travel), px: a bright head of BLOB_HEAD_PX plus
 * the band the crest swept over in the last SMEAR_SECONDS of animation (behind it, against its
 * travel), at most SMEAR_MAX_SHARE of the crest spacing (blobs of one crest never reach the
 * next) and BLOB_MAX_DEPTH_SHARE of the blob's length. The smear is speed x time, so it does
 * not grow with the height. 6.1 px for a 10 s sea, 6.9 px at 12 s, 8.6 px at 16 s (a 3 m
 * blob is 6.1 px long): at 1 to 3 m and 10 s or more a blob is at least as deep as it is
 * long, as the reference's (about 5 by 7 px, its smear on the long axis for about 80 % of its
 * blobs); the direction it travels reads from its crisp bright front and its fading back
 * (e2e).
 */
export const BLOB_HEAD_PX = 2.0;
export const SMEAR_SECONDS = 0.55;
export const SMEAR_MAX_SHARE = 0.5;
export const BLOB_MAX_DEPTH_SHARE = 1.6;
/**
 * Opacity of a blob across its depth (0 = the leading edge on the crest line, 1 = the back),
 * relative to the dash's opacity: a crisp, bright leading edge and a motion-blur falloff
 * towards the trailing side (drawn as a linear gradient, interpolated between these stops).
 */
export const BLOB_PROFILE: ReadonlyArray<{ readonly at: number; readonly alpha: number }> = [
  { at: 0, alpha: 0.8 },
  { at: 0.06, alpha: 1 },
  { at: 0.55, alpha: 0.9 },
  { at: 0.85, alpha: 0.5 },
  { at: 1, alpha: 0 },
];
/**
 * Dash tint: a lighter tint of the colour field under the dash, each channel lifted by
 * TINT_LIFT (of 255) but keeping at least TINT_HEADROOM of its distance to 255, so a dash over
 * a magenta sea is pale pink and over a blue sea lavender, never pure white. Measured on the
 * owner's reference: its blobs are the field colour plus about 70 to 90 per channel at near
 * full opacity, about equally in every channel (a lift, not a mix with white, which would grey
 * them). The lift is therefore about the same in every channel up to about 3 m (blue keeps at
 * least three quarters of red's and green's), so the hue of the sea is kept: TINT_LIFT 150
 * lifted red and green about twice as much as blue (whose room under 255 is smaller), which
 * turned the blobs grey-lavender. Drawn at the dash opacity, a head is lifted by about 51 per
 * channel at 1 m, 58 at 1.5 m, 71 at 2.5 m and 78 to 90 at 3 to 4 m (measured on a real
 * field at the final review: (58, 57, 51) over a 1.5 m sea, (76, 78, 65) over a 3 to 4 m
 * one): low seas are fainter than the reference's heads (D11's ink rule: a 4 m sea draws more
 * than twice the ink of a 1 m sea), 3 to 4 m heads match them. Partition dashes then mix in
 * their own colour by `tintMix` (amber secondary, cyan wind sea), so the systems stay apart by
 * colour as well as shape.
 */
export const TINT_LIFT = 95;
export const TINT_HEADROOM = 0.02;

/** Blob depth, px: min(BLOB_HEAD_PX + v x SMEAR_SECONDS, SMEAR_MAX_SHARE x s, BLOB_MAX_DEPTH_SHARE x length) x the system's depth factor. */
export function blobDepth(hsM: number, system: WaveSystem, speedPxPerS: number, spacingPx: number): number {
  const v = speedPxPerS > 0 ? speedPxPerS : 0;
  const s = spacingPx > 0 ? spacingPx : 0;
  const d = Math.min(BLOB_HEAD_PX + v * SMEAR_SECONDS, SMEAR_MAX_SHARE * s, BLOB_MAX_DEPTH_SHARE * dashLength(hsM, system));
  return Math.max(0, d) * FAMILY_STYLES[system].depthFactor;
}

/** The tint (r, g, b) of a dash of `system` over field colour `field` (null: no colour under it, the system's own colour). */
export function dashTint(field: readonly [number, number, number] | null, system: WaveSystem): [number, number, number] {
  const own = hexToRgb(FAMILY_STYLES[system].color);
  if (!field) return own;
  const m = FAMILY_STYLES[system].tintMix;
  return [0, 1, 2].map((c) => {
    const f = field[c]!;
    const lifted = Math.min(f + TINT_LIFT, 255 - TINT_HEADROOM * (255 - f));
    return Math.round(lifted + (own[c]! - lifted) * m);
  }) as [number, number, number];
}

/** A dash lives this many crest passages (3 T / tau seconds: 7.5 s for a 10 s swell). */
export const DASH_LIFE_PASSAGES = 3;
/** Fade-in over the first 20 % of the life, fade-out over the last 30 % (smoothstep): a display choice. */
const DASH_FADE_IN = 0.2;
const DASH_FADE_OUT_FROM = 0.7;
/** Dash cells, px (one dash per cell of a grid fixed in world pixels: the same density at every zoom). */
export const DASH_CELL_PX = { primary: 12, combined: 12, secondary: 13, windsea: 18 } as const satisfies Record<string, number>;
/** Most dashes per frame: desktop, and phones (views narrower than PHONE_WIDTH_PX). */
export const MAX_DASHES = 8000;
export const PHONE_MAX_DASHES = 3000;
const PHONE_WIDTH_PX = 640;
/**
 * Most filled quads per frame (one per dash: a blob, or a bar whose gap is in its fill). A
 * frame stops placing dashes when the next one could pass it (the same even, hashed cut as
 * the dash budget). Each quad is one fillRect under its own transform (no path is built or
 * stroked): docs/map-rendering.md sections 4.8 and 7 have the frame cost it was set for.
 */
export const MAX_FRAME_QUADS = 7_000;
/**
 * The quad budget adapts to the device (components/map/wavefronts.ts QuadBudget): while frames
 * take longer than DRAW_TARGET_MS to draw (the median of 20 frames, robust to a garbage
 * collection or a busy moment of the page), it drops in proportion to what they drew, never
 * below MIN_FRAME_QUADS; while frames it limits take under 80 % of it, it grows back by 15 %,
 * up to MAX_FRAME_QUADS. That keeps D11's frame draw p95 of 5 ms (about twice the median on
 * the software-rendered test machine, docs/map-rendering.md section 7) on slow machines and
 * the full density on fast ones.
 */
export const DRAW_TARGET_MS = 2.5;
export const MIN_FRAME_QUADS = 1_500;
/** Reduced motion: an arrowhead of ARROW_PX on every ARROW_EVERY-th primary or combined dash. */
export const ARROW_EVERY = 6;
export const ARROW_PX = 5;

const clamp = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));

/** Dash length along the crest, px: clamp(4 + 0.7 Hs, 4.5, 7.2) x the system's length factor (1; 2.5 secondary; 0.65 wind sea). */
export function dashLength(hsM: number, system: WaveSystem): number {
  return clamp(DASH_BASE_PX + DASH_PX_PER_M * hsM, DASH_MIN_PX, DASH_MAX_PX) * FAMILY_STYLES[system].lengthFactor;
}

/** Bar width, px (the secondary swell's split bars): clamp(1.8 + 0.3 Hs, 1.8, 3.4) x the system's depth factor (1). */
export function dashWidth(hsM: number, system: WaveSystem): number {
  return clamp(DASH_WIDTH_BASE_PX + DASH_WIDTH_PX_PER_M * hsM, DASH_WIDTH_BASE_PX, DASH_WIDTH_MAX_PX) * FAMILY_STYLES[system].depthFactor;
}

/** Dash opacity at full life: clamp(0.40 + 0.14 Hs, 0.40, 0.95). */
export function dashAlpha(hsM: number): number {
  return clamp(DASH_ALPHA_BASE + DASH_ALPHA_PER_M * hsM, DASH_ALPHA_BASE, DASH_ALPHA_MAX);
}

/** Life of one dash, s of animation: 3 crest passages, 3 T / tau (NaN without a period). */
export function dashLifeSeconds(periodS: number): number {
  return periodS > 0 ? (DASH_LIFE_PASSAGES * periodS) / TIME_SCALE : NaN;
}

/** 0 -> 0, 1 -> 1, smooth in between (clamped). */
const smoothstep = (x: number): number => {
  const c = clamp(x, 0, 1);
  return c * c * (3 - 2 * c);
};

/** Opacity factor over a dash's life (age 0..1): smoothstep in over 0-20 %, out over 70-100 %. */
export function dashFade(age: number): number {
  return smoothstep(age / DASH_FADE_IN) * smoothstep((1 - age) / (1 - DASH_FADE_OUT_FROM));
}

/** Most dashes per frame for a view `widthPx` CSS px wide. */
export const maxDashes = (widthPx: number): number => (widthPx < PHONE_WIDTH_PX ? PHONE_MAX_DASHES : MAX_DASHES);

export interface FamilyStyle {
  label: string;
  /** Short label for compact keys. */
  short: string;
  /** "blob": one soft smeared blob per dash; "bar": thin crisp bars (the secondary swell, split in two). */
  shape: "blob" | "bar";
  /** The system's own colour, mixed into the field tint by `tintMix` (0: the field tint alone). */
  color: string;
  tintMix: number;
  /** Dash length relative to dashLength's base (the short wind-sea dashes: 0.65). */
  lengthFactor: number;
  /** Blob depth relative to blobDepth's base, or bar width relative to dashWidth's base. */
  depthFactor: number;
  /** Gap in the middle of each dash, share of its length (0: one solid dash). */
  gap: number;
  /** Dash cell size, px (DASH_CELL_PX). */
  cellPx: number;
  /** Short "how it looks" text for the legend key. */
  look: string;
}

/**
 * Per-system dash styles, told apart by SHAPE and colour (never colour alone): the combined
 * sea and the primary swell are soft blobs in a light tint of the field under them, the
 * secondary swell long thin split bars (2.5 x the length, so as long as before the blobs were
 * made shorter; a gap of 0.2 of it in the middle) in an amber tint, the wind sea short blobs
 * (0.65 x the length, 0.8 x the depth) in a cyan tint. A blob's travel reads from its bright
 * front and fading back, a bar's from its length (each piece twice as long as wide). The
 * combined sea (the default, and what is drawn when the run has no partitions) is never
 * called "primary swell"; it is never drawn together with the partitions.
 */
export const FAMILY_STYLES: Record<WaveSystem, FamilyStyle> = {
  primary: { label: "Primary swell", short: "Primary", shape: "blob", color: "#ffffff", tintMix: 0, lengthFactor: 1, depthFactor: 1, gap: 0, cellPx: DASH_CELL_PX.primary, look: "light tinted blobs" },
  secondary: { label: "Secondary swell", short: "Secondary", shape: "bar", color: "#ffcf85", tintMix: 0.75, lengthFactor: 2.5, depthFactor: 1, gap: 0.2, cellPx: DASH_CELL_PX.secondary, look: "split amber bars" },
  windsea: { label: "Wind sea", short: "Wind sea", shape: "blob", color: "#9cecff", tintMix: 0.75, lengthFactor: 0.65, depthFactor: 0.8, gap: 0, cellPx: DASH_CELL_PX.windsea, look: "short cyan blobs" },
  combined: { label: "Combined sea (peak)", short: "Combined", shape: "blob", color: "#ffffff", tintMix: 0, lengthFactor: 1, depthFactor: 1, gap: 0, cellPx: DASH_CELL_PX.combined, look: "light tinted blobs" },
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
