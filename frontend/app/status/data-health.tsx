"use client";

/**
 * Data health on the status page: GET /api/health/data (backend app/services/health.py).
 *
 * Everything shown is what the backend reports: forecast-source ages against each
 * source's own stale limit, the last result of every scheduled job, map-layer and buoy
 * freshness, the model-run sync (components/status/sync-status.tsx), and an overall
 * ok / degraded / failing status with plain reasons. The limits are operational heuristics
 * (how late data may be), not forecast accuracy.
 */
import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, CheckCircle2, Database, HeartPulse, Map as MapIcon, Radio, XCircle } from "lucide-react";

import { SyncStatus } from "@/components/status/sync-status";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { get } from "@/lib/api";
import { relativeTime, titleCase } from "@/lib/format";
import type { SyncHealth } from "@/lib/sync";

export type OverallStatus = "ok" | "degraded" | "failing";
type Freshness = "ok" | "stale" | "missing";

export interface SourceHealth {
  code: string;
  name: string;
  role: "forecast" | "blend" | "ensemble";
  status: Freshness;
  newest_run_key: string | null;
  newest_issued_at: string | null;
  age_hours: number | null;
  stale_after_hours: number | null;
  latest_attempt_status: string | null;
  latest_attempt_started_at: string | null;
}

export interface JobHealth {
  job: string;
  schedule: string;
  status: "ok" | "failed" | "overdue" | "stuck" | "running" | "never_run";
  last_status: string | null;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_duration_ms: number | null;
  last_success_at: string | null;
  consecutive_failures: number;
  overdue_after_minutes: number | null;
}

export interface DataHealth {
  status: OverallStatus;
  checked_at: string;
  reasons: string[];
  problems: Array<{ key: string; severity: "degraded" | "failing"; message: string }>;
  forecast_sources: SourceHealth[];
  jobs: JobHealth[];
  map_fields: {
    mode: "with_noaa" | "standalone" | "off";
    status: Freshness | "off";
    run_key: string | null;
    cycle_time: string | null;
    age_hours: number | null;
    stale_after_hours: number | null;
    complete_valid_times: number;
    last_valid_time: string | null;
  };
  observations: {
    enabled: boolean;
    providers: Array<{
      provider: string;
      status: Freshness;
      active_stations: number;
      newest_observed_at: string | null;
      age_hours: number | null;
      stale_after_hours: number;
    }>;
  };
  heartbeat: { last_job_finished_at: string | null; age_minutes: number | null; limit_minutes: number };
  storage: { database_bytes: number; database_max_gb: number | null; redis?: RedisHealth | null };
  /** The synced pipeline (runner, last check, syncs, components, CPU); absent from older backends. */
  sync?: SyncHealth | null;
}

/** Redis (job queue, locks, call budgets, provider cache) memory against its cap. */
export interface RedisHealth {
  available: boolean;
  used_memory_bytes?: number | null;
  maxmemory_bytes?: number | null;
  maxmemory_policy?: string | null;
  cap_bytes?: number | null;
  cap_source?: "redis" | "setting" | null;
  used_fraction?: number | null;
  alert_fraction: number;
}

/** "Redis 1 MB of 256 MB (0.4 %, alarm above 80 %)"; the cap is labelled when it is only the
 * configured REDIS_MAXMEMORY because this Redis runs without one. */
export function redisLine(r: RedisHealth): string {
  if (!r.available) return "Redis is not answering";
  const used = r.used_memory_bytes ?? null;
  if (used === null) return "Redis memory unknown";
  const alarm = `alarm above ${Math.round(r.alert_fraction * 100)} %`;
  if (!r.cap_bytes || r.used_fraction == null) return `Redis ${formatBytes(used)} (no memory cap known, ${alarm})`;
  const pct = r.used_fraction * 100;
  const share = `${pct < 10 ? pct.toFixed(1) : Math.round(pct)} %`;
  const cap = r.cap_source === "setting" ? `${formatBytes(r.cap_bytes)} (REDIS_MAXMEMORY; Redis itself runs uncapped)` : formatBytes(r.cap_bytes);
  return `Redis ${formatBytes(used)} of ${cap} (${share}, ${alarm})`;
}

/** "184 MB", "12.3 GB" (binary units, as PostgreSQL's pg_size_pretty). */
export function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  return `${Math.round(bytes / 1024 ** 2)} MB`;
}

