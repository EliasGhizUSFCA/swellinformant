"use client";

/**
 * The admin freshness dashboard (spec section 16, design D12): GET /api/admin/freshness,
 * grouped as provider and models, detection to publication, problems, the freshness timeline,
 * stale spots, failures and refresh jobs, API usage against the limits, processing times and
 * latencies. Read on mount, on focus and when the published forecast version changes
 * (./use-admin-freshness.ts); never on a timer.
 */
import { LineChart, RefreshCw, ShieldCheck } from "lucide-react";
import Link from "next/link";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { relativeTime } from "@/lib/format";

import { utc } from "./freshness-format";
import {
  FailuresCard,
  ProblemsCard,
  ProviderCard,
  PublicationCard,
  RefreshJobsCard,
  StaleSpotsCard,
  TimelineCard,
  TimesCard,
  UsageCard,
} from "./freshness-sections";
import type { AdminFreshness } from "./freshness-types";
import { useAdminFreshness } from "./use-admin-freshness";

export function FreshnessView({ data }: { data: AdminFreshness }) {
  return (
    <div className="space-y-6" data-testid="freshness-dashboard">
      <ProblemsCard data={data} />
      <div className="grid gap-6 lg:grid-cols-2 [&>*]:min-w-0">
        <ProviderCard data={data} />
        <PublicationCard data={data} />
      </div>
      <TimelineCard data={data} />
      <div className="grid gap-6 lg:grid-cols-2 [&>*]:min-w-0">
        <StaleSpotsCard data={data} />
        <FailuresCard data={data} />
      </div>
      <RefreshJobsCard data={data} />
      <div className="grid gap-6 lg:grid-cols-2 [&>*]:min-w-0">
        <UsageCard data={data} />
        <TimesCard data={data} />
      </div>
      {data.notes.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs wrap-anywhere text-muted-foreground" data-testid="freshness-notes">
          {data.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function FreshnessDashboard() {
  const { report, versionId, versionKnown } = useAdminFreshness(true);
  const { data, error, isFetching, refetch } = report;
  if (error instanceof ApiError && error.status === 403) return <NotAdmin />;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted-foreground">
        <span data-testid="freshness-checked">
          {data ? (
            <>
              Checked {utc(data.checked_at)} ({relativeTime(data.checked_at)})
            </>
          ) : (
            "Loading…"
          )}
        </span>
        <span data-testid="freshness-version">
          {versionKnown ? (versionId == null ? "No forecast version published" : `Forecast version #${versionId}`) : "Version updates unavailable: reloads on focus"}
        </span>
        <Button variant="outline" size="sm" onClick={() => void refetch()} disabled={isFetching} data-testid="freshness-reload">
          <RefreshCw className={isFetching ? "animate-spin" : undefined} /> Reload
        </Button>
        <Link href="/status" className="inline-flex items-center gap-1.5 font-medium text-ocean hover:underline">
          <ShieldCheck className="size-4" /> Public status page
        </Link>
        <Link href="/status/accuracy" className="inline-flex items-center gap-1.5 font-medium text-ocean hover:underline">
          <LineChart className="size-4" /> Forecast accuracy
        </Link>
      </div>
      {error && !data && (
        <Alert variant="danger">
          <AlertTitle>The freshness report could not be loaded</AlertTitle>
          <AlertDescription>{error instanceof Error ? error.message : "Try again in a minute."}</AlertDescription>
        </Alert>
      )}
      {!data && !error && <Skeleton className="h-96" />}
      {data && <FreshnessView data={data} />}
    </div>
  );
}

export function NotAdmin() {
  return (
    <Alert data-testid="freshness-forbidden">
      <AlertTitle>Admins only</AlertTitle>
      <AlertDescription>
        The freshness dashboard is for admin accounts. Forecast freshness for everyone is on the{" "}
        <Link href="/status" className="font-medium text-ocean hover:underline">
          public status page
        </Link>
        .
      </AlertDescription>
    </Alert>
  );
}
