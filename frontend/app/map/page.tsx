"use client";

import { ArrowRight, MapPin, Plane } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useMemo, useState } from "react";

import { ConditionsSummary } from "@/components/conditions";
import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { DemoBadge, MatchStatusBadge, QualityBadge } from "@/components/quality";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useMe, useSpotEvents, useSpotOpportunities, useSpots } from "@/hooks/queries";
import { formatMoney, formatRange, heightRange, leadLabel, type Units } from "@/lib/format";
import { QUALITY_COLORS, QUALITY_LABELS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { QualityLabel, SpotSummary } from "@/types/api";

const SurfMap = dynamic(() => import("@/components/map/surf-map"), {
  ssr: false,
  loading: () => <Skeleton className="h-full w-full rounded-2xl" />,
});

function SpotPanel({ spot, units, signedIn }: { spot: SpotSummary; units: Units; signedIn: boolean }) {
  const events = useSpotEvents(spot.slug);
  const opps = useSpotOpportunities(spot.slug, signedIn);
  return (
    <Card className="h-full overflow-auto" data-testid="map-spot-panel">
      <CardContent className="space-y-4 p-5">
        <div>
          <div className="text-xs text-muted-foreground">
            {spot.region} · {spot.country}
          </div>
          <h2 className="font-display text-2xl font-semibold text-ink">{spot.name}</h2>
          <div className="mt-1 text-xs text-muted-foreground">
            <span className="capitalize">
              {spot.break_type} break · {spot.wave_direction} · {spot.skill_level}
            </span>{" "}
            · fly into {spot.primary_airport ?? "—"}
          </div>
        </div>
        {spot.current ? (
          <ConditionsSummary c={spot.current} tz={spot.timezone} units={units} title="Now" />
        ) : (
          <p className="text-sm text-muted-foreground">No current forecast for this spot.</p>
        )}
        {spot.best_upcoming && <ConditionsSummary c={spot.best_upcoming} tz={spot.timezone} units={units} title="Best in the next 7 days" />}
        <div>
          <div className="mb-2 text-sm font-semibold">Upcoming swell events</div>
          {events.isLoading && <Skeleton className="h-16" />}
          {(events.data ?? []).length === 0 && !events.isLoading && <p className="text-sm text-muted-foreground">None detected.</p>}
          <ul className="space-y-2">
            {(events.data ?? []).map((e) => (
              <li key={e.id} className="flex items-center justify-between gap-2 rounded-lg border p-2.5 text-sm">
                <span>
                  {formatRange(e.start_time, e.end_time, e.spot.timezone)}{" "}
                  <span className="text-muted-foreground">· {leadLabel(e.lead_days)}</span>
                  <br />
                  <span className="text-muted-foreground">
                    {heightRange(e.peak_breaking_height_min_ft, e.peak_breaking_height_max_ft, units)}
                  </span>
                </span>
                <span className="flex flex-col items-end gap-1">
                  <QualityBadge label={e.peak_label} score={e.peak_score} />
                  {e.is_demo && <DemoBadge />}
                </span>
              </li>
            ))}
          </ul>
        </div>
        {signedIn && (
          <div>
            <div className="mb-2 text-sm font-semibold">Your travel opportunities</div>
            {(opps.data ?? []).length === 0 ? (
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
        )}
        <Button asChild className="w-full">
          <Link href={`/spots/${spot.slug}`}>
            Full forecast <ArrowRight />
          </Link>
        </Button>
      </CardContent>
    </Card>
  );
}

export default function MapPage() {
  const { data: spots = [], isLoading } = useSpots();
  const { data: user } = useMe();
  const units = (user?.profile.units as Units) ?? "ft";
  const [selected, setSelected] = useState<string | null>(null);
  const [region, setRegion] = useState<string>("All");
  const regions = useMemo(() => ["All", ...[...new Set(spots.map((s) => s.region_group))].sort()], [spots]);
  const visible = region === "All" ? spots : spots.filter((s) => s.region_group === region);
  const selectedSpot = spots.find((s) => s.slug === selected);

  return (
    <AppShell wide>
      <PageHeader
        eyebrow="Live surf map"
        title="Where the swell is heading"
        description="Each marker is a monitored break, coloured by the best forecast quality in the next seven days. Click one for details."
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {regions.map((r) => (
          <button
            key={r}
            onClick={() => setRegion(r)}
            className={cn("rounded-full border px-3 py-1 text-sm transition", region === r ? "border-ink bg-ink text-white" : "bg-card hover:border-ink/40")}
          >
            {r}
          </button>
        ))}
        <div className="ml-auto flex flex-wrap items-center gap-3 text-xs text-muted-foreground" aria-label="Legend">
          {(Object.keys(QUALITY_COLORS) as QualityLabel[]).map((q) => (
            <span key={q} className="flex items-center gap-1.5">
              <span className="size-2.5 rounded-full" style={{ backgroundColor: QUALITY_COLORS[q] }} />
              {QUALITY_LABELS[q]}
            </span>
          ))}
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
        <div className="h-[70vh] min-h-[420px] overflow-hidden rounded-2xl border shadow-sm">
          {isLoading ? <Skeleton className="h-full w-full" /> : <SurfMap spots={visible} selected={selected} onSelect={setSelected} />}
        </div>
        <div className="lg:h-[70vh]">
          {selectedSpot ? (
            <SpotPanel spot={selectedSpot} units={units} signedIn={!!user} />
          ) : (
            <Card className="h-full">
              <CardContent className="flex h-full flex-col p-5">
                <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                  <MapPin className="size-4 text-ocean" /> Top spots right now
                </div>
                <ul className="flex-1 space-y-1 overflow-auto">
                  {[...visible]
                    .sort((a, b) => (b.best_upcoming?.score ?? -1) - (a.best_upcoming?.score ?? -1))
                    .slice(0, 15)
                    .map((s) => (
                      <li key={s.slug}>
                        <button onClick={() => setSelected(s.slug)} className="flex w-full items-center justify-between gap-2 rounded-lg px-2 py-2 text-left text-sm hover:bg-muted">
                          <span className="min-w-0 truncate">
                            {s.name} <span className="text-muted-foreground">· {s.country}</span>
                          </span>
                          {s.best_upcoming ? <QualityBadge label={s.best_upcoming.label} score={s.best_upcoming.score} /> : <span className="text-xs text-muted-foreground">no data</span>}
                        </button>
                      </li>
                    ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </AppShell>
  );
}
