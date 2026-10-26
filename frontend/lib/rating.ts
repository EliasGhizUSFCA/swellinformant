/**
 * Surf-quality rating text: the "Excellent Surf — 8.9/10" headline and a one-line reason.
 *
 * The 0–100 score comes from backend/app/services/surf_quality/scoring.py; the headline shows
 * it divided by ten with one decimal. The reason is built only from factors present in the
 * API response (swell period, direction fit, wind relation and speed, size against the
 * spot's working range, tide), so nothing is invented: a factor without data is left out,
 * and with no data at all there is no reason line.
 *
 * Thresholds (assumptions of this wording, not of the score):
 * * period: ≥ 13 s "long-period groundswell", ≥ 10 s "mid-period swell", else "short-period";
 * * wind strength: the backend's own class (`wind_strength`, from the prediction's stored
 *   wind_detail: glassy, light, moderate or strong). Predictions made before it existed are
 *   classed from the speed with the backend's documented Beaufort bounds (glassy ≤ 5 km/h,
 *   light < 12, moderate < 29, strong from 29; docs/SURF_SCORING.md), never with other
 *   cut-offs. "moderate" adds no word to the reason;
 * * fit components (0–1): ≥ 0.75 favourable, ≥ 0.4 workable, else limiting — the same cut-offs
 *   the backend uses for its explanation text. Without a wind fit, the backend's
 *   `wind_favourability` gives the wind's tone.
 *
 * A factor's tone (favourable / workable / limiting) is only ever the backend's own judgement:
 * its component fit, or the cut-off words of its explanation text. The period and wind
 * phrases above describe the conditions; whether they helped or held back the score depends on
 * the spot (its own period range and offshore direction), so without the API's fit the tone
 * stays null rather than guessed.
 */
import { asQuality, QUALITY_LABELS } from "@/lib/quality";

export type FactorTone = "good" | "ok" | "bad";

export interface RatingFactor {
  key: "period" | "direction" | "wind" | "size" | "tide";
  phrase: string;
  /** The backend's fit for this factor; null when the response does not say. */
  tone: FactorTone | null;
  /** Named in the one-line reason (size and tide only when they work against the rating). */
  inReason: boolean;
}

export interface ReasonInput {
  periodS?: number | null;
  windRelation?: string | null;
  windSpeedKmh?: number | null;
  /** Component fits (0–1) from the API's `components`, when the response has them. */
  components?: Record<string, unknown> | null;
  /** The API's explanation text (direction fit, size vs. range and tide are read from it). */
  explanation?: string | null;
  /** Predicted breaking faces (ft) and the spot's working range (ft), when known. */
  breakingMinFt?: number | null;
  breakingMaxFt?: number | null;
  /** The central breaking estimate (ft): the height the score's size fit is computed from. */
  breakingMidFt?: number | null;
  spotMinFt?: number | null;
  spotMaxFt?: number | null;
  /** The backend's wind class (`wind_strength`) and favourability (`wind_favourability`). */
  windStrength?: string | null;
  windFavourability?: string | null;
}

export type WindStrength = "glassy" | "light" | "moderate" | "strong";

/** The backend's Beaufort bounds for its wind classes (km/h; docs/SURF_SCORING.md, stored
 * components contract), used only for predictions that carry no `wind_strength`. */
export const WIND_STRENGTH_BOUNDS_KMH = { glassy: 5, light: 12, moderate: 29 } as const;
export const GROUNDSWELL_PERIOD_S = 13;
export const MID_PERIOD_S = 10;

/** 89 → "8.9" (one decimal of the 0–100 score divided by ten). */
export function score10(score: number): string {
  const s = Math.max(0, Math.min(100, Math.round(score)));
  return (s / 10).toFixed(1);
}

/** "very_good" → "Very Good Surf". */
export function ratingTitle(label: string): string {
  return `${QUALITY_LABELS[asQuality(label)]} Surf`;
}

export function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function fitTone(v: number): FactorTone {
  return v >= 0.75 ? "good" : v >= 0.4 ? "ok" : "bad";
}

