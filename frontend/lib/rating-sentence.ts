/**
 * The one plain sentence under a rating ("Excellent Surf — 8.9/10 — Well-aligned swell and
 * light offshore wind."), built from the backend's own main favourable and unfavourable
 * conditions of the hour (algorithm 2.2.0, `favourable` / `unfavourable`), never from free
 * text. Hours without those lists (scored before 2.2.0, or an older backend) fall back to
 * lib/rating.ratingReason, which reads the factors present in the response.
 *
 * The backend writes its phrases in fixed units (faces in feet, wind in km/h) and British
 * spelling; they are shown in the viewer's units and the UI's spelling (lib/units
 * explanationText), so one screen never mixes "4–6 ft" with "1.2–1.8 m".
 */
import { hourOutputs } from "@/lib/forecast-fields";
import { ratingReason } from "@/lib/rating";
import { explanationText, type HeightUnit, type WindUnit } from "@/lib/units";

type ReasonInput = Parameters<typeof ratingReason>[0];

function capitalise(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** "a", "a and b", "a, b and c". */
function joinList(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** A phrase of the backend in the viewer's units and spelling. */
export function phraseText(phrase: string, height: HeightUnit, wind: WindUnit): string {
  return explanationText(phrase, height, wind);
}

/**
 * "Well-aligned swell and light offshore wind, but too small (0.6–0.9 m)." from the hour's
 * lists: the favourable ones first, the unfavourable ones after "but" (or alone). Null when
 * the lists are absent or both empty, so the caller can fall back.
 */
export function conditionsSentence(
  favourable: readonly string[] | null | undefined,
  unfavourable: readonly string[] | null | undefined,
  height: HeightUnit,
  wind: WindUnit,
): string | null {
  const good = (favourable ?? []).map((p) => phraseText(p, height, wind));
  const bad = (unfavourable ?? []).map((p) => phraseText(p, height, wind));
  if (!good.length && !bad.length) return null;
  if (!bad.length) return `${capitalise(joinList(good))}.`;
  if (!good.length) return `${capitalise(joinList(bad))}.`;
  return `${capitalise(joinList(good))}, but ${joinList(bad)}.`;
}

/**
 * The reason sentence of an hour: from its favourable / unfavourable lists when the response
 * has them, else ratingReason(`fallback`) (today's factor wording). `hour` is any payload
 * that may carry the lists (Conditions, ForecastPoint, a timeline step's quality).
 */
export function hourReason(hour: unknown, fallback: ReasonInput | null | undefined, height: HeightUnit, wind: WindUnit): string | null {
  const o = hourOutputs(hour);
  return conditionsSentence(o.favourable, o.unfavourable, height, wind) ?? (fallback ? ratingReason(fallback) : null);
}
