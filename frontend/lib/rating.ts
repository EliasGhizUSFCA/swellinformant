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
 * * wind speed: < 12 km/h "light" (Beaufort 2 and below), ≥ 35 km/h "strong" (the score's
 *   strong-wind penalty starts there);
 * * fit components (0–1): ≥ 0.75 favourable, ≥ 0.4 workable, else limiting — the same cut-offs
 *   the backend uses for its explanation text.
 */
import { asQuality, QUALITY_LABELS } from "@/lib/quality";

export type FactorTone = "good" | "ok" | "bad";

export interface RatingFactor {
  key: "period" | "direction" | "wind" | "size" | "tide";
  phrase: string;
  tone: FactorTone;
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
  spotMinFt?: number | null;
  spotMaxFt?: number | null;
}

export const LIGHT_WIND_KMH = 12;
export const STRONG_WIND_KMH = 35;
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

/** "Excellent Surf — 8.9/10". */
export function ratingHeadline(label: string, score: number): string {
  return `${ratingTitle(label)} — ${score10(score)}/10`;
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function fitTone(v: number): FactorTone {
  return v >= 0.75 ? "good" : v >= 0.4 ? "ok" : "bad";
}

function periodFactor(periodS: number | null | undefined): RatingFactor | null {
  if (periodS == null || !Number.isFinite(periodS)) return null;
  if (periodS >= GROUNDSWELL_PERIOD_S) return { key: "period", phrase: "long-period groundswell", tone: "good" };
  if (periodS >= MID_PERIOD_S) return { key: "period", phrase: "mid-period swell", tone: "ok" };
  return { key: "period", phrase: "short-period swell", tone: "bad" };
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
  return { key: "direction", phrase, tone };
}

function windFactor(relation: string | null | undefined, speed: number | null | undefined): RatingFactor | null {
  if (!relation || relation === "unknown") return null;
  if (relation === "glassy") return { key: "wind", phrase: "glassy, near-calm conditions", tone: "good" };
  const light = speed != null && speed < LIGHT_WIND_KMH;
  const strong = speed != null && speed >= STRONG_WIND_KMH;
  const size = light ? "light " : strong ? "strong " : "";
  const name: Record<string, [string, FactorTone, FactorTone]> = {
    // [phrase, tone when light/moderate, tone when strong]
    offshore: ["offshore winds", "good", "ok"],
    "cross-offshore": ["cross-offshore winds", "good", "ok"],
    "cross-shore": ["cross-shore winds", "ok", "bad"],
    "cross-onshore": ["cross-onshore winds", "bad", "bad"],
    onshore: ["onshore winds", "bad", "bad"],
  };
  const entry = name[relation];
  if (!entry) return null;
  const [phrase, normal, strongTone] = entry;
  // Light onshore wind only roughens the surface a little: workable rather than limiting.
  const tone: FactorTone = strong ? strongTone : light && normal === "bad" ? "ok" : normal;
  return { key: "wind", phrase: `${size}${phrase}`, tone };
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
  const mid = (lo + hi) / 2;
  if (mid < rangeLo) return { key: "size", phrase: "waves below the break's usual size range", tone: "bad" };
  if (mid > rangeHi) return { key: "size", phrase: "waves above the break's usual size range", tone: "bad" };
  return { key: "size", phrase: "size within the break's range", tone: "good" };
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
  return { key: "tide", phrase: tone === "good" ? "favorable tide" : tone === "ok" ? "workable tide" : "unfavorable tide", tone };
}

/** Every factor the response supports, in reading order (period, direction, wind, size, tide). */
export function ratingFactors(input: ReasonInput): RatingFactor[] {
  return [
    periodFactor(input.periodS),
    directionFactor(input.components, input.explanation),
    windFactor(input.windRelation, input.windSpeedKmh),
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
 * Period, direction and wind are always named when known; size and tide only when they
 * limit the rating, so the line stays short. Null when the response has none of these.
 */
export function ratingReason(input: ReasonInput): string | null {
  const factors = ratingFactors(input).filter((f) => (f.key === "size" || f.key === "tide" ? f.tone === "bad" : true));
  const parts = factors.slice(0, 4).map((f) => f.phrase);
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
