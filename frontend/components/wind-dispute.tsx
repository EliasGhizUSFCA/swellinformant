import { AlertTriangle } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * The disputed-wind note of forecast-v1.1 (docs/forecast-confidence.md §3a), word for word as
 * the backend adds it to an hour's `confidence_reasons` when the sources disagree about which
 * wind is offshore at the break. Scores and `wind_relation` still use the spot profile's
 * direction, so the note belongs next to the wind it qualifies, not only among the confidence
 * reasons (where it also stays).
 */
const WIND_DISPUTE_REASON = "Sources disagree about which wind is offshore here.";

/** The API's own note when the reasons carry it, else null (nothing is inferred). */
export function windDisputeNote(reasons: readonly string[] | null | undefined): string | null {
  return (reasons ?? []).find((r) => r.trim() === WIND_DISPUTE_REASON) ?? null;
}

/** A small amber note (readable on light cards and on the dark map panel alike). */
export function WindDisputeNote({ reasons, className }: { reasons: readonly string[] | null | undefined; className?: string }) {
  const note = windDisputeNote(reasons);
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
