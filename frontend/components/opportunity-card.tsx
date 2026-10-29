import { ArrowRight, CalendarDays, Plane } from "lucide-react";
import Link from "next/link";

import { ConfidenceBadge } from "@/components/confidence";
import { DemoBadge, MatchStatusBadge, QualityBadge, ScoreRing } from "@/components/quality";
import { StaleBadge, StaleLine } from "@/components/stale-banner";
import { freshnessOf } from "@/lib/forecast-fields";
import { formatCalendarDate, formatMoney, formatRange, heightRange, leadLabel, type Units } from "@/lib/format";
import type { MatchSummary } from "@/types/api";

/**
 * A matched trip: overall match, our breaking-wave estimate and rating at the peak, the surf
 * window and the best flight, with the swell's forecast confidence from the API ("Not
 * assessed yet" when it has none), and an "Out of date" tag when the forecast run behind the
 * match is stale or the published version marks its spot stale (`freshness`, with the
 * spot's last update; spec 14).
 */
export function OpportunityCard({ match, units = "ft" }: { match: MatchSummary; units?: Units }) {
  const tz = match.spot.timezone;
  const fresh = freshnessOf(match);
  return (
    <Link
      href={`/opportunities/${match.id}`}
      className="group flex gap-4 rounded-2xl border bg-card p-4 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md"
      data-testid="opportunity-card"
    >
      <ScoreRing score={match.overall_score} label={`Overall match ${match.overall_score} of 100`} caption="match" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-semibold text-ink">{match.spot.name}</span>
          <span className="text-xs text-muted-foreground">{match.spot.country}</span>
          <MatchStatusBadge status={match.status} />
          {match.is_demo && <DemoBadge />}
          {match.has_mock_flights && <DemoBadge mock />}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span className="font-medium">
            {heightRange(match.breaking_height_min_ft, match.breaking_height_max_ft, units)}
            <span className="ml-1 text-xs font-normal text-muted-foreground">breaking</span>
          </span>
          <QualityBadge label={match.peak_label} score={match.peak_score} />
          <span className="flex items-center gap-1 text-muted-foreground">
            <CalendarDays className="size-3.5" />
            {formatRange(match.window_start, match.window_end, tz)} · {leadLabel(match.lead_days)}
          </span>
        </div>
        <div className="mt-2 text-sm text-muted-foreground">
          {match.status === "flight_found" && match.best_price ? (
            <span className="flex flex-wrap items-center gap-x-1.5 font-medium text-ink">
              <Plane className="size-3.5 text-ocean" />
              {match.origin_iata} → {match.destination_iata} · {formatMoney(match.best_price, match.currency)}
              {match.best_offer_expired && <span className="font-normal text-amber-700">(quote expired)</span>}
              <span className="font-normal text-muted-foreground">
                · arrive {formatCalendarDate(match.recommended_arrival_date)}, leave {formatCalendarDate(match.recommended_departure_date)}
              </span>
            </span>
          ) : (
            <span>{match.status_reason ?? "No eligible flight found yet."}</span>
          )}
        </div>
        <StaleLine freshness={fresh} className="mt-2" data-testid="opportunity-card-stale" />
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="flex flex-wrap items-center gap-1.5">
            <ConfidenceBadge value={match.forecast_confidence_class} />
            <StaleBadge
              stale={match.stale || fresh?.stale}
              title={match.stale ? undefined : "This spot's forecast was not updated with the latest refresh; it is not a current forecast."}
            />
          </span>
          <span className="flex items-center gap-1 text-xs font-medium text-ocean opacity-0 transition group-hover:opacity-100 group-focus-visible:opacity-100">
            Details <ArrowRight className="size-3.5" />
          </span>
        </div>
      </div>
    </Link>
  );
}