function componentTone(components: ReasonInput["components"], key: string): FactorTone | null {
  const fit = num(components?.[key]);
  return fit != null ? fitTone(fit) : null;
}

function periodFactor(periodS: number | null | undefined, components: ReasonInput["components"]): RatingFactor | null {
  if (periodS == null || !Number.isFinite(periodS)) return null;
  const tone = componentTone(components, "swell_period");
  const phrase = periodS >= GROUNDSWELL_PERIOD_S ? "long-period groundswell" : periodS >= MID_PERIOD_S ? "mid-period swell" : "short-period swell";
  return { key: "period", phrase, tone, inReason: true };
}

function directionFactor(components: ReasonInput["components"], explanation: string | null | undefined): RatingFactor | null {
  const fit = num(components?.swell_direction);
  let tone: FactorTone | null = fit != null ? fitTone(fit) : null;
  if (tone == null && explanation) {
    if (explanation.includes("well aligned with the break")) tone = "good";
    else if (explanation.includes("at the edge of the break's swell window")) tone = "ok";
    else if (explanation.includes("poorly aligned")) tone = "bad";
  }
  if (tone == null) return null;
  const phrase = tone === "good" ? "favorable direction" : tone === "ok" ? "direction at the edge of the swell window" : "poorly aligned direction";
  return { key: "direction", phrase, tone, inReason: true };
}

/** The backend's wind class, or for older predictions the class its bounds give the speed. */
export function windStrengthOf(strength: string | null | undefined, speedKmh: number | null | undefined): WindStrength | null {
  if (strength === "glassy" || strength === "light" || strength === "moderate" || strength === "strong") return strength;
  if (speedKmh == null || !Number.isFinite(speedKmh)) return null;
  const b = WIND_STRENGTH_BOUNDS_KMH;
  return speedKmh <= b.glassy ? "glassy" : speedKmh < b.light ? "light" : speedKmh < b.moderate ? "moderate" : "strong";
}

const FAVOURABILITY_TONE: Record<string, FactorTone> = { favourable: "good", workable: "ok", unfavourable: "bad" };

function windFactor(input: ReasonInput): RatingFactor | null {
  const relation = input.windRelation;
  const strength = windStrengthOf(input.windStrength, input.windSpeedKmh);
  const tone = componentTone(input.components, "wind") ?? FAVOURABILITY_TONE[input.windFavourability ?? ""] ?? null;
  // "glassy": at or below the calm threshold, where the score's wind fit is 1 (calm scores
  // like light offshore wind). A calm hour can have no direction, so no relation either.
  if (relation === "glassy" || (strength === "glassy" && (!relation || relation === "unknown")))
    return { key: "wind", phrase: "glassy, near-calm conditions", tone: tone ?? "good", inReason: true };
  if (!relation || relation === "unknown") return null;
  const PHRASE: Record<string, string> = {
    offshore: "offshore winds",
    "cross-offshore": "cross-offshore winds",
    "cross-shore": "cross-shore winds",
    "cross-onshore": "cross-onshore winds",
    onshore: "onshore winds",
  };
  const phrase = PHRASE[relation];
  if (!phrase) return null;
  const size = strength === "glassy" || strength === "light" ? "light " : strength === "strong" ? "strong " : "";
  return { key: "wind", phrase: `${size}${phrase}`, tone, inReason: true };
}

const SIZE_RE = /approx\. ([\d.]+)–([\d.]+) ft faces vs\. a working range of ([\d.]+)–([\d.]+) ft/;

function sizeFactor(input: ReasonInput): RatingFactor | null {
  let lo = input.breakingMinFt ?? null;
  let hi = input.breakingMaxFt ?? null;
  let rangeLo = input.spotMinFt ?? null;
  let rangeHi = input.spotMaxFt ?? null;
  const m = input.explanation ? SIZE_RE.exec(input.explanation) : null;
  if (m) {
    lo ??= Number(m[1]);
    hi ??= Number(m[2]);
    rangeLo ??= Number(m[3]);
    rangeHi ??= Number(m[4]);
  }
  if (lo == null || hi == null || rangeLo == null || rangeHi == null) return null;
  // Compare the height the score uses (the central estimate) when the response has it; the
  // midpoint of typical and sets overstates it, which could call a fitting size "too big".
  const mid = input.breakingMidFt ?? (lo + hi) / 2;
  const tone = componentTone(input.components, "wave_size");
  if (mid < rangeLo) return { key: "size", phrase: "waves below the break's usual size range", tone, inReason: true };
  if (mid > rangeHi) return { key: "size", phrase: "waves above the break's usual size range", tone, inReason: true };
  return { key: "size", phrase: "size within the break's range", tone, inReason: false };
}

