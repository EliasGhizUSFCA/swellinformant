"use client";

import { Activity, Database, LineChart } from "lucide-react";
import Link from "next/link";

import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useMe, useSystemStatus } from "@/hooks/queries";
import { relativeTime, titleCase } from "@/lib/format";

import { DataHealthSummary, JobsCard, LayersAndObservations, SourceFreshness, useDataHealth } from "./data-health";

export default function StatusPage() {
  const { data, isLoading } = useSystemStatus();
  const { data: health, isError: healthFailed } = useDataHealth();
  const { data: me } = useMe();
  return (
    <AppShell>
      <PageHeader eyebrow="System" title="Status & data freshness" description="Background workers keep monitoring whether or not anyone has the site open." />
      {me?.is_admin && (
        <Link href="/status/accuracy" className="mb-4 inline-flex items-center gap-2 text-sm font-medium text-ocean hover:underline" data-testid="accuracy-link">
          <LineChart className="size-4" /> Forecast accuracy dashboard (developers)
        </Link>
      )}
      {health && <DataHealthSummary data={health} />}
      {healthFailed && (
        <Alert variant="warn" className="mb-6">
          <AlertTitle>Data health could not be loaded</AlertTitle>
          <AlertDescription>The per-source and per-job checks below may be incomplete; try again in a minute.</AlertDescription>
        </Alert>
      )}
      {isLoading && <Skeleton className="h-64" />}
      {data && (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Database className="size-4 text-ocean" /> Forecast sources
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {health ? (
                <SourceFreshness sources={health.forecast_sources} />
              ) : (
                data.forecast.sources.map((s) => (
                  <div key={s.code} className="rounded-xl border p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{s.name}</span>
                      <Badge variant={s.stale ? "warn" : "good"}>{s.stale ? "stale" : "fresh"}</Badge>
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {s.latest_successful
                        ? `Latest run ${s.latest_successful.run_key} issued ${relativeTime(s.latest_successful.issued_at)} · ${s.latest_successful.spot_count} spots`
                        : "No successful run yet"}
                    </div>
                    {s.latest_attempt?.error && <div className="mt-1 text-xs text-destructive">Last attempt: {s.latest_attempt.error}</div>}
                  </div>
                ))
              )}
              <div className="text-xs text-muted-foreground">
                Providers: forecasts {data.mode.forecast_providers.join(", ")} · flights {data.mode.flight_provider} · email {data.mode.email_provider} · SMS{" "}
                {data.mode.sms_provider}
              </div>
              <div className="text-xs text-muted-foreground">
                Alert lead time {data.detection.lead_min_days}–{data.detection.lead_max_days} days · events from score ≥ {data.detection.event_min_score}
              </div>
            </CardContent>
          </Card>
          {health ? (
            <JobsCard jobs={health.jobs} />
          ) : (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Activity className="size-4 text-ocean" /> Background jobs
                </CardTitle>
              </CardHeader>
              <CardContent>
                <table className="w-full text-sm">
                  <tbody>
                    {data.jobs.map((j) => (
                      <tr key={j.job} className="border-b last:border-0">
                        <td className="py-2 font-mono text-xs">{j.job}</td>
                        <td className="py-2">
                          <Badge variant={j.status === "success" ? "good" : j.status === "failed" ? "danger" : "muted"}>{titleCase(j.status)}</Badge>
                        </td>
                        <td className="py-2 text-right text-xs text-muted-foreground">{j.started_at ? relativeTime(j.started_at) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          )}
          {health && <LayersAndObservations data={health} />}
        </div>
      )}
    </AppShell>
  );
}
