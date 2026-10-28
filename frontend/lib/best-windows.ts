/**
 * Chart annotations derived from the API's own per-step values only (spec 24): the best
 * windows marked on the surf-quality row and the tide suitability shaded on the tide row.
 *
 * * A best window is a run of consecutive daylight steps the API rated Good or better (its
 *   `quality.label`, the same class the day buttons count as "h good"). Night steps end a
 *   window; nothing is inferred between steps.
 * * Tide suitability is the score's own tide fit of a step (`why.fits.tide`, 0-1), shown
 *   only where the tide was part of the rating (`why.tide_included`: an established,
 *   researched preference); it is classed with the cut-offs the scoring explanation uses
 *   (>= 0.75 favourable, >= 0.4 workable, else unfavourable).
 */
import { asQuality, QUALITY_RANK } from "@/lib/quality";
import type { StepInfo } from "@/lib/timeline";
import type { QualityLabel } from "@/types/api";

export const BEST_WINDOW_MIN: QualityLabel = "good";

export interface BestWindow {
  /** First and last step index of the window. */
  first: number;
  last: number;
  /** Its extent on the time axis (epoch ms): from the first step's start to the last's end. */
  from: number;
  to: number;
  /** The best class and score inside, and the time of that step. */
  peak: QualityLabel;
  peakScore: number;
  peakT: number;
}

/** The runs of consecutive daylight steps rated `min` or better. */
export function bestWindows(steps: readonly StepInfo[], min: QualityLabel = BEST_WINDOW_MIN): BestWindow[] {
  const out: BestWindow[] = [];
  const floor = QUALITY_RANK[min];
  for (const s of steps) {
    const label = asQuality(s.step.quality.label);
    if (!s.step.is_daylight || QUALITY_RANK[label] < floor) continue;
    const score = s.step.quality.score;
    const last = out[out.length - 1];
    if (last && last.last === s.i - 1) {
      last.last = s.i;
      last.to = s.x1;
      if (score > last.peakScore) {
        last.peakScore = score;
        last.peak = label;
        last.peakT = s.t;
      }
    } else out.push({ first: s.i, last: s.i, from: s.x0, to: s.x1, peak: label, peakScore: score, peakT: s.t });
  }
  return out;
}

export type TideFit = "favourable" | "workable" | "unfavourable";

/** The tide suitability of one step, or null where the tide is not part of the rating. */
export function tideFit(step: StepInfo["step"]): TideFit | null {
  if (!step.why?.tide_included) return null;
  const v = step.why.fits?.tide;
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return v >= 0.75 ? "favourable" : v >= 0.4 ? "workable" : "unfavourable";
}

export interface TideFitRun {
  fit: TideFit;
  from: number;
  to: number;
  first: number;
  last: number;
}

/** Consecutive steps with the same tide suitability, merged (steps without one break runs). */
export function tideFitRuns(steps: readonly StepInfo[]): TideFitRun[] {
  const out: TideFitRun[] = [];
  for (const s of steps) {
    const fit = tideFit(s.step);
    if (!fit) continue;
    const last = out[out.length - 1];
    if (last && last.fit === fit && last.last === s.i - 1) {
      last.last = s.i;
      last.to = s.x1;
    } else out.push({ fit, from: s.x0, to: s.x1, first: s.i, last: s.i });
  }
  return out;
}