function tideFactor(components: ReasonInput["components"], explanation: string | null | undefined): RatingFactor | null {
  const fit = num(components?.tide);
  let tone: FactorTone | null = fit != null ? fitTone(fit) : null;
  if (tone == null && explanation) {
    if (explanation.includes("unfavourable tide")) tone = "bad";
    else if (explanation.includes("workable tide")) tone = "ok";
    else if (explanation.includes("favourable tide")) tone = "good";
  }
  if (tone == null) return null;
  return { key: "tide", phrase: tone === "good" ? "favorable tide" : tone === "ok" ? "workable tide" : "unfavorable tide", tone, inReason: tone === "bad" };
}

/** Every factor the response supports, in reading order (period, direction, wind, size, tide). */
export function ratingFactors(input: ReasonInput): RatingFactor[] {
  return [
    periodFactor(input.periodS, input.components),
    directionFactor(input.components, input.explanation),
    windFactor(input),
    sizeFactor(input),
    tideFactor(input.components, input.explanation),
  ].filter((f): f is RatingFactor => f != null);
}

function sentence(parts: string[]): string {
  const [first, ...rest] = parts;
  if (!first) return "";
  const head = first.charAt(0).toUpperCase() + first.slice(1);
  if (rest.length === 0) return `${head}.`;
  if (rest.length === 1) return `${head} and ${rest[0]}.`;
  return `${[head, ...rest.slice(0, -1)].join(", ")}, and ${rest[rest.length - 1]}.`;
}

/**
 * One line such as "Long-period groundswell, favorable direction, and light offshore winds."
 * Period, direction and wind are always named when known; size only when it is outside the
 * break's working range and tide only when the API calls it unfavourable, so the line stays
 * short. Null when the response has none of these.
 */
export function ratingReason(input: ReasonInput): string | null {
  const parts = ratingFactors(input)
    .filter((f) => f.inReason)
    .slice(0, 4)
    .map((f) => f.phrase);
  return parts.length ? sentence(parts) : null;
}

export const COMPONENT_LABEL: Record<string, string> = {
  swell_direction: "Swell direction",
  swell_period: "Swell period",
  wave_size: "Wave size",
  wind: "Wind",
  tide: "Tide",
  consistency: "Consistency",
};

export const TONE_WORD: Record<FactorTone, string> = { good: "Favorable", ok: "Workable", bad: "Limiting" };
export const TONE_BAR: Record<FactorTone, string> = { good: "bg-emerald-600", ok: "bg-amber-500", bad: "bg-orange-600" };

/** Component fits (0–1) present in the API's `components`, in the documented order. */
export function componentFits(components: Record<string, unknown> | null | undefined): Array<{ key: string; label: string; value: number; tone: FactorTone }> {
  if (!components) return [];
  return Object.keys(COMPONENT_LABEL)
    .map((key) => ({ key, value: num(components[key]) }))
    .filter((c): c is { key: string; value: number } => c.value != null)
    .map((c) => ({ key: c.key, label: COMPONENT_LABEL[c.key]!, value: Math.max(0, Math.min(1, c.value)), tone: fitTone(c.value) }));
}

/** The limiting-factor gates (< 1 means that factor capped the score). */
export function limitingGates(components: Record<string, unknown> | null | undefined): string[] {
  if (!components) return [];
  const names: Record<string, string> = { size_gate: "wave size", period_gate: "swell period", wind_gate: "wind" };
  return Object.entries(names)
    .filter(([k]) => {
      const v = num(components[k]);
      return v != null && v < 0.999;
    })
    .map(([, n]) => n);
}
