"use client";

import { ArrowRight, ChevronLeft, ChevronRight, Clock, MapPin, Plane, SlidersHorizontal, X } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ConditionsSummary } from "@/components/conditions";
import { ForecastBreakdown } from "@/components/forecast-breakdown";
import { AppShell } from "@/components/layout/app-shell";
import type { MarkerState } from "@/components/map/surf-map";
import { PageHeader } from "@/components/page-header";
import { DemoBadge, MatchStatusBadge, QualityBadge } from "@/components/quality";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useMapConditions, useMe, useSpotEvents, useSpotOpportunities, useSpots } from "@/hooks/queries";
import { formatMoney, formatRange, fromUnits, heightRange, leadLabel, titleCase, type Units } from "@/lib/format";
import { NO_FORECAST_COLOR, QUALITY_COLORS, QUALITY_LABELS, QUALITY_OPTIONS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { Conditions, MapSpot, QualityLabel, SpotSummary } from "@/types/api";

const SurfMap = dynamic(() => import("@/components/map/surf-map"), {
  ssr: false,
  loading: () => <Skeleton className="h-full w-full rounded-2xl" />,
});

const LEGEND: Array<{ label: string; color: string }> = [
  { label: "No usable forecast", color: NO_FORECAST_COLOR },
  ...(Object.keys(QUALITY_COLORS) as QualityLabel[]).map((q) => ({ label: QUALITY_LABELS[q], color: QUALITY_COLORS[q] })),
];

interface Filters {
  region: string;
  country: string;
  breakType: string;
  minHeight: string; // in the user's units; "" = any
  minQuality: string; // QualityLabel or ""
  eventsOnly: boolean;
}
const NO_FILTERS: Filters = { region: "All", country: "All", breakType: "any", minHeight: "", minQuality: "", eventsOnly: false };

function hourLabel(iso: string, tz: string): string {
  return new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", month: "short", day: "numeric", hour: "numeric" }).format(new Date(iso));
}

/** Forecast-time control: 3-hourly steps across the cached forecast range. */
function TimeControl({
  start,
  end,
  stepHours,
  value,
  onChange,
  tz,
}: {
  start: string;
  end: string;
  stepHours: number;
  value: number;
  onChange: (i: number) => void;
  tz: string;
}) {
  const steps = Math.max(0, Math.floor((new Date(end).getTime() - new Date(start).getTime()) / (stepHours * 3_600_000)));
  const at = new Date(new Date(start).getTime() + value * stepHours * 3_600_000).toISOString();
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-2xl border bg-card px-3 py-2 shadow-sm" data-testid="time-control">
      <Clock className="size-4 text-ocean" />
      <span className="text-sm font-medium" data-testid="time-label">
        {hourLabel(at, tz)}
      </span>
      <span className="text-xs text-muted-foreground">({tz.replaceAll("_", " ")})</span>
      <div className="ml-auto flex flex-1 items-center gap-2 sm:min-w-[320px]">
        <Button size="icon" variant="ghost" aria-label="Earlier" disabled={value <= 0} onClick={() => onChange(Math.max(0, value - 1))}>
          <ChevronLeft />
        </Button>
        <input
          type="range"
          min={0}
          max={steps}
          step={1}
          value={Math.min(value, steps)}
          onChange={(e) => onChange(Number(e.target.value))}
          aria-label="Forecast time"
          className="h-2 flex-1 cursor-pointer accent-[var(--color-ocean,#0e7490)]"
        />
        <Button size="icon" variant="ghost" aria-label="Later" disabled={value >= steps} onClick={() => onChange(Math.min(steps, value + 1))}>
          <ChevronRight />
        </Button>
        <Button size="sm" variant="outline" onClick={() => onChange(0)} disabled={value === 0}>
          Now
        </Button>
      </div>
    </div>
  );
}

