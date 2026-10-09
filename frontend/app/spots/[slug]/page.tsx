"use client";

import { AlertTriangle, Clock, Database, Info, Plane } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams } from "next/navigation";

import { DailyStrip, QualityChart, SwellChart, WindChart } from "@/components/charts/forecast-charts";
import { ConditionsSummary } from "@/components/conditions";
import { EventCard } from "@/components/event-card";
import { AppShell } from "@/components/layout/app-shell";
import { OpportunityCard } from "@/components/opportunity-card";
import { PageHeader, SectionTitle } from "@/components/page-header";
import { DemoBadge } from "@/components/quality";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useForecast, useMe, useSpot, useSpotEvents, useSpotOpportunities } from "@/hooks/queries";
import { compass, formatDateTime, heightRange, relativeTime, titleCase, type Units } from "@/lib/format";

const SurfMap = dynamic(() => import("@/components/map/surf-map"), { ssr: false, loading: () => <Skeleton className="h-56" /> });

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function Fact({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{k}</dt>
      <dd className="text-sm font-medium">{v}</dd>
    </div>
  );
}

export default function SpotPage() {
  const { slug } = useParams<{ slug: string }>();
  const { data: user } = useMe();
  const units = (user?.profile.units as Units) ?? "ft";
  const spot = useSpot(slug);
  const forecast = useForecast(slug, 10);
  const events = useSpotEvents(slug);
  const opps = useSpotOpportunities(slug, !!user);

  if (spot.isLoading) {
    return (
      <AppShell>
        <Skeleton className="mb-6 h-12 w-80" />
        <Skeleton className="h-96" />
      </AppShell>
    );
  }
  const s = spot.data;
  if (!s) {
    return (
      <AppShell>
        <p className="text-muted-foreground">Spot not found.</p>
      </AppShell>
    );
  }
  const fc = forecast.data;
  const points = fc?.points ?? [];

  return (
    <AppShell>
      <PageHeader
        eyebrow={`${s.region} · ${s.country}`}
        title={s.name}
        description={s.description}
        actions={
          <div className="flex flex-wrap gap-2">
            <Badge variant="secondary" className="capitalize">
              {s.break_type} · {s.wave_direction}
            </Badge>
            <Badge variant="secondary" className="capitalize">
              {s.skill_level}
            </Badge>
            {s.is_big_wave && <Badge variant="warn">Big wave</Badge>}
            {fc?.source?.is_demo && <DemoBadge />}
          </div>
        }
      />

      {fc?.stale && (
        <Alert variant="warn" className="mb-6">
          <AlertTriangle />
          <AlertTitle>Forecast data is stale</AlertTitle>
          <AlertDescription>The latest model run is older than expected; confidence is reduced and alerts are paused for this data.</AlertDescription>
        </Alert>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
        <div className="space-y-6">
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Surf quality & estimated breaking height</CardTitle>
              <span className="text-xs text-muted-foreground">{s.timezone} · shaded = night</span>
            </CardHeader>
            <CardContent>
              {forecast.isLoading && <Skeleton className="h-72" />}
              {fc && !fc.available && <p className="py-10 text-center text-sm text-muted-foreground">{fc.message}</p>}
              {points.length > 0 && <QualityChart points={points} tz={s.timezone} units={units} />}
            </CardContent>
          </Card>
          {fc && fc.daily.length > 0 && (
            <section>
              <SectionTitle>Daily outlook (best daylight conditions)</SectionTitle>
              <DailyStrip daily={fc.daily} units={units} />
            </section>
          )}
          {points.length > 0 && (
            <div className="grid gap-6 xl:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Swell height & period</CardTitle>
                </CardHeader>
                <CardContent>
                  <SwellChart points={points} tz={s.timezone} />
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>Wind</CardTitle>
                </CardHeader>
                <CardContent>
                  <WindChart points={points} tz={s.timezone} />
                </CardContent>
              </Card>
            </div>
          )}

          <section>
            <SectionTitle>Upcoming swell events</SectionTitle>
            {(events.data ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">No qualifying swell windows in the current forecast.</p>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                {(events.data ?? []).map((e) => (
                  <EventCard key={e.id} event={e} units={units} />
                ))}
              </div>
            )}
          </section>

          {user && (
            <section>
              <SectionTitle>Your travel opportunities here</SectionTitle>
              {(opps.data ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  None yet. <Link href="/searches/new" className="text-ocean hover:underline">Create a search</Link> that includes this spot.
                </p>
              ) : (
                <div className="space-y-3">
                  {(opps.data ?? []).map((m) => (
                    <OpportunityCard key={m.id} match={m} units={units} />
                  ))}
                </div>
              )}
            </section>
          )}
        </div>

        <aside className="space-y-6">
          {s.current && <ConditionsSummary c={s.current} tz={s.timezone} units={units} title="Now" />}
          <div className="h-56 overflow-hidden rounded-2xl border">
            <SurfMap spots={[s]} selected={s.slug} zoom={8} center={[s.latitude, s.longitude]} />
          </div>
          <Card>
            <CardHeader>
              <CardTitle>Spot characteristics</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-2 gap-4">
                <Fact k="Swell window" v={`${s.swell_window_min}°–${s.swell_window_max}°`} />
                <Fact k="Best swell" v={`${compass(s.optimal_swell_direction_min)}–${compass(s.optimal_swell_direction_max)}`} />
                <Fact k="Offshore wind" v={`from ${compass(s.offshore_wind_direction)}`} />
                <Fact k="Swell period" v={`${s.min_swell_period_s}s+ (ideal ${s.ideal_swell_period_s}s)`} />
                <Fact k="Working size" v={heightRange(s.wave_height_min_ft, s.wave_height_max_ft, units)} />
                <Fact k="Tide" v={s.tide_preference ? titleCase(s.tide_preference) : "Not established"} />
                <Fact k="Best months" v={s.best_months.map((m) => MONTHS[m - 1]).join(", ")} />
                <Fact k="Skill" v={titleCase(s.skill_level)} />
              </dl>
              <p className="mt-4 text-sm text-muted-foreground">{s.seasonality}</p>
              <p className="mt-3 flex gap-2 text-sm text-amber-800">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" /> {s.hazards}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Getting there</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {s.airports.map((a) => (
                <div key={a.airport.iata} className="rounded-xl border p-3">
                  <div className="flex items-center gap-2 font-medium">
                    <Plane className="size-4 text-ocean" /> {a.airport.iata} · {a.airport.city}
                    {a.is_primary && <Badge variant="good">primary</Badge>}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    ~{Math.round(a.transfer_minutes / 6) / 10} h by {a.transfer_mode.replaceAll("_", " ").replace("+", " + ")}. {a.notes}
                  </div>
                </div>
              ))}
              <p className="text-muted-foreground">{s.accessibility_notes}</p>
            </CardContent>
          </Card>
          {fc?.run && fc.source && (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Database className="size-4 text-ocean" /> Forecast data
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-xs text-muted-foreground">
                <div className="text-sm font-medium text-ink">{fc.source.name}</div>
                <div>
                  Wave model {fc.source.wave_model} · run {fc.run.wave_model_run_at ? formatDateTime(fc.run.wave_model_run_at, "UTC") : "—"} UTC
                </div>
                <div>
                  Wind model {fc.source.atmosphere_model} · run{" "}
                  {fc.run.atmosphere_model_run_at ? formatDateTime(fc.run.atmosphere_model_run_at, "UTC") : "—"} UTC
                </div>
                <div className="flex items-center gap-1.5">
                  <Clock className="size-3.5" /> Ingested {fc.run.completed_at ? relativeTime(fc.run.completed_at) : "—"}
                </div>
                <div className="flex gap-1.5 pt-1">
                  <Info className="mt-0.5 size-3.5 shrink-0" />
                  <span>
                    Breaking heights use the {fc.calibrated ? "calibrated spot model" : "Komar–Gaughan approximation with an uncalibrated spot factor"} —
                    an estimate, not a measurement. {fc.source.attribution}
                  </span>
                </div>
              </CardContent>
            </Card>
          )}
        </aside>
      </div>
    </AppShell>
  );
}
