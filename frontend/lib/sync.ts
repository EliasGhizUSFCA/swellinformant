/**
 * The synced data pipeline as the status page shows it: `GET /api/health/data` → `.sync`
 * (contract: backend/app/schemas/sync.py, filled by app/services/health.py).
 *
 * One sync runs when a provider publishes a new model run: spot forecasts, ensemble, world map
 * layers, predictions and alerts, then one commit publishes them together. Everything here is
 * what the backend reports: run keys, model cycles, timestamps, stage durations and states.
 * Older backends send no `sync` block; callers then show nothing.
 */
import type { BadgeVariant as Tone } from "@/lib/delivery";
import { titleCase } from "@/lib/format";
import { cycleLabel } from "@/lib/map-time";
import type { components } from "@/types/openapi";

/** A model run a sync stored, per component; `cycle` is the newest model cycle of that run. */
type SyncStoredRun = components["schemas"]["SyncStoredRunOut"];
export type SyncComponent = components["schemas"]["SyncComponentOut"];
export type SyncRun = components["schemas"]["SyncRunOut"];
/** `cpu_alert_fraction`: data health reports the runner as degraded above this share of
 * SYNC_CPU_BUDGET_HOURS. */
export type SyncHealth = components["schemas"]["SyncHealthOut"];
type SyncComponentState = SyncComponent["state"];
type SyncRunStatus = SyncRun["status"];

const DAY_MS = 86_400_000;

function parts(iso: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(iso))) {
    out[p.type] = p.value;
  }
  return out;
}

/** "14:32 UTC". */
export function utcClock(iso: string): string {
  const p = parts(iso);
  return `${p.hour}:${p.minute} UTC`;
}

/** "Oct 18, 14:32 UTC". */
export function utcDateTime(iso: string): string {
  const p = parts(iso);
  return `${p.month} ${p.day}, ${p.hour}:${p.minute} UTC`;
}

const sameUtcDay = (a: string, b: string): boolean => Math.floor(Date.parse(a) / DAY_MS) === Math.floor(Date.parse(b) / DAY_MS);

/**
 * "published 14:32 UTC" when it is the same UTC day as the model cycle named next to it (the
 * cycle label carries the date), else "published Oct 19, 02:10 UTC".
 */
export function publishedPhrase(publishedAt: string, cycle?: string | null): string {
  return `published ${cycle && sameUtcDay(publishedAt, cycle) ? utcClock(publishedAt) : utcDateTime(publishedAt)}`;
}

const STORED_KIND: Record<SyncStoredRun["kind"], string> = { spots: "spot forecasts", map: "map layers", ensemble: "ensemble" };

/** "spot forecasts 06Z Oct 22 and map layers 00Z Oct 22": each kind with its cycles (newest
 * first), from the runs a sync stored; null when it stored none with a known cycle. */
export function storedRunsPhrase(stored: readonly SyncStoredRun[]): string | null {
  const parts: string[] = [];
  for (const kind of ["spots", "map", "ensemble"] as const) {
    const cycles = [...new Set(stored.filter((r) => r.kind === kind && r.cycle).map((r) => r.cycle as string))].sort((a, b) => Date.parse(b) - Date.parse(a));
    if (cycles.length > 0) parts.push(`${STORED_KIND[kind]} ${cycles.map(cycleLabel).join(" and ")}`);
  }
  if (parts.length === 0) return null;
  return parts.length === 1 ? parts[0]! : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

/**
 * The published sync's line. With the runs it stored (backends since the Upgrade 4
 * integration): "Updated with spot forecasts 06Z Oct 22 and map layers 00Z Oct 22 · published
 * 15:04 UTC", so spot and map cycles are named apart (the anchor cycle is only the cycle the
 * two were coalesced on). An older backend sends only the anchor cycle: "Updated with the 12Z Oct
 * 18 model run · published 14:32 UTC". Nothing stored with a cycle (a forced republication) and
 * no anchor: "Published Oct 18, 14:32 UTC".
 */
export function publicationLine(run: Pick<SyncRun, "anchor_cycle" | "published_at" | "stored">): string {
  const publishedAt = run.published_at as string;
  if (run.stored !== undefined) {
    const phrase = storedRunsPhrase(run.stored);
    if (!phrase) return `Published ${utcDateTime(publishedAt)}`;
    const newest = run.stored.map((r) => r.cycle).filter((c): c is string => !!c).sort((a, b) => Date.parse(b) - Date.parse(a))[0];
    return `Updated with ${phrase} · ${publishedPhrase(publishedAt, newest)}`;
  }
  const cycle = run.anchor_cycle;
  if (!cycle) return `Published ${utcDateTime(publishedAt)}`;
  return `Updated with the ${cycleLabel(cycle)} model run · ${publishedPhrase(publishedAt, cycle)}`;
}

/** "35 ms", "3.8 s", "21 s", "4 min 5 s", "1 h 12 min"; "—" when unknown. */
export function formatDurationMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 9950) return `${(ms / 1000).toFixed(1)} s`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  const min = Math.round(s / 60);
  return `${Math.floor(min / 60)} h ${min % 60} min`;
}

