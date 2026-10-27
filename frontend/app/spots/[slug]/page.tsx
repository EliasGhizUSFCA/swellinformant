"use client";

import { AlertTriangle, CalendarRange, Clock, Database, GitCompareArrows, Info, Plane, RefreshCw, Waves, Wind } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect } from "react";

import { uncertaintyLabel } from "@/components/charts/forecast-charts";
import { ConditionsSummary } from "@/components/conditions";
import { ForecastConfidence, LocalConfidence, ResearchConfidence } from "@/components/confidence";
import { EventCard } from "@/components/event-card";
import { ForecastBreakdown, methodLabel } from "@/components/forecast-breakdown";
import { ForecastUpdated } from "@/components/forecast-updated";
import { AttributionLinks, ForecastProvenance, useNowMs } from "@/components/forecast-provenance";
import { SpotForecastTimeline } from "@/components/forecast/forecast-timeline";
import { TimelineSelectionProvider, useTimelineActions, useTimelineSelection, useTimelineStore } from "@/components/forecast/timeline-selection";
import { InfoPopover } from "@/components/info-popover";
import { AppShell } from "@/components/layout/app-shell";
import { OpportunityCard } from "@/components/opportunity-card";
import { PageHeader, SectionTitle } from "@/components/page-header";
import { DemoBadge } from "@/components/quality";
import { RatingHeadline, WhyThisRating } from "@/components/rating";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Disclosure } from "@/components/ui/disclosure";
import { Skeleton } from "@/components/ui/skeleton";
import { StaleBanner } from "@/components/stale-banner";
import { WindDisputeNote } from "@/components/wind-dispute";
import { useForecast, useMe, useSpot, useSpotEvents, useSpotOpportunities, useSpotTimeline } from "@/hooks/queries";
import { forecastReasonsOf, rangeWords, runAgeHours } from "@/lib/confidence";
import { nearestPointIndex, pointToConditions } from "@/lib/forecast";
import { compass, formatDateTime, formatSpotDateTime, heightRange, relativeTime, seconds, titleCase, type Units } from "@/lib/format";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { ratingFactors, ratingReason } from "@/lib/rating";
import { offshoreHeightText, openingStepIndex, parseTimeParam } from "@/lib/timeline";
import { formatWind, useWindUnit } from "@/lib/units";
import type { ForecastPoint, SpotDetail, SpotForecast, SpotTimeline } from "@/types/api";

const SurfMap = dynamic(() => import("@/components/map/surf-map"), { ssr: false, loading: () => <Skeleton className="h-56" /> });
// Recharts loads only when someone opens "Compare forecast models" (a lazy, collapsed disclosure).
const ModelComparisonChart = dynamic(
  () => import("@/components/charts/model-comparison").then((m) => m.ModelComparisonChart),
  { loading: () => <Skeleton className="h-72" /> },
);

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
  wind_direction_disagree: "wind direction differs > 60° (both at 10 km/h or more)",
  same_family_mismatch: "two copies of the same model disagree (possible data problem)",
};

/**
 * The page's summary: the best surf in the next week (rating, our breaking-wave estimate,
 * swell, wind, forecast confidence) and, separately, how well the break itself is researched.
 * ``points`` (the spot forecast, when loaded) supplies the score components of the best hour,
 * which the summary itself does not carry, so "Why this rating?" can show the API's own fits.
 */
