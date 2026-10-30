"use client";

import { useNowMs } from "@/components/forecast-provenance";
import { StaleBanner, StaleLine } from "@/components/stale-banner";
import { combinedFreshness, FRESHNESS_REASON_TEXT } from "@/lib/forecast-fields";
import { relativeTime, updatedAgo } from "@/lib/format";

/** What the trip page needs of a match to say how current its forecast is: the run's
 * staleness (`stale`) and the spot's served-forecast freshness (`freshness`), on the match
 * and on its swell event. */
export type TripForecast = {
  stale?: boolean | null;
  freshness?: unknown;
  event: { stale?: boolean | null; freshness?: unknown; last_updated_at: string };
};

/** The trip page's "Forecast out of date" banner: the run's staleness and the spot's
 * freshness (e.g. its latest refresh failed while other spots updated), with when the spot
 * was last updated and why, as on the spot page (spec 14; review r3-frontend-N1). */
export function TripStaleBanner({ match }: { match: TripForecast }) {
  const now = useNowMs();
  const fresh = combinedFreshness(match, match.event);
  return (
    <StaleBanner
      stale={match.stale || match.event.stale}
      freshnessStale={!!fresh?.stale}
      lastUpdated={fresh?.stale && fresh.lastUpdatedAt ? updatedAgo(fresh.lastUpdatedAt, now) : null}
      reason={fresh?.stale && fresh.reason ? (FRESHNESS_REASON_TEXT[fresh.reason] ?? null) : null}
    />
  );
}

/** Under the swell overview: when this site last updated the spot's forecast (its served
 * update, `freshness.last_updated_at`; the swell event's own touch time only without one).
 * A stale spot is never "updated N minutes ago": the swell detection re-reads every served
 * spot, a stale one on its retained run too, and stamps the event each time. */
export function TripUpdatedLine({ match }: { match: TripForecast }) {
  const now = useNowMs();
  const fresh = combinedFreshness(match, match.event);
  if (fresh?.stale) return <StaleLine freshness={fresh} nowMs={now} data-testid="trip-stale-line" />;
  const at = fresh?.lastUpdatedAt ?? match.event.last_updated_at;
  return (
    <p className="text-xs text-muted-foreground" data-testid="trip-updated">
      Forecast updated {relativeTime(at, new Date(now))}.
    </p>
  );
}
