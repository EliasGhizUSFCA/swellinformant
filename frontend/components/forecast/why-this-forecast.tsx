"use client";

import { InfoPopover } from "@/components/info-popover";
import { ConditionLists } from "@/components/rating";
import { hourOutputs } from "@/lib/forecast-fields";
import { Disclosure } from "@/components/ui/disclosure";
import { componentFits, score10, TONE_BAR, TONE_WORD } from "@/lib/rating";
import { explanationText, type HeightUnit, type WindUnit } from "@/lib/units";
import { cn } from "@/lib/utils";
import type { TimelineStep } from "@/types/api";

// The score's gates (multipliers <= 1 the scoring engine applies when a factor is outside the
// break's working range), by the short names of the timeline contract.
const GATE_LABEL: Record<string, string> = {
  size: "wave size",
  period: "swell period",
  wind: "wind",
};

const CONTRIBUTION_LABEL: Record<string, string> = {
  swell_direction: "swell direction",
  swell_period: "swell period",
  wave_size: "wave size",
  wind: "wind",
  tide: "tide",
  consistency: "consistency",
};

/**
 * "Why this forecast?": what the scoring engine itself recorded for the selected step, and
 * nothing else: its explanation sentence, the main favourable and unfavourable conditions it
 * listed (when sent), each factor's fit (0-1, with the app's usual
 * Favorable / Workable / Limiting words), the gates that held the score back, the points each
 * factor cost when the run stored them, and whether tide was part of the rating. No text is
 * composed here beyond labels; the sentence's numbers are shown in the viewer's units.
 */
export function WhyThisForecast({
  step,
  method,
  units = "ft",
  windUnit = "km/h",
  className,
}: {
  step: TimelineStep;
  method?: React.ReactNode;
  units?: HeightUnit;
  windUnit?: WindUnit;
  className?: string;
}) {
  const why = step.why;
  const outputs = hourOutputs(step.quality);
  const fits = componentFits(why.fits);
  const gates = Object.entries(why.gates)
    .filter(([, v]) => v < 0.999)
    .map(([k, v]) => ({ key: k, label: GATE_LABEL[k] ?? k.replace(/_/g, " "), value: v }));
  const lost = Object.entries(why.contributions ?? {})
    .filter(([, v]) => Math.abs(v) >= 0.05)
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  return (
    <Disclosure
      summary="Why this forecast?"
      className={className}
      contentClassName="space-y-2.5 rounded-xl border bg-muted/40 p-3 text-xs text-muted-foreground"
      data-testid="why-this-forecast"
    >
      {why.explanation && (
        <p className="text-sm text-ink" data-testid="why-explanation">
          {explanationText(why.explanation, units, windUnit)}
        </p>
      )}
      <ConditionLists favourable={outputs.favourable} unfavourable={outputs.unfavourable} units={units} />
      {fits.length > 0 && (
        <ul className="grid gap-x-6 gap-y-1 sm:grid-cols-2" aria-label="How each factor fits this break" data-testid="why-fits">
          {fits.map((f) => (
            <li key={f.key} className="grid grid-cols-[7.5rem_1fr_4.5rem] items-center gap-2" data-fit={f.key} data-value={f.value}>
              <span>{f.label}</span>
              <span className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
                <span className={cn("block h-full rounded-full", TONE_BAR[f.tone])} style={{ width: `${Math.round(f.value * 100)}%` }} />
              </span>
              <span className="text-right font-medium text-ink">{TONE_WORD[f.tone]}</span>
            </li>
          ))}
        </ul>
      )}
      {gates.length > 0 && (
        <p data-testid="why-gates">
          Held back by{" "}
          {gates.map((g, i) => (
            <span key={g.key}>
              {i > 0 ? ", " : ""}
              <span className="font-medium text-ink">{g.label}</span> (score × {g.value.toFixed(2)})
            </span>
          ))}
          .
        </p>
      )}
      {lost.length > 0 && (
        <p data-testid="why-contributions">
          Points lost:{" "}
          {lost.map(([k, v], i) => (
            <span key={k}>
              {i > 0 ? ", " : ""}
              {CONTRIBUTION_LABEL[k] ?? k.replace(/_/g, " ")} {v > 0 ? "−" : "+"}
              {Math.abs(v).toFixed(1)}
            </span>
          ))}
          .
        </p>
      )}
      <p>{why.tide_included ? "Tide is part of this rating." : "Tide is not part of this rating."}</p>
      <p className="flex flex-wrap items-center gap-1">
        Quality score {step.quality.score} / 100 ({score10(step.quality.score)}/10), a ranking index, not a probability.
        <InfoPopover term="qualityRating" />
      </p>
      {method && <div>{method}</div>}
    </Disclosure>
  );
}
