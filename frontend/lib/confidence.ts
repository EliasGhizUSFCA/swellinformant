/**
 * Confidence classes as the API reports them. Two separate assessments use the same three
 * classes and must never be merged into one number:
 *
 * * forecast confidence: how much an upcoming prediction may still change (per forecast
 *   hour; `forecast_confidence_class`, plus `local_confidence_class` for the breaking-wave
 *   estimate at the break), and
 * * research confidence: how well a break's ideal conditions are documented (per spot).
 *
 * Both are documented heuristic classifications, not calibrated probabilities. A missing or
 * unknown class is "Not assessed yet" — never a default of High.
 */

export type ConfidenceClass = "high" | "moderate" | "low";

export const CONFIDENCE_CLASSES: readonly ConfidenceClass[] = ["high", "moderate", "low"];

export const CONFIDENCE_LABEL: Record<ConfidenceClass, string> = {
  high: "High",
  moderate: "Moderate",
  low: "Low",
};

export const NOT_ASSESSED = "Not assessed yet";

/** The owner's wording for what forecast confidence means (shown in every details view). */
export const FORECAST_CONFIDENCE_MEANING =
  "A rating of how much the forecast may still change, based on how far ahead it is, whether models agree and data quality. It is not a probability.";

export const RESEARCH_CONFIDENCE_MEANING =
  "How well this break's ideal conditions (swell direction, size, period, wind, tide, bathymetry and season) are documented by credible sources. It describes the spot, not the forecast, and is not a probability.";

export const LOCAL_CONFIDENCE_MEANING =
  "How much to trust our breaking-wave estimate at this break. It also depends on how well the break is researched, so it can differ from the offshore forecast confidence. It is not a probability.";

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
 * The legacy 0–100 confidence index, shown only inside details and never as a percentage.
 */
export function legacyIndexText(index: number | null | undefined): string | null {
  if (index == null || !Number.isFinite(index)) return null;
  return `index ${Math.round(index)} / 100 (not a probability)`;
}

/** "about 7 days ahead" from the API's lead time in hours; null when unknown or in the past. */
export function leadTimeText(leadHours: number | null | undefined): string | null {
  if (leadHours == null || !Number.isFinite(leadHours) || leadHours < 0) return null;
  if (leadHours < 1) return "under an hour ahead";
  if (leadHours < 36) {
    const h = Math.round(leadHours);
    return `about ${h} hour${h === 1 ? "" : "s"} ahead`;
  }
  const d = Math.round(leadHours / 24);
  return `about ${d} days ahead`;
}

/**
 * The reason that explains a local (at-the-break) class, chosen from the API's own reasons:
 * the last one that talks about the break or its research (the API lists the forecast reasons
 * first and the research-based one after them). Words such as "spot" are left out on purpose:
 * forecast reasons use them too ("not yet checked against buoys near this spot"). Null when no
 * reason qualifies, so nothing is invented or misattributed.
 */
export function pickLocalReason(reasons: readonly string[] | null | undefined): string | null {
  const r = [...(reasons ?? [])].reverse().find((x) => /\b(break|research|researched|bathymetry|nearshore)\b/i.test(x));
  return r ?? null;
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
