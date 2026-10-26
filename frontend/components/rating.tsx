"use client";

import { InfoPopover } from "@/components/info-popover";
import { Disclosure } from "@/components/ui/disclosure";
import { asQuality, QUALITY_COLORS, QUALITY_COLORS_DARK } from "@/lib/quality";
import { componentFits, limitingGates, ratingTitle, score10, TONE_BAR, TONE_WORD, type FactorTone, type RatingFactor } from "@/lib/rating";
import { explanationText, useWindUnit, type HeightUnit } from "@/lib/units";
import { cn } from "@/lib/utils";

/**
 * "Excellent Surf — 8.9/10" with an optional one-line reason. The reason must come from
 * lib/rating.ratingReason (built from the API's factors), never free text.
 */
export function RatingHeadline({
  label,
  score,
  reason,
  size = "md",
  dark = false,
  className,
}: {
  label: string;
  score: number;
  reason?: string | null;
  size?: "sm" | "md" | "lg";
  dark?: boolean;
  className?: string;
}) {
  const q = asQuality(label);
  return (
    <div className={className} data-testid="rating-headline">
      <div
        className={cn(
          "flex items-center gap-2 font-semibold",
          dark ? "text-white" : "text-ink",
          size === "lg" ? "font-display text-xl sm:text-2xl" : size === "md" ? "text-base sm:text-lg" : "text-sm",
        )}
      >
        <span className={cn("shrink-0 rounded-full", size === "sm" ? "size-2" : "size-2.5")} style={{ backgroundColor: (dark ? QUALITY_COLORS_DARK : QUALITY_COLORS)[q] }} aria-hidden="true" />
        <span data-testid="rating-title">
          {ratingTitle(label)} — <span className="tabular-nums">{score10(score)}/10</span>
        </span>
      </div>
      {reason && (
        <p className={cn("mt-0.5", size === "sm" ? "text-xs" : "text-sm", dark ? "text-white/75" : "text-muted-foreground")} data-testid="rating-reason">
          {reason}
        </p>
      )}
    </div>
  );
}

const TONE_TEXT: Record<FactorTone, string> = { good: "text-emerald-800", ok: "text-amber-900", bad: "text-orange-800" };

const FACTOR_LABEL: Record<RatingFactor["key"], string> = {
  period: "Swell period",
  direction: "Swell direction",
  wind: "Wind",
  size: "Wave size",
  tide: "Tide",
};

/**
 * The factor details behind a rating: the API's explanation, each component's fit (when the
 * response includes them, otherwise the ``factors`` read from the response by
 * lib/rating.ratingFactors, toned only where the API judged them), which factors capped the
 * score, the 0–100 score and the method.
 */
export function WhyThisRating({
  score,
  explanation,
  components,
  factors,
  method,
  units = "ft",
  className,
}: {
  score: number;
  explanation?: string | null;
  components?: Record<string, unknown> | null;
  factors?: readonly RatingFactor[] | null;
  method?: React.ReactNode;
  /** The viewer's height unit (the explanation is converted to it; wind follows useWindUnit). */
  units?: HeightUnit;
  className?: string;
}) {
  const [windUnit] = useWindUnit();
  const fits = componentFits(components);
  const gates = limitingGates(components);
  const derived = fits.length === 0 ? (factors ?? []) : [];
  return (
    <Disclosure summary="Why this rating?" className={className} contentClassName="space-y-2 rounded-lg bg-card/70 p-2.5 text-xs text-muted-foreground" data-testid="why-rating">
      {explanation && (
        <p className="text-ink" data-testid="why-rating-explanation">
          {explanationText(explanation, units, windUnit)}
        </p>
      )}
      {fits.length > 0 && (
        <ul className="space-y-1" aria-label="Rating factors" data-testid="rating-fits">
          {fits.map((f) => (
            <li key={f.key} className="grid grid-cols-[7.5rem_1fr_4.5rem] items-center gap-2">
              <span>{f.label}</span>
              <span className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
                <span className={cn("block h-full rounded-full", TONE_BAR[f.tone])} style={{ width: `${Math.round(f.value * 100)}%` }} />
              </span>
              <span className="text-right font-medium text-ink">{TONE_WORD[f.tone]}</span>
            </li>
          ))}
        </ul>
      )}
      {derived.length > 0 && (
        <ul className="space-y-1" aria-label="Rating factors" data-testid="rating-factors">
          {derived.map((f) => (
            <li key={f.key} className="flex flex-wrap items-center gap-x-1.5">
              <span className="w-28 shrink-0">{FACTOR_LABEL[f.key]}</span>
              <span className="text-ink">{f.phrase}</span>
              {f.phrase.includes("groundswell") && <InfoPopover term="groundswell" />}
              {/* A tone only where the API gives this factor's fit; never guessed here. */}
              {f.tone && <span className={cn("ml-auto font-medium", TONE_TEXT[f.tone])}>{TONE_WORD[f.tone]}</span>}
            </li>
          ))}
        </ul>
      )}
      {gates.length > 0 && <p>Held back by: {gates.join(", ")}.</p>}
      <p className="flex flex-wrap items-center gap-1">
        Quality score {score} / 100 (a ranking index, not a probability).
        <InfoPopover term="qualityRating" />
      </p>
      {method && <div>{method}</div>}
    </Disclosure>
  );
}
