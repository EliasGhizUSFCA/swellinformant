"use client";

import { AlertTriangle, ChevronLeft, ChevronRight, Clock, Database, GitCompareArrows, Info, Plane } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { DailyStrip, QualityChart, SwellChart, uncertaintyLabel, WindChart } from "@/components/charts/forecast-charts";
import { ModelComparisonChart } from "@/components/charts/model-comparison";
import { ConditionsSummary } from "@/components/conditions";
import { EventCard } from "@/components/event-card";
import { ForecastBreakdown, methodLabel } from "@/components/forecast-breakdown";
import { AppShell } from "@/components/layout/app-shell";
import { OpportunityCard } from "@/components/opportunity-card";
import { PageHeader, SectionTitle } from "@/components/page-header";
import { DemoBadge } from "@/components/quality";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useForecast, useMe, useSpot, useSpotEvents, useSpotOpportunities } from "@/hooks/queries";
import { nearestPointIndex, pointToConditions } from "@/lib/forecast";
import { compass, formatDateTime, heightRange, relativeTime, titleCase, type Units } from "@/lib/format";
import type { SpotForecast } from "@/types/api";

const SurfMap = dynamic(() => import("@/components/map/surf-map"), { ssr: false, loading: () => <Skeleton className="h-56" /> });

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const PROVENANCE_LABEL: Record<string, string> = {
  measured: "measured",
  literature: "from literature",
  calibrated: "calibrated",
  estimated: "estimated",
  unknown: "unknown",
};

const FLAG_LABEL: Record<string, string> = {
  hs_disagree: "wave height differs > 40% between independent models",
  period_disagree: "peak period differs > 3 s",
  direction_disagree: "swell direction differs",
  wind_disagree: "wind speed differs > 15 km/h",
  same_family_mismatch: "two copies of the same model disagree (possible data problem)",
};

/** Pick a forecast hour (by day or step) and see the raw model data next to our prediction. */
function ForecastHour({ fc, tz, units, selected, onSelect }: { fc: SpotForecast; tz: string; units: Units; selected: string | null; onSelect: (iso: string) => void }) {
  const points = fc.points;
  const idx = nearestPointIndex(points, selected);
  const point = points[idx];
  if (!point) return null;
  const step = (delta: number) => {
    const next = points[Math.min(points.length - 1, Math.max(0, idx + delta))];
    if (next) onSelect(next.valid_time);
  };
  return (
    <Card data-testid="forecast-hour">
      <CardHeader className="flex-row items-center justify-between gap-2">
        <CardTitle>Forecast for a chosen hour</CardTitle>
        <div className="flex items-center gap-1">
          <button type="button" aria-label="Three hours earlier" onClick={() => step(-3)} className="rounded-md border p-1 hover:bg-muted">
            <ChevronLeft className="size-4" />
          </button>
          <button type="button" aria-label="Three hours later" onClick={() => step(3)} className="rounded-md border p-1 hover:bg-muted">
            <ChevronRight className="size-4" />
          </button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <input
          type="range"
          min={0}
          max={points.length - 1}
          value={idx}
          onChange={(e) => {
            const next = points[Number(e.target.value)];
            if (next) onSelect(next.valid_time);
          }}
          className="w-full accent-[var(--ocean)]"
          aria-label="Forecast hour"
          data-testid="hour-slider"
        />
        <ForecastBreakdown c={pointToConditions(point, fc)} tz={tz} units={units} />
      </CardContent>
    </Card>
  );
}

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
  const [selected, setSelected] = useState<string | null>(null);

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
  const selectedDay = (() => {
    const p = points[nearestPointIndex(points, selected)];
    return p ? new Intl.DateTimeFormat("en-CA", { timeZone: s.timezone }).format(new Date(p.valid_time)) : null;
  })();

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
              <SectionTitle>Daily outlook (best daylight conditions) · select a day</SectionTitle>
              <DailyStrip daily={fc.daily} units={units} selected={selected ? selectedDay : null} onSelect={(d) => d.best_time && setSelected(d.best_time)} />
            </section>
          )}
          {fc && points.length > 0 && <ForecastHour fc={fc} tz={s.timezone} units={units} selected={selected} onSelect={setSelected} />}
          {fc && fc.models.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <GitCompareArrows className="size-4 text-ocean" /> Raw model data · model comparison
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ModelComparisonChart series={fc.models} ensemble={fc.ensemble} tz={s.timezone} />
              </CardContent>
            </Card>
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
                {s.physical_profile && (
                  <Fact
                    k="Shore normal (model input)"
                    v={`${Math.round(s.physical_profile.shore_normal_deg)}° · ${PROVENANCE_LABEL[s.physical_profile.data_quality.shore_normal_deg ?? "unknown"]}`}
                  />
                )}
                {s.physical_profile && (
                  <Fact
                    k="Offshore point depth"
                    v={s.physical_profile.forecast_point_depth_m != null ? `${Math.round(s.physical_profile.forecast_point_depth_m)} m` : "unknown (deep water assumed)"}
                  />
                )}
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
                  Wave model {fc.source.wave_model}
                  {fc.run.wave_model_run_at && ` · run ${formatDateTime(fc.run.wave_model_run_at, "UTC")} UTC`}
                </div>
                <div>
                  Wind model {fc.source.atmosphere_model}
                  {fc.run.atmosphere_model_run_at && ` · run ${formatDateTime(fc.run.atmosphere_model_run_at, "UTC")} UTC`}
                </div>
                <div className="flex items-center gap-1.5">
                  <Clock className="size-3.5" /> Ingested {fc.run.completed_at ? relativeTime(fc.run.completed_at) : "—"}
                </div>
                {fc.blend && (
                  <div className="space-y-1 rounded-lg border p-2" data-testid="blend-info">
                    <div className="font-medium text-ink">Blended from {fc.blend.members.length} independent models</div>
                    <div>{fc.blend.members.map((m) => String(m.code)).join(" + ")}</div>
                    {fc.blend.cross_checks.length > 0 && <div>Cross-checked against {fc.blend.cross_checks.map((m) => String(m.code)).join(", ")} (same model family)</div>}
                    <div>
                      Weights: {typeof fc.blend.skill === "string" ? "lead-time prior (no verification yet)" : "from verification against buoys"}
                    </div>
                    {fc.blend.flagged_hours > 0 && (
                      <div className="text-amber-800">
                        Models disagree in {fc.blend.flagged_hours} hours:{" "}
                        {Object.entries(fc.blend.flags)
                          .map(([k, n]) => `${FLAG_LABEL[k] ?? k} (${n} h)`)
                          .join("; ")}
                      </div>
                    )}
                  </div>
                )}
                <div className="flex gap-1.5 pt-1">
                  <Info className="mt-0.5 size-3.5 shrink-0" />
                  <span data-testid="method-note">
                    Breaking heights: {methodLabel(fc.breaking_height_method)}
                    {fc.algorithm_version ? ` (v${fc.algorithm_version})` : ""} — an estimate, not a measurement
                    {fc.calibrated ? "" : ", not yet calibrated against surf observations at this break"}. Forecast ranges:{" "}
                    {uncertaintyLabel(points[0]?.uncertainty_kind)}. {fc.source.attribution}
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
