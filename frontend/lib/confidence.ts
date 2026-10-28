/**
 * Confidence classes as the API reports them. Two separate assessments use the same three
 * classes and must never be merged into one number (spec 20):
 *
 * * forecast confidence: how much an upcoming prediction may still change (per forecast
 *   hour; `forecast_confidence_class`), and
 * * research confidence: how well a break's ideal conditions are documented (per spot).
 *
 * The API's deprecated `local_confidence_*` fields combined the two; the UI does not show them.
 *
 * Both are documented heuristic classifications, not calibrated probabilities. A missing or
 * unknown class is "Not assessed yet" — never a default of High.
 */

export type ConfidenceClass = "high" | "moderate" | "low";

const CONFIDENCE_LABEL: Record<ConfidenceClass, string> = {
  high: "High",
  moderate: "Moderate",
  low: "Low",
};

const NOT_ASSESSED = "Not assessed yet";

/** The owner's wording for what forecast confidence means (shown in every details view). */
export const FORECAST_CONFIDENCE_MEANING =
  "A rating of how much the forecast may still change, based on how far ahead it is, whether models agree and data quality. It is not a probability.";

export const RESEARCH_CONFIDENCE_MEANING =
  "How well this break's ideal conditions (swell direction, size, period, wind, tide, bathymetry and season) are documented by credible sources. It describes the spot, not the forecast, and is not a probability.";

/** A known class, or null for missing / unknown values (rendered as "Not assessed yet"). */
export function asConfidenceClass(value: string | null | undefined): ConfidenceClass | null {
  const v = value?.trim().toLowerCase();
  return v === "high" || v === "moderate" || v === "low" ? v : null;
}

export function confidenceLabel(value: string | null | undefined): string {
  const c = asConfidenceClass(value);
  return c ? CONFIDENCE_LABEL[c] : NOT_ASSESSED;
}

/** Whether an API object carries a field at all (absent = the backend does not send it yet). */
export function hasField<T extends object>(obj: T | null | undefined, key: string): boolean {
  return obj != null && Object.prototype.hasOwnProperty.call(obj, key);
}

/**
 * The legacy 0–100 index (the matching engine's "confidence" component). It measures the
 * offshore forecast's spread, not the forecast-confidence class, so it is never labelled
 * "forecast confidence" and never shown as a percentage.
 */
export const LEGACY_INDEX_LABEL = "Offshore spread index";

export function legacyIndexText(index: number | null | undefined): string | null {
  if (index == null || !Number.isFinite(index)) return null;
  return `${Math.round(index)} / 100 (not a probability)`;
}

/**
 * How our breaking-height range is named: "Likely range … (unverified)" while it comes from an
 * assumed error model or the ensemble spread (meant to hold the height about 8 times in 10,
 * not yet checked at the break); only a range calibrated against observations is called an
 * "80% range" (verified).
 */
export function rangeWords(kind: string | null | undefined): { name: string; note: string } {
  return kind === "calibrated" ? { name: "80% range", note: "verified" } : { name: "Likely range", note: "unverified" };
}

// Reasons the backend adds to the deprecated mixed `confidence_reasons` list that are NOT
// about the forecast (backend forecast_confidence.RESEARCH_REASONS, WIND_DISPUTE_REASON and
// NOT_VERIFIED_REASON, word for word).
export const RESEARCH_REASON_TEXTS = [
  "How this break responds to swell is not well documented yet.",
  "How this break responds to swell is only partly documented.",
  "How this break responds to swell is well documented.",
] as const;
export const WIND_DISPUTE_TEXT = "Sources disagree about which wind is offshore here.";
/** A note that fires on every hour while no buoy near the spot has been checked. */
export const NOT_VERIFIED_TEXT = "Not yet checked against buoys near this spot.";
const NOT_FORECAST = new Set<string>([...RESEARCH_REASON_TEXTS, WIND_DISPUTE_TEXT, NOT_VERIFIED_TEXT]);

/**
 * Forecast-confidence reasons only: whatever list is passed (the API's
 * `forecast_confidence_reasons`, or the deprecated mixed `confidence_reasons`), without the
 * research sentences, the disputed-wind note and the constant not-verified note.
 */
export function forecastOnlyReasons(reasons: readonly string[] | null | undefined): string[] {
  return [...new Set((reasons ?? []).map((r) => r.trim()).filter((r) => r !== "" && !NOT_FORECAST.has(r)))];
}

/** The forecast reasons of an API hour: `forecast_confidence_reasons`, or (older responses)
 * the mixed list filtered. */
export function forecastReasonsOf(c: { forecast_confidence_reasons?: readonly string[] | null; confidence_reasons?: readonly string[] | null }): string[] {
  const own = forecastOnlyReasons(c.forecast_confidence_reasons);
  return own.length > 0 ? own : forecastOnlyReasons(c.confidence_reasons);
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

function span(ms: number): string {
  const abs = Math.abs(ms);
  if (abs < MINUTE) return "under a minute";
  if (abs < HOUR) return `${Math.round(abs / MINUTE)} min`;
  if (abs < 48 * HOUR) return `${Math.round(abs / HOUR)} h`;
  return `${Math.round(abs / (24 * HOUR))} days`;
}

/**
 * How far a forecast valid time is from now: "in 2 h", "1 h ago", "in 3 days". A model
 * value is never "now": the nearest step can be up to one step away, and this says how far.
 */
export function validInText(validIso: string | null | undefined, nowMs: number): string | null {
  const t = validIso ? Date.parse(validIso) : NaN;
  if (!Number.isFinite(t) || !Number.isFinite(nowMs)) return null;
  const d = t - nowMs;
  if (Math.abs(d) < MINUTE) return "within a minute";
  return d > 0 ? `in ${span(d)}` : `${span(d)} ago`;
}

/** Age of the model run behind a forecast: "model run 16 h ago" (from its init time). */
export function modelRunText(issuedIso: string | null | undefined, nowMs: number): string | null {
  const t = issuedIso ? Date.parse(issuedIso) : NaN;
  if (!Number.isFinite(t) || !Number.isFinite(nowMs)) return null;
  // A clock a little behind the server's never makes a run look like it is in the future.
  return `model run ${span(Math.max(0, nowMs - t))} ago`;
}

/** Whole hours since a model run's init time (null when unknown). */
export function runAgeHours(issuedIso: string | null | undefined, nowMs: number): number | null {
  const t = issuedIso ? Date.parse(issuedIso) : NaN;
  return Number.isFinite(t) && Number.isFinite(nowMs) ? Math.max(0, (nowMs - t) / HOUR) : null;
}

/**
 * Only http(s) URLs become links; anything else (javascript:, data:, relative paths) is
 * shown as plain text.
 */
export function safeExternalUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}

export const SOURCE_TYPE_LABEL: Record<string, string> = {
  survey_data: "Survey data",
  peer_reviewed: "Peer-reviewed",
  official: "Official publication",
  guidebook: "Guidebook",
  encyclopedia: "Encyclopedia",
  surf_forecast_site: "Surf forecast site",
  magazine: "Magazine",
  community: "Community",
};
