import { AlertTriangle } from "lucide-react";

import { WIND_DISPUTE_TEXT } from "@/lib/confidence";
import { cn } from "@/lib/utils";

/**
 * The disputed-wind note of forecast-v1.1 (docs/forecast-confidence.md §3a): the API's
 * `wind_dispute_note` when the sources disagree about which wind is offshore at the break.
 * Scores and `wind_relation` still use the spot profile's direction, so the note belongs next
 * to the wind it qualifies; it is not a forecast-confidence reason. Older responses only carry
 * it inside the deprecated mixed `confidence_reasons`, word for word.
 */
const WIND_DISPUTE_REASON = WIND_DISPUTE_TEXT;

/** The API's own note when the reasons carry it, else null (nothing is inferred). */
export function windDisputeNote(reasons: readonly string[] | null | undefined): string | null {
  return (reasons ?? []).find((r) => r.trim() === WIND_DISPUTE_REASON) ?? null;
}

/** A small amber note (readable on light cards and on the dark map panel alike). `note` is
 * the API's `wind_dispute_note`; without it the note is looked up in `reasons`. */
export function WindDisputeNote({
  reasons,
  note: given,
  className,
}: {
  reasons?: readonly string[] | null;
  note?: string | null;
  className?: string;
}) {
  const note = given?.trim() || windDisputeNote(reasons);
  if (!note) return null;
  return (
    <p
      className={cn("flex w-fit max-w-full items-start gap-1.5 rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-xs text-amber-900", className)}
      data-testid="wind-dispute-note"
    >
      <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      <span>{note}</span>
    </p>
  );
}
