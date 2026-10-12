"use client";

import { AlertTriangle, ChevronLeft, ChevronRight, Clock, Database, GitCompareArrows, Info, Plane, Waves, Wind } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { DailyStrip, QualityChart, SwellChart, uncertaintyLabel, WindChart } from "@/components/charts/forecast-charts";
import { ModelComparisonChart } from "@/components/charts/model-comparison";
import { ConditionsSummary } from "@/components/conditions";
import { ForecastConfidence, LocalConfidence, ResearchConfidence } from "@/components/confidence";
import { EventCard } from "@/components/event-card";
import { ForecastBreakdown, methodLabel } from "@/components/forecast-breakdown";
import { InfoPopover } from "@/components/info-popover";
import { AppShell } from "@/components/layout/app-shell";
import { OpportunityCard } from "@/components/opportunity-card";
import { PageHeader, SectionTitle } from "@/components/page-header";
import { DemoBadge } from "@/components/quality";
import { RatingHeadline, WhyThisRating } from "@/components/rating";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Disclosure } from "@/components/ui/disclosure";
import { Skeleton } from "@/components/ui/skeleton";
import { useForecast, useMe, useSpot, useSpotEvents, useSpotOpportunities } from "@/hooks/queries";
import { nearestPointIndex, pointToConditions } from "@/lib/forecast";
import { compass, formatDateTime, heightRange, kmh, metres, relativeTime, seconds, titleCase, type Units } from "@/lib/format";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { ratingFactors, ratingReason } from "@/lib/rating";
import type { ForecastPoint, SpotDetail, SpotForecast } from "@/types/api";

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

/**
 * The page's summary: the best surf in the next week (rating, our breaking-wave estimate,
 * swell, wind, forecast confidence) and, separately, how well the break itself is researched.
 * ``points`` (the spot forecast, when loaded) supplies the score components of the best hour,
 * which the summary itself does not carry, so "Why this rating?" can show the API's own fits.
 */
function SurfOutlook({ s, units, points }: { s: SpotDetail; units: Units; points: readonly ForecastPoint[] }) {
  const c = s.best_upcoming;
  const at = c ? new Date(c.valid_time).getTime() : NaN;
  const components = points.find((p) => new Date(p.valid_time).getTime() === at)?.components ?? null;
  const factorInput = c && {
    components,
    periodS: c.swell_period_s,
    windRelation: c.wind_relation,
    windSpeedKmh: c.wind_speed_kmh,
    explanation: c.explanation,
    breakingMinFt: c.breaking_height_min_ft,
    breakingMaxFt: c.breaking_height_max_ft,
    breakingMidFt: c.breaking_height_mid_ft,
    spotMinFt: s.wave_height_min_ft,
    spotMaxFt: s.wave_height_max_ft,
  };
  return (
    <Card className="mb-6" data-testid="surf-outlook">
      <CardContent className="grid gap-5 p-5 md:grid-cols-[1.25fr_1fr] md:gap-8">
        <div className="min-w-0" data-testid="best-upcoming">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 text-xs">
            <span className="font-semibold tracking-wide text-muted-foreground uppercase">Best surf, next 7 days</span>
            {c && (
              <span className="text-muted-foreground">
                {formatDateTime(c.valid_time, s.timezone)} local{c.is_daylight ? "" : " · night"}
              </span>
            )}
          </div>
          {c && factorInput ? (
            <>
              <RatingHeadline className="mt-2" size="lg" label={c.label} score={c.score} reason={ratingReason(factorInput)} />
              <div className="mt-3 flex flex-wrap items-baseline gap-x-2">
                <span className="font-display text-3xl font-semibold">{heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)}</span>
                <span className="text-xs text-muted-foreground">
                  est. breaking faces <InfoPopover term="breakingHeight" />
                </span>
              </div>
              {c.breaking_p10_ft != null && c.breaking_p90_ft != null && (
                <div className="text-xs text-muted-foreground">
                  80% range {heightRange(c.breaking_p10_ft, c.breaking_p90_ft, units)} <InfoPopover term="range80" />
                </div>
              )}
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <Waves className="size-3.5" /> Offshore swell {metres(c.swell_height_m)} @ {seconds(c.swell_period_s)} from {compass(c.swell_direction_deg)}
                </span>
                <span className="flex items-center gap-1.5">
                  <Wind className="size-3.5" /> {kmh(c.wind_speed_kmh)} from {compass(c.wind_direction_deg)} ·{" "}
                  {WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation}
                  <InfoPopover term="windRelation" />
                </span>
              </div>
              <WhyThisRating
                className="mt-2"
                score={c.score}
                explanation={c.explanation}
                components={components}
                factors={ratingFactors(factorInput)}
                method={<>Method: {methodLabel(c.method)}{c.algorithm_version ? ` (v${c.algorithm_version})` : ""}</>}
              />
              <div className="mt-3 space-y-1.5 border-t pt-3">
                <ForecastConfidence value={c.forecast_confidence_class} reasons={c.confidence_reasons} leadHours={c.lead_hours} legacyIndex={c.confidence} />
                <LocalConfidence value={c.local_confidence_class} forecastValue={c.forecast_confidence_class} reasons={c.confidence_reasons} />
              </div>
            </>
          ) : (
            <p className="mt-2 text-sm text-muted-foreground" data-testid="no-best-upcoming">
              No usable forecast for the next 7 days yet.
            </p>
          )}
        </div>
        {/* A separate assessment of the break itself: kept visibly apart from the forecast. */}
        <div className="min-w-0 border-t pt-4 md:border-t-0 md:border-l md:pt-0 md:pl-8">
          <div className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">About this break</div>
          <ResearchConfidence research={s.research} />
        </div>
      </CardContent>
    </Card>
  );
}

