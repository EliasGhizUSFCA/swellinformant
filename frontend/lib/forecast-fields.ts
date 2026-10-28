/**
 * Defensive readers for the Swell 2.0 forecast fields, which reach the UI through several
 * payloads (spot summary and detail, spot forecast points, timeline steps, map conditions):
 *
 * * the hour outputs of the quality model (algorithm 2.2.0): `favourable` and `unfavourable`
 *   (short plain phrases built by the backend from the score's component fits, at most three
 *   each; null for hours scored before 2.2.0), `offshore_class` and `model_version`;
 * * the two separate confidences (spec 20): `forecast_confidence` {level, kind "heuristic",
 *   method} and `research_confidence` (the spot's research class), never combined;
 * * `freshness` of a served spot forecast (design D10): {version_id, last_updated_at, stale,
 *   reason}.
 *
 * Every reader takes any object (or nothing) and checks the shape itself: a payload from an
 * older backend, a missing field or a value of the wrong type reads as null, never as a
 * guess, and never throws. Nothing here invents a value.
 */

export type ConfidenceLevel = "high" | "moderate" | "low";

export interface HourOutputs {
  /** null: not in the response (hours scored before 2.2.0, or an older backend). */
  favourable: string[] | null;
  unfavourable: string[] | null;
  /** "light offshore", "strong onshore", "glassy" ... (offshore = from the land to the sea). */
  offshoreClass: string | null;
  modelVersion: string | null;
}

export interface ForecastConfidenceInfo {
  level: ConfidenceLevel;
  /** Always "heuristic" today: a documented classification, not a calibrated probability. */
  kind: string;
  method: string | null;
}

export interface Freshness {
  versionId: number | null;
  lastUpdatedAt: string | null;
  stale: boolean;
  reason: string | null;
}

type Bag = Record<string, unknown>;

const bag = (x: unknown): Bag | null => (x && typeof x === "object" && !Array.isArray(x) ? (x as Bag) : null);
const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);
const isLevel = (v: unknown): v is ConfidenceLevel => v === "high" || v === "moderate" || v === "low";

/** A list of non-empty strings, else null (absent, null or not a list). Items that are not
 * strings are dropped; duplicates are kept once. */
function phrases(v: unknown): string[] | null {
  if (!Array.isArray(v)) return null;
  const out: string[] = [];
  for (const item of v) {
    const s = str(item)?.trim();
    if (s && !out.includes(s)) out.push(s);
  }
  return out;
}

/** The hour outputs of one forecast hour (a Conditions, ForecastPoint or timeline step's
 * `quality`). */
export function hourOutputs(x: unknown): HourOutputs {
  const b = bag(x);
  return {
    favourable: b ? phrases(b.favourable) : null,
    unfavourable: b ? phrases(b.unfavourable) : null,
    offshoreClass: b ? str(b.offshore_class) : null,
    modelVersion: b ? str(b.model_version) : null,
  };
}

/** The forecast confidence object of one hour, or null. Falls back to the older
 * `forecast_confidence_class` string (same meaning, heuristic) when the object is absent. */
export function forecastConfidenceOf(x: unknown): ForecastConfidenceInfo | null {
  const b = bag(x);
  if (!b) return null;
  const fc = bag(b.forecast_confidence);
  if (fc && isLevel(fc.level)) return { level: fc.level, kind: str(fc.kind) ?? "heuristic", method: str(fc.method) };
  return isLevel(b.forecast_confidence_class) ? { level: b.forecast_confidence_class, kind: "heuristic", method: null } : null;
}

/** The research confidence the hour was scored with, or null (not assessed / not sent). */
export function researchConfidenceOf(x: unknown): ConfidenceLevel | null {
  const b = bag(x);
  return b && isLevel(b.research_confidence) ? b.research_confidence : null;
}

/** The `freshness` block of a spot payload, or null when the payload has none. */
export function freshnessOf(x: unknown): Freshness | null {
  const f = bag(bag(x)?.freshness);
  if (!f) return null;
  const id = f.version_id;
  const at = str(f.last_updated_at);
  return {
    versionId: typeof id === "number" && Number.isSafeInteger(id) && id > 0 ? id : null,
    lastUpdatedAt: at && Number.isFinite(Date.parse(at)) ? at : null,
    stale: f.stale === true,
    reason: str(f.reason),
  };
}

/** The first freshness block found among `sources` (e.g. the forecast, the timeline and the
 * spot itself), stale if any of them says so: a spot is never shown as current while one of
 * its payloads is marked stale. */
export function combinedFreshness(...sources: unknown[]): Freshness | null {
  const found = sources.map(freshnessOf).filter((f): f is Freshness => f != null);
  if (!found.length) return null;
  const stale = found.find((f) => f.stale);
  return stale ?? found[0]!;
}

/** One plain sentence per freshness reason (the API's `freshness.reason`), shown with the
 * stale label. */
export const FRESHNESS_REASON_TEXT: Record<string, string> = {
  missing: "No forecast could be loaded for this spot.",
  refresh_failed: "The latest refresh failed for this spot; the previous forecast is shown.",
  atmosphere_age: "The wind forecast is older than its model's update interval.",
  wave_age: "The wave forecast is older than its model's update interval.",
  age: "This forecast is older than its model's update interval.",
};
