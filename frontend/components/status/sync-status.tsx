"use client";

/**
 * The status page's sync section (`GET /api/health/data` → `.sync`): which model run the site
 * last published, whether the runner is still checking for new runs, the last sync with its
 * stages, each component's state at the last check and, when SYNC_CPU_BUDGET_HOURS is set, the
 * CPU used this month against it. Older backends send no sync block: nothing is rendered.
 */
import { Cpu, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { relativeTime } from "@/lib/format";
import { cycleLabel } from "@/lib/map-time";
import {
  componentKindLabel,
  componentState,
  CPU_BUDGET_ALERT_FRACTION,
  cpuBudget,
  formatCpuHours,
  formatDurationMs,
  isSameRun,
  publicationLine,
  runDurationMs,
  runnerLabel,
  runStatus,
  stageLabel,
  stageTone,
  type SyncComponent,
  type SyncHealth,
  type SyncRun,
  triggerLabel,
} from "@/lib/sync";
import { cn } from "@/lib/utils";

const METER_TONE = { good: "bg-emerald-500", warn: "bg-amber-500", danger: "bg-red-500", muted: "bg-muted-foreground", info: "bg-sky-500" } as const;

const STAGE_PILL = {
  good: "border-emerald-200 bg-emerald-50 text-emerald-800",
  muted: "border-transparent bg-muted text-muted-foreground",
  danger: "border-red-200 bg-red-50 text-red-700",
  warn: "border-amber-200 bg-amber-50 text-amber-800",
  info: "border-sky-200 bg-sky-50 text-sky-700",
} as const;

function LastCheck({ sync, now }: { sync: SyncHealth; now: Date | undefined }) {
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground" data-testid="sync-last-check">
      <span>
        {runnerLabel(sync.runner)} · checks every {sync.check_minutes} min ·{" "}
        {sync.last_check_at ? `last check ${relativeTime(sync.last_check_at, now)}` : "no check recorded yet"}
      </span>
      {sync.heartbeat_stale && <Badge variant="danger">Not checking</Badge>}
    </p>
  );
}

function Published({ run, now }: { run: SyncRun | null | undefined; now: Date | undefined }) {
  if (!run?.published_at) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="sync-published">
        Nothing published yet.
      </p>
    );
  }
  return (
    <p className="text-sm font-medium text-ink" data-testid="sync-published">
      {publicationLine(run.anchor_cycle, run.published_at)}
      <span className="ml-1.5 text-xs font-normal text-muted-foreground">({relativeTime(run.published_at, now)})</span>
    </p>
  );
}

export function SyncStages({ run }: { run: SyncRun }) {
  const stages = run.stages ?? [];
  if (stages.length === 0) return <p className="text-xs text-muted-foreground">No stage finished yet.</p>;
  return (
    <ol className="flex flex-wrap gap-1.5" data-testid="sync-stages" aria-label={`Stages of sync ${run.id}`}>
      {stages.map((s, i) => {
        const tone = stageTone(s.status);
        const value = s.status === "success" ? formatDurationMs(s.duration_ms) : s.status;
        return (
          <li
            key={`${s.name}-${i}`}
            data-status={s.status}
            className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] whitespace-nowrap", STAGE_PILL[tone])}
          >
            <span className="font-medium">{stageLabel(s.name)}</span>
            <span className="tabular-nums opacity-80">{value}</span>
          </li>
        );
      })}
    </ol>
  );
}

function LastRun({ run, published, now }: { run: SyncRun; published: boolean; now: Date | undefined }) {
  const { label, tone } = runStatus(run.status);
  const took = runDurationMs(run, now?.getTime());
  return (
    <div className="space-y-2" data-testid="sync-last-run">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <span className="font-medium">{published ? "Last sync" : "Newest sync"}</span>
        <Badge variant={tone}>{label}</Badge>
        <span className="text-xs text-muted-foreground">
          #{run.id} · {triggerLabel(run.trigger)} · started {relativeTime(run.started_at, now)}
          {took !== null && ` · ${run.status === "running" ? "running for" : "took"} ${formatDurationMs(took)}`}
        </span>
      </div>
      <SyncStages run={run} />
    </div>
  );
}

