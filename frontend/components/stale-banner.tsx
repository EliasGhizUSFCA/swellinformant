import { AlertTriangle, Clock } from "lucide-react";

import { useNowMs } from "@/components/forecast-provenance";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { FRESHNESS_REASON_SHORT, type Freshness } from "@/lib/forecast-fields";
import { updatedAgo } from "@/lib/format";
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
 *
 * `stale` is the run's own staleness (the API's `stale`), the one that caps the confidence
 * classes; `freshnessStale` is the spot's served-forecast staleness (`freshness.stale`), which
 * the API reports separately and which caps nothing. A spot stale only by its freshness says
 * when it was last updated and that it is not current, never that its confidence was capped.
 */
export function StaleBanner({
  stale,
  freshnessStale = false,
  alertsPaused = false,
  runAgeHours,
  blend = false,
  lastUpdated,
  reason,
  className,
}: {
  stale: boolean | null | undefined;
  freshnessStale?: boolean | null;
  alertsPaused?: boolean | null;
  runAgeHours?: number | null;
  blend?: boolean;
  lastUpdated?: string | null;
  reason?: string | null;
  className?: string;
}) {
  if (!stale && !freshnessStale) return null;
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
        {stale
          ? `${age ? `${blend ? "Its oldest model run" : "The model run"} is ${age} old` : "A newer model run is overdue"}, so confidence is capped at Moderate.`
          : "Not a current forecast."}
        {alertsPaused ? " New alerts are paused until fresh data arrives." : ""}
      </AlertDescription>
    </Alert>
  );
}

/** A compact "Out of date" tag for cards whose forecast run is stale (nothing when fresh). */
export function StaleBadge({
  stale,
  dark = false,
  className,
  title = "The forecast run behind this is older than its stale limit; confidence is capped at Moderate.",
}: {
  stale: boolean | null | undefined;
  dark?: boolean;
  className?: string;
  title?: string;
}) {
  if (!stale) return null;
  return (
    <Badge
      variant="warn"
      className={cn(dark && "border-amber-300/40 bg-amber-400/15 text-amber-100", className)}
      title={title}
      data-testid="stale-badge"
    >
      <Clock aria-hidden="true" /> Out of date
    </Badge>
  );
}

/** One line for a card whose served forecast is out of date (its `freshness.stale`): "Forecast
 * last updated 9 hours ago. Not a current forecast." and why, in a few words (the API's
 * `freshness.reason`: "Its latest refresh failed.", "The wind run is overdue."). Nothing when
 * the forecast is current, so a stale spot's rating is never shown as current (spec 14).
 * Without `nowMs` its age follows the clock (once a minute) on an open page. */
export function StaleLine(props: {
  freshness: Freshness | null | undefined;
  nowMs?: number;
  className?: string;
  "data-testid"?: string;
}) {
  if (!props.freshness?.stale) return null;
  return <StaleLineText {...props} freshness={props.freshness} />;
}

function StaleLineText({
  freshness,
  nowMs,
  className,
  "data-testid": testId = "stale-line",
}: {
  freshness: Freshness;
  nowMs?: number;
  className?: string;
  "data-testid"?: string;
}) {
  const clock = useNowMs();
  const when = freshness.lastUpdatedAt ? `Forecast last updated ${updatedAgo(freshness.lastUpdatedAt, nowMs ?? clock)}.` : "Forecast out of date.";
  const why = freshness.reason ? FRESHNESS_REASON_SHORT[freshness.reason] : undefined;
  return (
    <p className={cn("text-xs font-medium text-amber-800", className)} data-testid={testId}>
      {[when, "Not a current forecast.", why].filter(Boolean).join(" ")}
    </p>
  );
}