function SurfOutlook({ s, units, points }: { s: SpotDetail; units: Units; points: readonly ForecastPoint[] }) {
  const [windUnit] = useWindUnit();
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
            <h2 className="font-semibold tracking-wide text-muted-foreground uppercase">Best surf, next 7 days</h2>
            {c && (
              <span className="flex items-center gap-2 text-muted-foreground">
                {formatSpotDateTime(c.valid_time, s.timezone, { zone: true })}
                {c.is_daylight ? "" : " · night"}
                <ShowInTimeline iso={c.valid_time} />
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
                <div className="text-xs text-muted-foreground" data-testid="best-likely-range">
                  {rangeWords(c.uncertainty_kind).name} {heightRange(c.breaking_p10_ft, c.breaking_p90_ft, units)} ({rangeWords(c.uncertainty_kind).note}){" "}
                  <InfoPopover term="likelyRange" />
                </div>
              )}
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <Waves className="size-3.5" /> Offshore swell {offshoreHeightText(c.swell_height_m, units)} @ {seconds(c.swell_period_s)} from {compass(c.swell_direction_deg)}
                </span>
                <span className="flex items-center gap-1.5">
                  <Wind className="size-3.5" /> {formatWind(c.wind_speed_kmh, windUnit)} from {compass(c.wind_direction_deg)} ·{" "}
                  {WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation}
                  <InfoPopover term="windRelation" />
                </span>
              </div>
              <WindDisputeNote note={c.wind_dispute_note} reasons={c.confidence_reasons} className="mt-1.5" />
              <WhyThisRating
                className="mt-2"
                score={c.score}
                explanation={c.explanation}
                components={components}
                factors={ratingFactors(factorInput)}
                units={units}
                method={<>Method: {methodLabel(c.method)}{c.algorithm_version ? ` (v${c.algorithm_version})` : ""}</>}
              />
              <div className="mt-3 space-y-1.5 border-t pt-3">
                {/* Forecast reasons only: research and the not-yet-verified note are not about the forecast. */}
                <ForecastConfidence
                  value={c.forecast_confidence_class}
                  reasons={forecastReasonsOf(c)}
                  notes={c.confidence_reasons}
                  validTime={c.valid_time}
                  runIssuedAt={c.run_issued_at}
                  blend={c.source_code === "blend"}
                  legacyIndex={c.confidence}
                />
                <LocalConfidence value={c.local_confidence_class} forecastValue={c.forecast_confidence_class} reasons={c.confidence_reasons} reason={c.local_confidence_reason} />
              </div>
            </>
          ) : (
            <p className="mt-2 text-sm text-muted-foreground" data-testid="no-best-upcoming">
              No usable forecast for the next 7 days yet.
            </p>
          )}
        </div>
        {/* A separate assessment of the break itself: kept visibly apart from the forecast. */}
        <div id="research" className="min-w-0 scroll-mt-24 border-t pt-4 md:border-t-0 md:border-l md:pt-0 md:pl-8">
          <div className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">About this break</div>
          <ResearchConfidence research={s.research} compact />
        </div>
      </CardContent>
    </Card>
  );
}

/** Selects the week's best step in the timeline below and brings it into view. */
function ShowInTimeline({ iso }: { iso: string }) {
  const { select } = useTimelineActions();
  return (
    <button
      type="button"
      className="inline-flex items-center gap-1 rounded-full border bg-card px-2 py-0.5 font-medium text-ocean transition-colors hover:bg-secondary"
      onClick={() => {
        select(Date.parse(iso));
        const reduce = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        document.getElementById("surf-forecast")?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
      }}
    >
      <CalendarRange className="size-3" aria-hidden="true" /> Show in timeline
    </button>
  );
}

/** Seeds the shared selection once the timeline arrives: the ?t= deep link, else the step the
 * page opens on (now, or the next daylight step). */
function InitialSelection({ timeline }: { timeline: SpotTimeline | undefined }) {
  const store = useTimelineStore();
  useEffect(() => {
    if (!timeline?.steps.length) return;
    let linked: number | null = null;
    try {
      linked = parseTimeParam(new URLSearchParams(window.location.search).get("t"));
    } catch {
      linked = null;
    }
    const i = openingStepIndex(timeline, Date.now());
    store.initialise(linked ?? (i >= 0 ? Date.parse(timeline.steps[i]!.t) : null));
  }, [timeline, store]);
  return null;
}