function ComponentRow({ c, names }: { c: SyncComponent; names: ReadonlyMap<string, string> }) {
  const { label, tone } = componentState(c.state);
  const keys = [c.stored_run_key && `stored ${c.stored_run_key}`, c.available_run_key && `available ${c.available_run_key}`].filter(Boolean).join(" · ");
  return (
    <li className="py-2 first:pt-0 last:pb-0" data-testid={`sync-component-${c.kind}-${c.source}`} data-state={c.state}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-sm font-medium">{componentKindLabel(c.kind)}</div>
          <div className="text-xs text-muted-foreground wrap-anywhere" title={keys || undefined}>
            {names.get(c.source) ?? c.source}
            {c.available_cycle && ` · ${cycleLabel(c.available_cycle)} run`}
          </div>
        </div>
        <Badge variant={tone} className="shrink-0">
          {label}
        </Badge>
      </div>
      {c.detail && (
        <p className="mt-1 line-clamp-3 text-xs text-muted-foreground wrap-anywhere" title={c.detail}>
          {c.detail}
        </p>
      )}
    </li>
  );
}

export function CpuMeter({ sync }: { sync: SyncHealth }) {
  const cpu = cpuBudget(sync);
  if (!cpu) return null;
  const pct = Math.round(cpu.fraction * 100);
  return (
    <div className="space-y-1.5" data-testid="sync-cpu">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 text-xs">
        <span className="flex items-center gap-1.5 font-medium text-ink">
          <Cpu className="size-3.5 text-ocean" aria-hidden /> CPU this month (UTC)
        </span>
        <span className="text-muted-foreground tabular-nums">
          at least {formatCpuHours(cpu.used)} of {formatCpuHours(cpu.budget)} ({pct} %)
        </span>
      </div>
      <div
        role="meter"
        aria-label="CPU used this month against the budget"
        aria-valuemin={0}
        aria-valuemax={cpu.budget}
        aria-valuenow={Math.min(cpu.used, cpu.budget)}
        aria-valuetext={`${formatCpuHours(cpu.used)} of ${formatCpuHours(cpu.budget)}, ${pct} %`}
        data-tone={cpu.tone}
        className="relative h-2 overflow-hidden rounded-full bg-muted"
      >
        <div className={cn("h-full rounded-full", METER_TONE[cpu.tone])} style={{ width: `${Math.min(100, cpu.fraction * 100)}%` }} />
        {/* The alert threshold (data health turns degraded above it). */}
        <div className="absolute inset-y-0 w-px bg-ink/40" style={{ left: `${CPU_BUDGET_ALERT_FRACTION * 100}%` }} aria-hidden />
      </div>
      <p className="text-[11px] text-muted-foreground">Budget SYNC_CPU_BUDGET_HOURS · alert above {Math.round(CPU_BUDGET_ALERT_FRACTION * 100)} %</p>
    </div>
  );
}

/**
 * The sync card. `sourceNames` maps forecast source codes to their names (from the same data
 * health report); codes without a name are shown as they are.
 */
export function SyncStatus({
  sync,
  sourceNames,
  now,
  className,
}: {
  sync: SyncHealth | null | undefined;
  sourceNames?: ReadonlyMap<string, string>;
  /** Epoch ms for relative times and a running sync's duration (tests pin it); default: now. */
  now?: number;
  className?: string;
}) {
  if (!sync) return null;
  const at = now === undefined ? undefined : new Date(now);
  const names = sourceNames ?? new Map<string, string>();
  const components = sync.components ?? [];
  const lastRun = sync.last_run ?? null;
  return (
    <Card className={className} data-testid="sync-status">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <RefreshCw className="size-4 text-ocean" aria-hidden /> Forecast sync
        </CardTitle>
        <p className="text-xs text-muted-foreground">Each new model run is fetched and published in one sync: spots and map together.</p>
      </CardHeader>
      <CardContent className="grid gap-5 lg:grid-cols-2">
        <div className="min-w-0 space-y-4">
          <div className="space-y-1">
            <Published run={sync.last_published} now={at} />
            <LastCheck sync={sync} now={at} />
          </div>
          {lastRun ? (
            <LastRun run={lastRun} published={isSameRun(lastRun, sync.last_published)} now={at} />
          ) : (
            <p className="text-sm text-muted-foreground" data-testid="sync-last-run">
              No sync has run yet.
            </p>
          )}
          <CpuMeter sync={sync} />
        </div>
        <div className="min-w-0">
          <h4 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">At the last check</h4>
          {components.length > 0 ? (
            <ul className="divide-y" data-testid="sync-components">
              {components.map((c) => (
                <ComponentRow key={`${c.kind}-${c.source}`} c={c} names={names} />
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground" data-testid="sync-components">
              {sync.runner === "celery" ? "The Celery runner reports no component states; its checks are the sync.tick job." : "No check reported yet."}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
