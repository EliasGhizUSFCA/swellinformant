"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, CalendarCheck, ChevronDown, ExternalLink, Info, Luggage, Plane, RefreshCw, ShieldCheck, Waves, XCircle } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { DailyStrip } from "@/components/charts/forecast-charts";
import { AppShell } from "@/components/layout/app-shell";
import { PageHeader, SectionTitle } from "@/components/page-header";
import { ConfidenceBadge, DemoBadge, MatchStatusBadge, QualityBadge, ScoreRing } from "@/components/quality";
import { RequireAuth } from "@/components/require-auth";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { qk, useOpportunity } from "@/hooks/queries";
import { errorMessage, post } from "@/lib/api";
import {
  compass,
  formatCalendarDate,
  formatDateTime,
  formatDuration,
  formatMoney,
  formatRange,
  heightRange,
  metres,
  relativeTime,
  seconds,
  type Units,
} from "@/lib/format";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { FlightSlice, MatchDetail, Offer, User } from "@/types/api";

function localTime(local: string): string {
  // Provider wall-clock time at the airport ("2026-08-08T10:40:00").
  const [d, t] = local.split("T");
  return `${formatCalendarDate(d, { weekday: "short" })} ${t?.slice(0, 5) ?? ""}`;
}

function SliceView({ title, slice }: { title: string; slice: FlightSlice }) {
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm font-medium">
        {title}: {slice.origin} → {slice.destination}
        <span className="text-muted-foreground">
          · {formatDuration(slice.duration_minutes)} · {slice.stops === 0 ? "non-stop" : `${slice.stops} stop${slice.stops > 1 ? "s" : ""}`}
        </span>
      </div>
      <ol className="space-y-1.5 border-l-2 border-secondary pl-4 text-xs">
        {slice.segments.map((s, i) => (
          <li key={i}>
            <span className="font-medium">{s.flight_number}</span> {s.carrier_name} · {s.origin} {localTime(s.departure_local)} → {s.destination}{" "}
            {localTime(s.arrival_local)} <span className="text-muted-foreground">({formatDuration(s.duration_minutes)}, local times)</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function OfferCard({ offer, best }: { offer: Offer; best: boolean }) {
  const [open, setOpen] = useState(best);
  const validated = offer.validation_status === "valid" || offer.validation_status === "price_changed";
  return (
    <div className={cn("rounded-2xl border bg-card p-4", best && "border-primary ring-1 ring-primary")} data-testid="offer-card">
      <div className="flex flex-wrap items-center gap-3">
        {best && <Badge>Best match</Badge>}
        <span className="font-display text-2xl font-semibold">{formatMoney(offer.price_converted ?? offer.price_per_traveler, offer.currency)}</span>
        <span className="text-xs text-muted-foreground">per traveller · total {formatMoney(offer.total_price, offer.currency)}</span>
        {offer.is_mock && <DemoBadge mock />}
        <span className="ml-auto text-sm text-muted-foreground">
          {offer.airline_names.join(", ")} · {formatDuration(offer.outbound.duration_minutes)} out / {formatDuration(offer.inbound.duration_minutes)} back
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>
          Arrive {localTime(offer.outbound.arrival_local)} ({offer.destination_timezone}) · Depart {localTime(offer.inbound.departure_local)}
        </span>
        <span className="flex items-center gap-1">
          <ShieldCheck className={cn("size-3.5", validated ? "text-emerald-600" : "text-muted-foreground")} />
          {validated && offer.last_validated_at
            ? `Price re-checked ${relativeTime(offer.last_validated_at)}${offer.validation_status === "price_changed" ? " (changed)" : ""}`
            : `Quoted ${relativeTime(offer.quoted_at)} by ${offer.provider_label}`}
        </span>
        {offer.baggage && (
          <span className="flex items-center gap-1">
            <Luggage className="size-3.5" /> {offer.baggage}
          </span>
        )}
      </div>
      <button type="button" className="mt-3 flex items-center gap-1 text-xs font-medium text-ocean" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        Itinerary <ChevronDown className={cn("size-3.5 transition", open && "rotate-180")} />
      </button>
      {open && (
        <div className="mt-3 space-y-4">
          <SliceView title="Outbound" slice={offer.outbound} />
          <SliceView title="Return" slice={offer.inbound} />
        </div>
      )}
      <div className="mt-4 flex flex-wrap gap-2">
        {offer.links.map((l) => (
          <Button key={l.url} asChild variant="outline" size="sm">
            <a href={l.url} target="_blank" rel="noopener noreferrer">
              {l.label} <ExternalLink />
            </a>
          </Button>
        ))}
      </div>
    </div>
  );
}

const SCORE_LABELS: Record<string, string> = {
  surf: "Surf quality",
  affordability: "Travel affordability",
  confidence: "Forecast confidence",
  convenience: "Trip convenience",
};

function Detail({ user, m }: { user: User; m: MatchDetail }) {
  const units = (user.profile.units as Units) ?? "ft";
  const tz = m.spot.timezone;
  const queryClient = useQueryClient();
  const refresh = useMutation({
    mutationFn: () => post<MatchDetail>(`/api/opportunities/${m.id}/refresh-flights`),
    onSuccess: (data) => {
      queryClient.setQueryData(qk.opportunity(m.id), data);
      toast.success("Flight check requested — results update within a few minutes.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const dismiss = useMutation({
    mutationFn: () => post(`/api/opportunities/${m.id}/dismiss`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.opportunity(m.id) });
      void queryClient.invalidateQueries({ queryKey: ["opportunities"] });
      toast.success("Opportunity dismissed.");
    },
  });
  const e = m.event;
  const transfer = m.spot_airports.find((a) => a.airport.iata === m.destination_iata);

  return (
    <>
      <PageHeader
        eyebrow={
          <Link href={`/spots/${m.spot.slug}`} className="hover:underline">
            {m.spot.region} · {m.spot.country}
          </Link>
        }
        title={`${m.spot.name}, ${formatRange(m.window_start, m.window_end, tz)}`}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <MatchStatusBadge status={m.status} />
            <QualityBadge label={m.peak_label} score={m.peak_score} />
            <ConfidenceBadge label={m.confidence_label} value={m.confidence} />
            {m.is_demo && <DemoBadge />}
            {m.has_mock_flights && <DemoBadge mock />}
            <span className="text-xs">from your search “{m.search_name}”</span>
          </span>
        }
        actions={
          <>
            <Button variant="outline" onClick={() => refresh.mutate()} disabled={refresh.isPending || m.status === "expired" || m.status === "dismissed"}>
              <RefreshCw className={cn(refresh.isPending && "animate-spin")} /> Re-check flights
            </Button>
            {m.status !== "dismissed" && (
              <Button variant="ghost" onClick={() => dismiss.mutate()}>
                <XCircle /> Dismiss
              </Button>
            )}
          </>
        }
      />

      {m.status === "surf_only" && (
        <Alert variant="warn" className="mb-6">
          <Waves />
          <AlertTitle>Surf matches — no eligible flight yet</AlertTitle>
          <AlertDescription>{m.status_reason ?? "No itinerary meets your budget and schedule."} We&apos;ll keep checking.</AlertDescription>
        </Alert>
      )}
      {m.status === "pending_flights" && (
        <Alert variant="info" className="mb-6">
          <Plane />
          <AlertTitle>Checking flights</AlertTitle>
          <AlertDescription>The swell matches; flight search is queued. {m.flight_search_error}</AlertDescription>
        </Alert>
      )}
      {(m.status === "expired" || m.status === "dismissed") && (
        <Alert variant="default" className="mb-6">
          <Info />
          <AlertDescription>{m.status_reason ?? "This opportunity is no longer active."}</AlertDescription>
        </Alert>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Swell event overview</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-6 sm:grid-cols-2">
              <div className="space-y-2 text-sm">
                <div className="font-display text-3xl font-semibold">{heightRange(m.breaking_height_min_ft, m.breaking_height_max_ft, units)}</div>
                <div className="text-xs text-muted-foreground">estimated breaking faces at peak (approximate)</div>
                <div>
                  Swell {metres(e.peak_swell_height_m)} @ {seconds(e.peak_swell_period_s)} from {compass(e.peak_swell_direction_deg)}
                </div>
                <div>
                  Wind {e.peak_wind_speed_kmh != null ? `${Math.round(e.peak_wind_speed_kmh)} km/h` : "—"} from {compass(e.peak_wind_direction_deg)} ·{" "}
                  {WIND_RELATION_LABEL[e.peak_wind_relation] ?? e.peak_wind_relation}
                </div>
                <div>
                  Best surf window: <strong>{formatDateTime(m.window_start, tz)}</strong> → <strong>{formatDateTime(m.window_end, tz)}</strong> ({tz})
                </div>
                <div className="text-muted-foreground">
                  {m.qualifying_hours} daylight hours meet your criteria · event v{e.version}, updated {relativeTime(e.last_updated_at)}
                </div>
              </div>
              <div className="space-y-3">
                <div className="flex items-center gap-3">
                  <ScoreRing score={m.overall_score} size={64} />
                  <div>
                    <div className="font-semibold">Overall match</div>
                    <div className="text-xs text-muted-foreground">Heuristic index — not a probability.</div>
                  </div>
                </div>
                {Object.entries(m.scores).map(([k, v]) =>
                  v == null ? null : (
                    <div key={k}>
                      <div className="mb-1 flex justify-between text-xs">
                        <span>{SCORE_LABELS[k] ?? k}</span>
                        <span className="font-medium tabular-nums">{Math.round(Number(v))}/100</span>
                      </div>
                      <Progress value={Number(v)} />
                    </div>
                  ),
                )}
              </div>
            </CardContent>
          </Card>

          {m.daily.length > 0 && (
            <section>
              <SectionTitle>Daily conditions around your trip</SectionTitle>
              <DailyStrip daily={m.daily} units={units} />
            </section>
          )}

          <section>
            <SectionTitle>Flight options</SectionTitle>
            {m.offers.length === 0 ? (
              <p className="text-sm text-muted-foreground">No eligible offers stored for this opportunity.</p>
            ) : (
              <div className="space-y-3">
                {m.offers.map((o, i) => (
                  <OfferCard key={o.id} offer={o} best={i === 0} />
                ))}
              </div>
            )}
            {m.inspection_links.length > 0 && (
              <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
                <span className="text-muted-foreground">Compare elsewhere:</span>
                {m.inspection_links.map((l) => (
                  <a key={l.url} href={l.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-ocean hover:underline">
                    {l.label} <ExternalLink className="size-3" />
                  </a>
                ))}
              </div>
            )}
          </section>
        </div>

        <aside className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <CalendarCheck className="size-4 text-ocean" /> Recommended trip
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-xl bg-secondary p-3">
                  <div className="text-xs text-muted-foreground">Arrive</div>
                  <div className="font-display text-xl font-semibold">{formatCalendarDate(m.recommended_arrival_date)}</div>
                </div>
                <div className="rounded-xl bg-secondary p-3">
                  <div className="text-xs text-muted-foreground">Leave</div>
                  <div className="font-display text-xl font-semibold">{formatCalendarDate(m.recommended_departure_date)}</div>
                </div>
              </div>
              {m.best_price && (
                <div className="flex items-center justify-between rounded-xl border p-3">
                  <span className="flex items-center gap-2">
                    <Plane className="size-4 text-ocean" /> {m.origin_iata} <ArrowRight className="size-3" /> {m.destination_iata}
                  </span>
                  <span className="font-semibold">{formatMoney(m.best_price, m.currency)}</span>
                </div>
              )}
              {m.travel_window && (
                <div className="space-y-1 text-xs text-muted-foreground">
                  <div>
                    Land at {m.destination_iata} between {formatDateTime(m.travel_window.arrive_after, m.travel_window.destination_timezone)} and{" "}
                    {formatDateTime(m.travel_window.arrive_by, m.travel_window.destination_timezone)}
                  </div>
                  <div>
                    Fly home between {formatDateTime(m.travel_window.depart_after, m.travel_window.destination_timezone)} and{" "}
                    {formatDateTime(m.travel_window.depart_before, m.travel_window.destination_timezone)}
                  </div>
                  <div>All times local to {m.travel_window.destination_timezone}.</div>
                </div>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Airport & ground transfer</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {transfer ? (
                <>
                  <div className="font-medium">
                    {transfer.airport.name} ({transfer.airport.iata})
                  </div>
                  <div className="text-muted-foreground">
                    ~{Math.round(transfer.transfer_minutes / 6) / 10} h by {transfer.transfer_mode.replaceAll("_", " ").replace("+", " + ")} to the break. {transfer.notes}
                  </div>
                </>
              ) : (
                <div className="text-muted-foreground">No practical airport within your limits.</div>
              )}
              <p className="text-muted-foreground">{m.accessibility_notes}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <AlertTriangle className="size-4 text-amber-600" /> Uncertainty & limitations
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ul className="list-disc space-y-2 pl-4 text-xs text-muted-foreground">
                {m.limitations.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            </CardContent>
          </Card>
        </aside>
      </div>
    </>
  );
}

function Loader({ user, id }: { user: User; id: string }) {
  const { data, isLoading, error } = useOpportunity(id);
  if (isLoading) return <Skeleton className="h-96" />;
  if (error || !data) return <p className="text-muted-foreground">Opportunity not found.</p>;
  return <Detail user={user} m={data} />;
}

export default function OpportunityPage() {
  const { id } = useParams<{ id: string }>();
  return (
    <AppShell>
      <RequireAuth>{(user) => <Loader user={user} id={id} />}</RequireAuth>
    </AppShell>
  );
}