/** Pick a forecast hour (by day or step) and see our prediction next to the raw model data. */
function ForecastHour({
  fc,
  s,
  units,
  selected,
  onSelect,
}: {
  fc: SpotForecast;
  s: SpotDetail;
  units: Units;
  selected: string | null;
  onSelect: (iso: string) => void;
}) {
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
        <CardTitle>Hour by hour</CardTitle>
        <div className="flex items-center gap-1">
          <button type="button" aria-label="Three hours earlier" onClick={() => step(-3)} className="rounded-md border p-1.5 hover:bg-muted">
            <ChevronLeft className="size-4" />
          </button>
          <button type="button" aria-label="Three hours later" onClick={() => step(3)} className="rounded-md border p-1.5 hover:bg-muted">
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
        <ForecastBreakdown
          c={pointToConditions(point, fc)}
          tz={s.timezone}
          units={units}
          components={point.components}
          point={point}
          spotRange={{ minFt: s.wave_height_min_ft, maxFt: s.wave_height_max_ft }}
        />
      </CardContent>
    </Card>
  );
}

function Fact({ k, v }: { k: React.ReactNode; v: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{k}</dt>
      <dd className="text-sm font-medium">{v}</dd>
    </div>
  );
}

/** Where the forecast comes from: models, runs, the blend and the breaking-height method. */
function ForecastSource({ fc }: { fc: SpotForecast }) {
  if (!fc.run || !fc.source) return null;
  return (
    <Disclosure
      variant="card"
      summary={
        <>
          <Database className="size-4 text-ocean" /> About this forecast
        </>
      }
      hint={fc.source.is_demo ? "demo data" : undefined}
      contentClassName="space-y-2 text-xs text-muted-foreground"
      data-testid="forecast-source"
    >
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
          <div>Weights: {typeof fc.blend.skill === "string" ? "lead-time prior (no verification yet)" : "from verification against buoys"}</div>
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
          {uncertaintyLabel(fc.points[0]?.uncertainty_kind)}. {fc.source.attribution}
        </span>
      </div>
    </Disclosure>
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
  const profile = s.physical_profile;

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
          <AlertDescription>The latest model run is late, so confidence is reduced and alerts are paused for this data.</AlertDescription>
        </Alert>
      )}

      <SurfOutlook s={s} units={units} points={points} />

      <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
        <div className="min-w-0 space-y-6">
          <Card>
            <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
              <CardTitle>Surf quality & breaking waves</CardTitle>
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
              <SectionTitle>Daily outlook · tap a day for its best hour</SectionTitle>
              <DailyStrip daily={fc.daily} units={units} selected={selected ? selectedDay : null} onSelect={(d) => d.best_time && setSelected(d.best_time)} />
            </section>
          )}
          {fc && points.length > 0 && <ForecastHour fc={fc} s={s} units={units} selected={selected} onSelect={setSelected} />}
          {points.length > 0 && (
            <div className="grid gap-6 xl:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Offshore swell height & period</CardTitle>
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
          {fc && fc.models.length > 0 && (
            <Disclosure
              variant="card"
              lazy
              className="shadow-sm"
              summary={
                <>
                  <GitCompareArrows className="size-4 text-ocean" /> Compare forecast models
                </>
              }
              hint={`${fc.models.length} model${fc.models.length === 1 ? "" : "s"}`}
            >
              <ModelComparisonChart series={fc.models} ensemble={fc.ensemble} tz={s.timezone} />
            </Disclosure>
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

        <aside className="min-w-0 space-y-6">
          {s.current && (
            <ConditionsSummary c={s.current} tz={s.timezone} units={units} title="Now" spotRange={{ minFt: s.wave_height_min_ft, maxFt: s.wave_height_max_ft }} />
          )}
          <div className="h-56 overflow-hidden rounded-2xl border">
            <SurfMap spots={[s]} selected={s.slug} zoom={8} center={[s.latitude, s.longitude]} />
          </div>
          <Card>
            <CardHeader>
              <CardTitle>Spot characteristics</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <dl className="grid grid-cols-2 gap-4">
                <Fact k="Best swell" v={`${compass(s.optimal_swell_direction_min)}–${compass(s.optimal_swell_direction_max)}`} />
                <Fact k="Offshore wind" v={`from ${compass(s.offshore_wind_direction)}`} />
                <Fact k="Swell period" v={`${s.min_swell_period_s}s+ (ideal ${s.ideal_swell_period_s}s)`} />
                <Fact k="Working size" v={heightRange(s.wave_height_min_ft, s.wave_height_max_ft, units)} />
                <Fact k="Tide" v={s.tide_preference ? titleCase(s.tide_preference) : "Not established"} />
                <Fact k="Best months" v={s.best_months.map((m) => MONTHS[m - 1]).join(", ")} />
              </dl>
              <p className="flex gap-2 text-sm text-amber-800">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" /> {s.hazards}
              </p>
              <Disclosure summary="Season and technical details" contentClassName="space-y-3">
                <p className="text-sm text-muted-foreground">{s.seasonality}</p>
                <dl className="grid grid-cols-2 gap-4">
                  <Fact
                    k={
                      <span className="inline-flex items-center gap-1">
                        Swell window <InfoPopover term="swellWindow" />
                      </span>
                    }
                    v={`${s.swell_window_min}°–${s.swell_window_max}°`}
                  />
                  <Fact k="Skill" v={titleCase(s.skill_level)} />
                  {profile && (
                    <Fact
                      k="Shore normal (model input)"
                      v={`${Math.round(profile.shore_normal_deg)}° · ${PROVENANCE_LABEL[profile.data_quality.shore_normal_deg ?? "unknown"]}`}
                    />
                  )}
                  {profile && (
                    <Fact
                      k="Offshore point depth"
                      v={profile.forecast_point_depth_m != null ? `${Math.round(profile.forecast_point_depth_m)} m` : "unknown (deep water assumed)"}
                    />
                  )}
                  {profile && (
                    <Fact k="Controlling depth" v={profile.controlling_depth_m != null ? `${Math.round(profile.controlling_depth_m)} m` : "unknown"} />
                  )}
                </dl>
                {profile?.notes && <p className="text-xs text-muted-foreground">{profile.notes}</p>}
              </Disclosure>
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
          {fc && <ForecastSource fc={fc} />}
        </aside>
      </div>
    </AppShell>
  );
}