function FilterBar({
  spots,
  filters,
  setFilters,
  units,
}: {
  spots: SpotSummary[];
  filters: Filters;
  setFilters: (f: Filters) => void;
  units: Units;
}) {
  const regions = useMemo(() => ["All", ...[...new Set(spots.map((s) => s.region_group))].sort()], [spots]);
  const countries = useMemo(
    () => ["All", ...[...new Set(spots.filter((s) => filters.region === "All" || s.region_group === filters.region).map((s) => s.country))].sort()],
    [spots, filters.region],
  );
  const breakTypes = useMemo(() => [...new Set(spots.map((s) => s.break_type))].sort(), [spots]);
  const set = (patch: Partial<Filters>) => setFilters({ ...filters, ...patch });
  const select = "h-9 rounded-lg border bg-card px-2 text-sm";
  return (
    <div className="space-y-3" data-testid="map-filters">
      <div className="flex flex-wrap items-center gap-2">
        {regions.map((r) => (
          <button
            key={r}
            onClick={() => set({ region: r, country: "All" })}
            aria-pressed={filters.region === r}
            className={cn("rounded-full border px-3 py-1 text-sm transition", filters.region === r ? "border-ink bg-ink text-white" : "bg-card hover:border-ink/40")}
          >
            {r}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">Country</span>
          <select className={select} value={filters.country} onChange={(e) => set({ country: e.target.value })} aria-label="Country">
            {countries.map((c) => (
              <option key={c} value={c}>
                {c === "All" ? "All countries" : c}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">Break type</span>
          <select className={select} value={filters.breakType} onChange={(e) => set({ breakType: e.target.value })} aria-label="Break type">
            <option value="any">Any break</option>
            {breakTypes.map((b) => (
              <option key={b} value={b}>
                {titleCase(b)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">Min. breaking height ({units})</span>
          <input
            type="number"
            min={0}
            step={units === "m" ? 0.5 : 1}
            inputMode="decimal"
            className={cn(select, "w-28")}
            value={filters.minHeight}
            onChange={(e) => set({ minHeight: e.target.value })}
            aria-label="Minimum breaking wave height"
            placeholder="Any"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">Min. quality</span>
          <select className={select} value={filters.minQuality} onChange={(e) => set({ minQuality: e.target.value })} aria-label="Minimum quality">
            <option value="">Any quality</option>
            {QUALITY_OPTIONS.map((q) => (
              <option key={q.value} value={q.value}>
                {q.label} ({q.min}+)
              </option>
            ))}
          </select>
        </label>
        <label className="flex h-9 items-center gap-2">
          <input type="checkbox" checked={filters.eventsOnly} onChange={(e) => set({ eventsOnly: e.target.checked })} className="size-4 accent-[#0e7490]" />
          Swell event at this time
        </label>
        {JSON.stringify(filters) !== JSON.stringify(NO_FILTERS) && (
          <Button size="sm" variant="ghost" onClick={() => setFilters(NO_FILTERS)}>
            <X /> Clear filters
          </Button>
        )}
      </div>
    </div>
  );
}

function SpotPanel({
  spot,
  at,
  units,
  signedIn,
  onClose,
}: {
  spot: SpotSummary;
  at: Conditions | null;
  units: Units;
  signedIn: boolean;
  onClose: () => void;
}) {
  const events = useSpotEvents(spot.slug);
  const opps = useSpotOpportunities(spot.slug, signedIn);
  return (
    <Card className="h-full overflow-auto" data-testid="map-spot-panel">
      <CardContent className="space-y-4 p-5">
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
      </CardContent>
    </Card>
  );
}

function passes(s: SpotSummary, m: MapSpot | undefined, f: Filters, units: Units): boolean {
  if (f.region !== "All" && s.region_group !== f.region) return false;
  if (f.country !== "All" && s.country !== f.country) return false;
  if (f.breakType !== "any" && s.break_type !== f.breakType) return false;
  const c = m?.conditions;
  if (f.minHeight !== "" && Number(f.minHeight) > 0) {
    if (!c) return false;
    const mid = (c.breaking_height_min_ft + c.breaking_height_max_ft) / 2;
    if (mid < fromUnits(Number(f.minHeight), units)) return false;
  }
  if (f.minQuality) {
    const min = QUALITY_OPTIONS.find((q) => q.value === f.minQuality)?.min ?? 0;
    if (!c || c.score < min) return false;
  }
  if (f.eventsOnly && !m?.event_at_time) return false;
  return true;
}

/** Sort key for the "best at the selected time" list: daylight hours above night ones. */
function rankKey(c: Conditions | null | undefined): number {
  if (!c) return -1;
  return (c.is_daylight ? 1000 : 0) + c.score;
}

export default function MapPage() {
  const { data: spots = [], isLoading, isError } = useSpots();
  const { data: user } = useMe();
  const units = (user?.profile.units as Units) ?? "ft";
  const tz = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const [selected, setSelected] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [stepIndex, setStepIndex] = useState(0);
  const [debouncedIndex, setDebouncedIndex] = useState(0);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedIndex(stepIndex), 250);
    return () => clearTimeout(t);
  }, [stepIndex]);
  // The "now" snapshot (cached) defines the scrubbable range; other hours are separate queries.
  const nowSnapshot = useMapConditions(null);
  // Step 0 is the server's "now" hour (the hour the default snapshot shows), so the label
  // and the markers always describe the same time.
  const range = useMemo(() => {
    const d = nowSnapshot.data;
    return d?.valid_time && d.range_end ? { start: d.valid_time, end: d.range_end, step: d.step_hours } : null;
  }, [nowSnapshot.data]);
  const at = range && debouncedIndex > 0 ? new Date(new Date(range.start).getTime() + debouncedIndex * range.step * 3_600_000).toISOString() : null;
  const snapshot = useMapConditions(at);

  const bySlug = useMemo(() => new Map((snapshot.data?.spots ?? []).map((m) => [m.slug, m])), [snapshot.data]);
  const visible = useMemo(() => spots.filter((s) => passes(s, bySlug.get(s.slug), filters, units)), [spots, bySlug, filters, units]);
  const markers = useMemo(() => {
    const out: Record<string, MarkerState> = {};
    for (const s of spots) {
      const c = bySlug.get(s.slug)?.conditions;
      out[s.slug] = c
        ? {
            score: c.score,
            tooltip: `${QUALITY_LABELS[c.label as QualityLabel] ?? c.label} ${c.score}/100 · ${heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)}`,
            highlight: !!bySlug.get(s.slug)?.event_at_time,
          }
        : { score: null, tooltip: "No usable forecast at this time" };
    }
    return out;
  }, [spots, bySlug, units]);
  const selectedSpot = spots.find((s) => s.slug === selected);
  const noForecastAnywhere = snapshot.data && !snapshot.data.valid_time;

  return (
    <AppShell wide>
      <PageHeader
        eyebrow="Live surf map"
        title="Where the swell is heading"
        description="Each marker is a monitored break, coloured by our predicted surf quality at the selected forecast time. Click one for the offshore model data and our breaking-wave prediction."
      />
      <div className="mb-4 space-y-3">
        {range ? (
          <TimeControl start={range.start} end={range.end} stepHours={range.step} value={stepIndex} onChange={setStepIndex} tz={tz} />
        ) : (
          <Skeleton className="h-12 w-full rounded-2xl" />
        )}
        <details className="group rounded-2xl border bg-card p-3 shadow-sm" open>
          <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium md:hidden">
            <SlidersHorizontal className="size-4" /> Filters
          </summary>
          <div className="mt-3 md:mt-0">
            <FilterBar spots={spots} filters={filters} setFilters={setFilters} units={units} />
          </div>
        </details>
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
          <span data-testid="visible-count">
            Showing {visible.length} of {spots.length} spots
            {snapshot.isFetching && " · updating…"}
          </span>
          <div className="flex flex-wrap items-center gap-3" aria-label="Legend" data-testid="map-legend">
            {LEGEND.map((l) => (
              <span key={l.label} className="flex items-center gap-1.5">
                <span className="size-2.5 rounded-full" style={{ backgroundColor: l.color }} />
                {l.label}
              </span>
            ))}
          </div>
        </div>
      </div>
      {isError && <p className="mb-3 text-sm text-destructive">Could not load the surf spots. Is the backend running?</p>}
      {noForecastAnywhere && (
        <p className="mb-3 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          No forecast has been ingested yet. Markers are grey until the first forecast run completes (see the status page).
        </p>
      )}
      <div className="grid gap-4 lg:grid-cols-[1fr_400px]">
        <div className="h-[60vh] min-h-[380px] overflow-hidden rounded-2xl border shadow-sm lg:h-[70vh]">
          {isLoading ? (
            <Skeleton className="h-full w-full" />
          ) : (
            <SurfMap spots={visible} selected={selected} onSelect={setSelected} markers={markers} />
          )}
        </div>
        <div className="lg:h-[70vh]">
          {selectedSpot ? (
            <SpotPanel
              spot={selectedSpot}
              at={bySlug.get(selectedSpot.slug)?.conditions ?? null}
              units={units}
              signedIn={!!user}
              onClose={() => setSelected(null)}
            />
          ) : (
            <Card className="h-full">
              <CardContent className="flex h-full flex-col p-5">
                <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                  <MapPin className="size-4 text-ocean" /> Best at the selected time
                </div>
                <ul className="flex-1 space-y-1 overflow-auto" data-testid="top-spots">
                  {[...visible]
                    // Daylight first: a high score at night is not surf anyone can ride.
                    .sort((a, b) => rankKey(bySlug.get(b.slug)?.conditions) - rankKey(bySlug.get(a.slug)?.conditions))
                    .slice(0, 15)
                    .map((s) => {
                      const c = bySlug.get(s.slug)?.conditions;
                      return (
                        <li key={s.slug}>
                          <button onClick={() => setSelected(s.slug)} className="flex w-full items-center justify-between gap-2 rounded-lg px-2 py-2 text-left text-sm hover:bg-muted">
                            <span className="min-w-0 truncate">
                              {s.name} <span className="text-muted-foreground">· {s.country}</span>
                            </span>
                            {c && !c.is_daylight && <span className="shrink-0 text-xs text-muted-foreground" data-testid="night-tag">night</span>}
                            {c ? <QualityBadge label={c.label} score={c.score} /> : <span className="text-xs text-muted-foreground">no data</span>}
                          </button>
                        </li>
                      );
                    })}
                  {visible.length === 0 && <li className="px-2 py-2 text-sm text-muted-foreground">No spots match these filters at this time.</li>}
                </ul>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </AppShell>
  );
}