/** Wall time of a sync: finished − started, or so far while it runs; null when unknown. */
export function runDurationMs(run: Pick<SyncRun, "status" | "started_at" | "finished_at">, now: number = Date.now()): number | null {
  const start = Date.parse(run.started_at);
  const end = run.finished_at ? Date.parse(run.finished_at) : run.status === "running" ? now : NaN;
  const ms = end - start;
  return Number.isFinite(ms) && ms >= 0 ? ms : null;
}

const RUN_STATUS: Record<SyncRunStatus, { label: string; tone: Tone }> = {
  running: { label: "Running", tone: "info" },
  success: { label: "Success", tone: "good" },
  partial: { label: "Partial", tone: "warn" },
  failed: { label: "Failed", tone: "danger" },
  skipped: { label: "Skipped", tone: "muted" },
  abandoned: { label: "Abandoned", tone: "danger" },
};

/** Label and badge colour of a sync's status; an unknown status is muted, never green. */
export const runStatus = (status: string): { label: string; tone: Tone } => RUN_STATUS[status as SyncRunStatus] ?? { label: titleCase(status), tone: "muted" };

const STAGE_TONE: Record<string, Tone> = { success: "good", skipped: "muted", failed: "danger" };
export const stageTone = (status: string): Tone => STAGE_TONE[status] ?? "muted";

const STAGE_LABEL: Record<string, string> = {
  spots: "Spot forecasts",
  ensemble: "Ensemble",
  map: "Map layers",
  blend: "Blend",
  predict: "Surf predictions",
  publish: "Publish",
  detect: "Swell detection",
  match: "Matching",
  flights: "Flights",
  alerts: "Alerts",
  deliver: "Delivery",
  prune: "Cleanup",
};
export const stageLabel = (name: string): string => STAGE_LABEL[name] ?? titleCase(name);

const COMPONENT_STATE: Record<SyncComponentState, { label: string; tone: Tone }> = {
  current: { label: "Current", tone: "good" },
  new: { label: "New run", tone: "info" },
  waiting: { label: "Waiting", tone: "info" },
  in_progress: { label: "Syncing", tone: "info" },
  failed: { label: "Failed", tone: "danger" },
  unknown: { label: "Unknown", tone: "warn" },
  disabled: { label: "Off", tone: "muted" },
};

/** Label and badge colour of a component's state at the last check. */
export const componentState = (state: string): { label: string; tone: Tone } =>
  COMPONENT_STATE[state as SyncComponentState] ?? { label: titleCase(state), tone: "muted" };

const KIND_LABEL: Record<SyncComponent["kind"], string> = {
  spots: "Spot forecasts",
  ensemble: "Ensemble spread",
  map: "World map layers",
};
export const componentKindLabel = (kind: string): string => KIND_LABEL[kind as SyncComponent["kind"]] ?? titleCase(kind);

/** A component's source by name: the data health report's forecast-source name, else the name
 * the backend gives the component (it names the map fields and the ensemble too), else the code. */
export const sourceName = (code: string, names?: ReadonlyMap<string, string>, own?: string | null): string => names?.get(code) ?? own ?? code;

const TRIGGER_LABEL: Record<SyncRun["trigger"], string> = {
  daemon: "sync daemon",
  celery: "Celery",
  cli: "command line",
  pipeline: "pipeline",
  github: "GitHub Actions",
  http: "HTTP",
};
export const triggerLabel = (trigger: string): string => TRIGGER_LABEL[trigger as SyncRun["trigger"]] ?? trigger;

export const runnerLabel = (runner: SyncHealth["runner"]): string => (runner === "daemon" ? "Sync daemon" : "Celery (sync.tick)");

/**
 * The CPU meter: share of SYNC_CPU_BUDGET_HOURS used this UTC month, or null when no budget is
 * set (then no meter is shown) or the use is unknown. `fraction` is not capped (1.2 = 120 %).
 * Amber above the report's `cpu_alert_fraction` (where data health turns degraded), red at the
 * budget; a report without it (an older backend) has no amber step and no `alert`.
 */
export function cpuBudget(
  sync: Pick<SyncHealth, "cpu_hours_month" | "cpu_budget_hours"> & Partial<Pick<SyncHealth, "cpu_alert_fraction">>,
): { used: number; budget: number; fraction: number; alert: number | null; tone: Tone } | null {
  const budget = sync.cpu_budget_hours;
  const used = sync.cpu_hours_month;
  if (budget == null || !(budget > 0) || used == null || !Number.isFinite(used)) return null;
  const fraction = Math.max(0, used) / budget;
  const alert = sync.cpu_alert_fraction != null && sync.cpu_alert_fraction > 0 ? sync.cpu_alert_fraction : null;
  return { used, budget, fraction, alert, tone: fraction >= 1 ? "danger" : alert !== null && fraction > alert ? "warn" : "good" };
}

/** "2.4 h", "12.5 h", "10 h", "0.04 h", "0.004 h" (the backend rounds to 0.001 h, so a small
 * but non-zero use early in the month never reads "0 h"). */
export function formatCpuHours(hours: number): string {
  const abs = Math.abs(hours);
  return `${Number(hours.toFixed(abs >= 0.1 ? 1 : abs >= 0.01 ? 2 : 3))} h`;
}

/** Both are the same sync (the newest sync is the one last published). */
export const isSameRun = (a: SyncRun | null | undefined, b: SyncRun | null | undefined): boolean => !!a && !!b && a.id === b.id;