/** The spot's forecast section: summary, day buttons and the synchronised chart rows. */
function ForecastSection({
  query,
  units,
}: {
  query: ReturnType<typeof useSpotTimeline>;
  units: Units;
}) {
  const tl = query.data;
  const p = tl?.provenance;
  const method = p ? (
    <>
      Breaking heights: {methodLabel(p.breaking_method)}
      {p.algorithm_version ? ` (v${p.algorithm_version})` : ""}
      {p.calibrated ? "" : ", not yet calibrated against surf observations at this break"}.
    </>
  ) : null;
  return (
    <Card id="surf-forecast" className="mb-6 scroll-mt-20" data-testid="surf-forecast">
      <CardHeader className="flex-row flex-wrap items-end justify-between gap-x-4 gap-y-1 px-4 pb-3 sm:px-5">
        <div>
          <CardTitle as="h2" className="font-display text-xl sm:text-2xl">
            Surf forecast
          </CardTitle>
          {p && (
            // Every model run behind the timeline with its init time, when it was fetched, and
            // the sources' credit and licence links (Open-Meteo, ECMWF, NOAA) next to the data.
            <div className="mt-0.5" data-testid="timeline-provenance">
              <ForecastProvenance
                source={p.source}
                run={p.run.init_time ? { issued_at: p.run.init_time, completed_at: p.run.completed_at } : null}
                members={p.members}
                generatedAt={tl?.generated_at}
                links={p.source.attribution_links}
              />
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <ForecastUpdated />
          {tl?.stale && <Badge variant="warn">Stale data</Badge>}
        </div>
      </CardHeader>
      <CardContent className="px-3 pb-4 sm:px-5 sm:pb-5">
        {query.isLoading && <Skeleton className="h-[34rem]" />}
        {query.isError && (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed py-10 text-center text-sm text-muted-foreground" data-testid="timeline-error">
            <p>The forecast timeline could not be loaded.</p>
            <button type="button" onClick={() => query.refetch()} className="inline-flex items-center gap-1.5 rounded-lg border bg-card px-3 py-1.5 font-medium text-ink hover:bg-muted">
              <RefreshCw className="size-3.5" aria-hidden="true" /> Try again
            </button>
          </div>
        )}
        {tl && !tl.available && (
          <p className="py-10 text-center text-sm text-muted-foreground" data-testid="timeline-unavailable">
            {tl.message ?? "No forecast is available for this spot yet."}
          </p>
        )}
        {tl && tl.available && tl.steps.length > 0 && <SpotForecastTimeline timeline={tl} units={units} method={method} />}
      </CardContent>
    </Card>
  );
}

/**
 * The selected step in full: our prediction (rating, breaking-wave estimate and its range,
 * confidence) next to the raw offshore model data it comes from, from the same run as the
 * timeline (the /forecast point at the selected valid time).
 */
function SelectedStepDetails({ fc, s, units }: { fc: SpotForecast; s: SpotDetail; units: Units }) {
  const selectedT = useTimelineSelection((x) => x.selectedT);
  const points = fc.points;
  const point = points[nearestPointIndex(points, selectedT == null ? null : new Date(selectedT).toISOString())];
  if (!point) return null;
  return (
    <Card data-testid="forecast-hour">
      <CardHeader>
        <CardTitle as="h2">Our prediction and the model data</CardTitle>
        <p className="text-xs text-muted-foreground">For the step selected in the timeline. Offshore model values are not surf heights.</p>
      </CardHeader>
      <CardContent>
        <ForecastBreakdown
          c={pointToConditions(point, fc)}
          tz={s.timezone}
          units={units}
          components={point.components}
          point={point}
          spotRange={{ minFt: s.wave_height_min_ft, maxFt: s.wave_height_max_ft }}
          source={fc.source}
          run={fc.run}
        />
      </CardContent>
    </Card>
  );
}

/** Says only what is true of a stale forecast: confidence capped, alerts paused only once the
 * alert job withholds them (`alerts_paused`), and the run's age (a blend's oldest member's). */
function ForecastStaleBanner({ fc }: { fc: SpotForecast }) {
  const now = useNowMs();
  return <StaleBanner stale={fc.stale} alertsPaused={fc.alerts_paused} runAgeHours={runAgeHours(fc.run?.issued_at ?? null, now)} blend={fc.source?.code === "blend"} />;
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
      <AttributionLinks links={fc.source.attribution_links} />
    </Disclosure>
  );
}

/** The first sentence of a spot description, and the rest (shown on request). */
function splitDescription(text: string): [string, string] {
  const m = /^(.+?[.!?])\s+(?=[A-Z0-9"“(])/.exec(text.trim());
  return m ? [m[1]!, text.trim().slice(m[0].length)] : [text.trim(), ""];
}

function SpotDescription({ text }: { text: string }) {
  const [first, rest] = splitDescription(text);
  if (!rest) return <p>{first}</p>;
  return (
    <div data-testid="spot-description">
      <p>{first}</p>
      <Disclosure summary="More about this break" className="mt-1" contentClassName="mt-1 text-sm text-muted-foreground">
        <p>{rest}</p>
      </Disclosure>
    </div>
  );
}

export default function SpotPage() {
  const { slug } = useParams<{ slug: string }>();
  const { data: user } = useMe();
  const units = (user?.profile.units as Units) ?? "ft";
  const spot = useSpot(slug);
  const forecast = useForecast(slug, 10);
  const timeline = useSpotTimeline(slug, 10);
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
  const profile = s.physical_profile;

  return (
    <AppShell>
      <PageHeader
        eyebrow={`${s.region} · ${s.country}`}
        title={s.name}
        description={<SpotDescription text={s.description} />}
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

      {fc && <ForecastStaleBanner fc={fc} />}

      {/* One selection per spot: a new spot starts from its own default (or ?t=). */}
      <TimelineSelectionProvider key={slug} initialT={null}>
        <InitialSelection timeline={timeline.data} />
        <SurfOutlook s={s} units={units} points={points} />

        <ForecastSection query={timeline} units={units} />
        <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
          <div className="min-w-0 space-y-6">
            {fc && points.length > 0 && <SelectedStepDetails fc={fc} s={s} units={units} />}
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
              <ConditionsSummary c={s.current} tz={s.timezone} units={units} spotRange={{ minFt: s.wave_height_min_ft, maxFt: s.wave_height_max_ft }} />
            )}
            <div className="h-56 overflow-hidden rounded-2xl border">
              <SurfMap spots={[s]} selected={s.slug} zoom={8} center={[s.latitude, s.longitude]} />
            </div>
            <Card>
              <CardHeader>
                <CardTitle as="h2">Spot characteristics</CardTitle>
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
                <CardTitle as="h2">Getting there</CardTitle>
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
      </TimelineSelectionProvider>
    </AppShell>
  );
}
