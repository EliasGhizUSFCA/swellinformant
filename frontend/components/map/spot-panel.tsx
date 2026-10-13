"use client";

import { ArrowRight, Plane, X } from "lucide-react";
import Link from "next/link";

import { ConditionsSummary } from "@/components/conditions";
import { ConfidenceBadge } from "@/components/confidence";
import { ForecastBreakdown } from "@/components/forecast-breakdown";
import { DemoBadge, MatchStatusBadge, QualityBadge } from "@/components/quality";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSpotEvents, useSpotOpportunities } from "@/hooks/queries";
import { formatMoney, formatRange, heightRange, leadLabel, type Units } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Conditions, SpotSummary } from "@/types/api";

import { DARK_TOKENS, GLASS } from "./map-ui";

/**
 * One spot at the selected time: the raw offshore model data next to our own surf
 * prediction, upcoming swell events and (signed in) the user's travel opportunities.
 */
export function SpotPanel({
  spot,
  at,
  units,
  signedIn,
  onClose,
  className,
}: {
  spot: SpotSummary;
  at: Conditions | null;
  units: Units;
  signedIn: boolean;
  onClose: () => void;
  className?: string;
}) {
  const events = useSpotEvents(spot.slug);
  const opps = useSpotOpportunities(spot.slug, signedIn);
  return (
    <section
      aria-label={`${spot.name} details`}
      className={cn("overflow-y-auto overscroll-contain rounded-2xl text-foreground", GLASS, "bg-[#15191d]/95", className)}
      style={DARK_TOKENS}
      data-testid="map-spot-panel"
    >
      <div className="space-y-4 p-5">
        <div className="flex items-start justify-between gap-2">
          <div>
            <div className="text-xs text-muted-foreground">
              {spot.region} · <span data-testid="panel-country">{spot.country}</span>
            </div>
            <h2 className="font-display text-2xl font-semibold text-ink" data-testid="panel-spot-name">
              {spot.name}
            </h2>
            <div className="mt-1 text-xs text-muted-foreground">
              <span className="capitalize">
                {spot.break_type} break · {spot.wave_direction} · {spot.skill_level}
              </span>{" "}
              · fly into {spot.primary_airport ?? "—"}
            </div>
          </div>
          <Button size="icon" variant="ghost" aria-label="Close spot details" onClick={onClose}>
            <X />
          </Button>
        </div>

        {at ? (
          <ForecastBreakdown c={at} tz={spot.timezone} units={units} />
        ) : (
          <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground" data-testid="no-forecast">
            No usable forecast for this spot at the selected time.
          </p>
        )}

        {/*
          Forecast and local confidence for the selected hour are part of ForecastBreakdown
          above. Research confidence is a separate, per-spot assessment (how well the break's
          ideal conditions are documented): shown on its own line, never merged with either.
        */}
        <div className="flex flex-wrap items-center gap-2 text-xs" data-testid="forecast-confidence-slot">
          <ConfidenceBadge value={spot.research_class} kind="research" data-testid="panel-research-confidence" />
          <Link href={`/spots/${spot.slug}#research`} className="text-muted-foreground underline-offset-2 hover:underline">
            What we know about this break
          </Link>
        </div>

        {spot.best_upcoming && <ConditionsSummary c={spot.best_upcoming} tz={spot.timezone} units={units} title="Best surf window, next 7 days" />}

        <div>
          <div className="mb-2 text-sm font-semibold">Upcoming swell events</div>
          {events.isLoading && <Skeleton className="h-16" />}
          {(events.data ?? []).length === 0 && !events.isLoading && <p className="text-sm text-muted-foreground">None detected.</p>}
          <ul className="space-y-2">
            {(events.data ?? []).map((e) => (
              <li key={e.id} className="flex items-center justify-between gap-2 rounded-lg border p-2.5 text-sm">
                <span>
                  {formatRange(e.start_time, e.end_time, e.spot.timezone)} <span className="text-muted-foreground">· {leadLabel(e.lead_days)}</span>
                  <br />
                  <span className="text-muted-foreground">{heightRange(e.peak_breaking_height_min_ft, e.peak_breaking_height_max_ft, units)}</span>
                </span>
                <span className="flex flex-col items-end gap-1">
                  <QualityBadge label={e.peak_label} score={e.peak_score} />
                  {e.is_demo && <DemoBadge />}
                </span>
              </li>
            ))}
          </ul>
        </div>

        <div>
          <div className="mb-2 text-sm font-semibold">Your travel opportunities</div>
          {!signedIn ? (
            <p className="text-sm text-muted-foreground" data-testid="opps-signed-out">
              <Link href={`/login?next=/map`} className="text-ocean hover:underline">
                Sign in
              </Link>{" "}
              to see trips matched to your saved searches.
            </p>
          ) : (opps.data ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">None for this spot yet.</p>
          ) : (
            <ul className="space-y-2">
              {(opps.data ?? []).map((m) => (
                <li key={m.id}>
                  <Link href={`/opportunities/${m.id}`} className="flex items-center justify-between gap-2 rounded-lg border p-2.5 text-sm hover:bg-muted">
                    <span className="flex items-center gap-2">
                      <Plane className="size-3.5 text-ocean" />
                      {m.best_price ? formatMoney(m.best_price, m.currency) : "Surf only"} · {formatRange(m.window_start, m.window_end, m.spot.timezone)}
                    </span>
                    <MatchStatusBadge status={m.status} />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
        <Button asChild className="w-full">
          <Link href={`/spots/${spot.slug}`}>
            Full forecast & charts <ArrowRight />
          </Link>
        </Button>
      </div>
    </section>
  );
}
