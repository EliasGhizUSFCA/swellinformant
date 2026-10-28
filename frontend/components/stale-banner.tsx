import { AlertTriangle, Clock } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** "20 h" / "3 days" for a model run's age. */
export function ageText(hours: number): string {
  return hours < 48 ? `${Math.max(1, Math.round(hours))} h` : `${Math.round(hours / 24)} days`;
}

/**
 * The forecast is older than its source's stale limit. It says only what is true: the
 * confidence classes are capped at Moderate while the data is stale, and alerts are paused
 * only when `alertsPaused` says so (the alert job's own rule, at twice the stale age).
 * `runAgeHours` is the age of the run's issue time; a blend's issue time is its oldest
 * member's, so with `blend` the age is named as the oldest model run's. `lastUpdated` ("7
 * hours ago", from the spot's `freshness.last_updated_at`) leads with when this site last
 * updated the spot's forecast ("Forecast last updated 7 hours ago."), and `reason` says why
 * it is stale (e.g. its latest refresh failed while other spots updated).
 */
export function StaleBanner({
  stale,
  alertsPaused = false,
  runAgeHours,
  blend = false,
  lastUpdated,
  reason,
  className,
}: {
  stale: boolean | null | undefined;
  alertsPaused?: boolean | null;
  runAgeHours?: number | null;
  blend?: boolean;
  lastUpdated?: string | null;
  reason?: string | null;
  className?: string;
}) {
  if (!stale) return null;
  const age = runAgeHours != null && Number.isFinite(runAgeHours) ? ageText(runAgeHours) : null;
  return (
    <Alert variant="warn" className={cn("mb-6", className)} data-testid="stale-banner">
      <AlertTriangle />
      <AlertTitle>Forecast out of date</AlertTitle>
      <AlertDescription>
        {lastUpdated && (
          <span className="font-medium" data-testid="stale-last-updated">
            Forecast last updated {lastUpdated}.{" "}
          </span>
        )}
        {reason ? `${reason} ` : ""}
        {age ? `${blend ? "Its oldest model run" : "The model run"} is ${age} old` : "A newer model run is overdue"}, so confidence is capped at Moderate.
        {alertsPaused ? " New alerts are paused until fresh data arrives." : ""}
      </AlertDescription>
    </Alert>
  );
}

/** A compact "Out of date" tag for cards whose forecast run is stale (nothing when fresh). */
export function StaleBadge({ stale, dark = false, className }: { stale: boolean | null | undefined; dark?: boolean; className?: string }) {
  if (!stale) return null;
  return (
    <Badge
      variant="warn"
      className={cn(dark && "border-amber-300/40 bg-amber-400/15 text-amber-100", className)}
      title="The forecast run behind this is older than its stale limit; confidence is capped at Moderate."
      data-testid="stale-badge"
    >
      <Clock aria-hidden="true" /> Out of date
    </Badge>
  );
}