export const useDataHealth = () =>
  useQuery({
    queryKey: ["health", "data"],
    queryFn: ({ signal }) => get<DataHealth>("/api/health/data", signal),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

type Tone = "good" | "warn" | "danger" | "muted" | "info";

export function overallTone(status: OverallStatus): { tone: Tone; alert: "success" | "warn" | "danger"; label: string } {
  if (status === "ok") return { tone: "good", alert: "success", label: "All data checks pass" };
  if (status === "degraded") return { tone: "warn", alert: "warn", label: "Degraded" };
  return { tone: "danger", alert: "danger", label: "Failing" };
}

const TONES: Record<string, Tone> = {
  ok: "good",
  running: "info",
  stale: "warn",
  overdue: "warn",
  missing: "danger",
  failed: "danger",
  stuck: "danger",
};

/** Badge colour for a source, job, layer or provider state ("never_run", "off": muted). */
export const freshnessTone = (status: string): Tone => TONES[status] ?? "muted";

/** "3.4 h", "45 min", "2.1 days" for an age in hours. */
export function formatHours(hours: number | null): string {
  if (hours === null || Number.isNaN(hours)) return "—";
  if (hours < 1.5) return `${Math.max(0, Math.round(hours * 60))} min`;
  if (hours < 48) return `${hours.toFixed(1)} h`;
  return `${(hours / 24).toFixed(1)} days`;
}

export function ageLine(s: Pick<SourceHealth, "age_hours" | "stale_after_hours" | "newest_run_key" | "role">): string {
  if (s.newest_run_key === null || s.age_hours === null) return "No successful run yet";
  const limit = s.stale_after_hours === null ? "stale when any member is stale" : `stale after ${formatHours(s.stale_after_hours)}`;
  return `Run ${s.newest_run_key} · issued ${formatHours(s.age_hours)} ago · ${limit}`;
}

const ROLE_LABEL: Record<SourceHealth["role"], string> = {
  forecast: "spot forecast source",
  blend: "multi-model blend",
  ensemble: "ensemble spread (uncertainty only)",
};

export function DataHealthSummary({ data }: { data: DataHealth }) {
  const { alert, label } = overallTone(data.status);
  const Icon = data.status === "ok" ? CheckCircle2 : data.status === "degraded" ? AlertTriangle : XCircle;
  return (
    <Alert variant={alert} className="mb-6" data-testid="data-health-summary">
      <Icon />
      <AlertTitle>
        Data health: {label}
        <span className="ml-2 text-xs font-normal opacity-80">checked {relativeTime(data.checked_at)}</span>
      </AlertTitle>
      <AlertDescription>
        {data.reasons.length === 0 ? (
          <p>Every forecast source is within its freshness limit and every scheduled job is running.</p>
        ) : (
          <ul className="list-disc space-y-1 pl-4" data-testid="data-health-reasons">
            {data.problems.map((p) => (
              <li key={p.key}>
                {p.severity === "failing" && <strong>Failing: </strong>}
                {p.message}
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-xs opacity-80">
          Freshness limits are operational checks of how late data may be, not a measure of forecast accuracy.
        </p>
      </AlertDescription>
    </Alert>
  );
}

export function SourceFreshness({ sources }: { sources: SourceHealth[] }) {
  return (
    <div className="space-y-3" data-testid="data-health-sources">
      {sources.map((s) => (
        <div key={s.code} className="rounded-xl border p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">{s.name}</span>
            <Badge variant={freshnessTone(s.status)}>{s.status === "ok" ? "fresh" : s.status}</Badge>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">{ROLE_LABEL[s.role]}</div>
          <div className="mt-1 text-xs text-muted-foreground">{ageLine(s)}</div>
          {s.latest_attempt_status === "failed" && s.latest_attempt_started_at && (
            <div className="mt-1 text-xs text-destructive">Latest attempt {relativeTime(s.latest_attempt_started_at)} failed</div>
          )}
        </div>
      ))}
    </div>
  );
}

export function JobTable({ jobs }: { jobs: JobHealth[] }) {
  return (
    <table className="w-full text-sm" data-testid="data-health-jobs">
      <thead>
        <tr className="border-b text-left text-xs text-muted-foreground">
          <th className="py-1 font-normal">Job</th>
          <th className="py-1 font-normal">State</th>
          <th className="py-1 text-right font-normal">Last run</th>
        </tr>
      </thead>
      <tbody>
        {jobs.map((j) => (
          <tr key={j.job} className="border-b align-top last:border-0">
            <td className="py-2">
              <div className="font-mono text-xs wrap-anywhere">{j.job}</div>
              <div className="text-xs text-muted-foreground">{j.schedule}</div>
            </td>
            <td className="py-2">
              <Badge variant={freshnessTone(j.status)}>{titleCase(j.status)}</Badge>
              {j.consecutive_failures > 1 && <div className="mt-1 text-xs text-destructive">{j.consecutive_failures} failures in a row</div>}
            </td>
            <td className="py-2 text-right text-xs text-muted-foreground">
              {j.last_started_at ? relativeTime(j.last_started_at) : "—"}
              {j.last_duration_ms !== null && <div>{(j.last_duration_ms / 1000).toFixed(1)} s</div>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function LayersAndObservations({ data }: { data: DataHealth }) {
  const m = data.map_fields;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MapIcon className="size-4 text-ocean" /> Map layers, buoys and workers
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm" data-testid="data-health-other">
        <div className="flex items-start justify-between gap-2">
          <div>
            <div className="font-medium">Map ocean layers (offshore model fields, not surf height)</div>
            <div className="text-xs text-muted-foreground">
              {m.mode === "off"
                ? "Switched off in this configuration"
                : m.cycle_time
                  ? `GFS cycle ${m.run_key ?? ""} · ${formatHours(m.age_hours)} old · stale after ${formatHours(m.stale_after_hours)} · ${m.complete_valid_times} time steps`
                  : "No complete field set stored yet"}
            </div>
          </div>
          <Badge variant={freshnessTone(m.status)}>{m.status === "ok" ? "fresh" : m.status}</Badge>
        </div>
        <div>
          <div className="flex items-center gap-2 font-medium">
            <Radio className="size-4 text-ocean" /> Buoy observations
          </div>
          {!data.observations.enabled ? (
            <div className="text-xs text-muted-foreground">Not enabled (OBSERVATIONS_ENABLED=false)</div>
          ) : (
            <ul className="mt-1 space-y-1">
              {data.observations.providers.map((p) => (
                <li key={p.provider} className="flex items-center justify-between gap-2 text-xs">
                  <span>
                    {p.provider.toUpperCase()} · {p.active_stations} active station{p.active_stations === 1 ? "" : "s"} ·{" "}
                    {p.newest_observed_at ? `newest ${formatHours(p.age_hours)} ago` : "none stored"} · stale after {formatHours(p.stale_after_hours)}
                  </span>
                  <Badge variant={freshnessTone(p.status)}>{p.status === "ok" ? "fresh" : p.status}</Badge>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex items-start gap-2">
          <HeartPulse className="mt-0.5 size-4 text-ocean" />
          <div className="text-xs text-muted-foreground">
            {data.heartbeat.last_job_finished_at
              ? `Last background job finished ${relativeTime(data.heartbeat.last_job_finished_at)} (alarm after ${data.heartbeat.limit_minutes} min of silence)`
              : "No background job has finished yet"}
          </div>
        </div>
        <div className="flex items-start gap-2">
          <Database className="mt-0.5 size-4 text-ocean" />
          <div className="text-xs text-muted-foreground">
            Database {formatBytes(data.storage.database_bytes)}
            {data.storage.database_max_gb !== null ? ` (alarm above ${data.storage.database_max_gb} GB)` : " (no size alarm configured)"}
            {data.storage.redis && <div data-testid="data-health-redis">{redisLine(data.storage.redis)}</div>}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function JobsCard({ jobs }: { jobs: JobHealth[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Activity className="size-4 text-ocean" /> Background jobs
        </CardTitle>
      </CardHeader>
      <CardContent>
        <JobTable jobs={jobs} />
      </CardContent>
    </Card>
  );
}

/** The sync section; nothing when the backend sends no sync block (older backends). */
export function SyncCard({ data, className }: { data: DataHealth; className?: string }) {
  if (!data.sync) return null;
  const names = new Map(data.forecast_sources.map((s) => [s.code, s.name] as const));
  return <SyncStatus sync={data.sync} sourceNames={names} className={className} />;
}
