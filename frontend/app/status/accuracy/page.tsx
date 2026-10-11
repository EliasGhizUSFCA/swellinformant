"use client";

import { CheckCircle2, FlaskConical, Gauge, XCircle } from "lucide-react";
import { useMemo, useState } from "react";

import { DailyErrorChart, ErrorHistogram, LeadMetricChart, PairScatter, colorMap, leadLabel } from "@/components/charts/accuracy-charts";
import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { RequireAuth } from "@/components/require-auth";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useAccuracyMetrics, useAccuracyOverview, useAccuracyPairs } from "@/hooks/queries";
import { relativeTime } from "@/lib/format";
import type { AccuracyMetric, AccuracyOverview, ModelVersionSummary } from "@/types/api";

// "hs": model Hm0 vs buoy spectral Hm0; "hs_h13": vs buoys that only report H1/3 (never mixed).
const VARIABLES = {
  hs: { label: "Wave height (Hm0)", unit: "m" },
  hs_h13: { label: "Wave height (H1/3 buoys)", unit: "m" },
  tp: { label: "Peak period", unit: "s" },
  dir: { label: "Direction", unit: "°" },
} as const;
type Variable = keyof typeof VARIABLES;
const ALL = "__all__";

function fmt(v: number | null | undefined, digits = 3): string {
  return v == null ? "–" : v.toFixed(digits);
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="mt-1 text-xl font-semibold">{value}</div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}

function MetricsTable({ rows, keyLabel, testId }: { rows: AccuracyMetric[]; keyLabel: (r: AccuracyMetric) => string; testId: string }) {
  if (!rows.length) return <p className="text-sm text-muted-foreground">No metrics computed for this selection yet.</p>;
  return (
    <div className="overflow-x-auto" data-testid={testId}>
      <table className="w-full text-xs">
        <thead className="text-left text-muted-foreground">
          <tr>
            {["", "Lead", "n", "Bias", "MAE", "RMSE", "SI", "HH", "CC", "Skill vs persistence", "Skill vs climatology"].map((h) => (
              <th key={h} className="px-2 py-1 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.scope}-${r.scope_key}-${r.source_code}-${r.model_version}-${r.lead_bucket_h}`} className="border-t">
              <td className="px-2 py-1 font-medium">{keyLabel(r)}</td>
              <td className="px-2 py-1">{leadLabel(r.lead_bucket_h)}</td>
              <td className="px-2 py-1">{r.n}</td>
              <td className="px-2 py-1">{fmt(r.bias)}</td>
              <td className="px-2 py-1">{fmt(r.mae)}</td>
              <td className="px-2 py-1">{fmt(r.rmse)}</td>
              <td className="px-2 py-1">{fmt(r.scatter_index)}</td>
              <td className="px-2 py-1">{fmt(r.hh)}</td>
              <td className="px-2 py-1">{fmt(r.correlation)}</td>
              <td className="px-2 py-1">{fmt(r.skill_vs_persistence, 2)}</td>
              <td className="px-2 py-1">{fmt(r.skill_vs_climatology, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type Criterion = { id?: string; name?: string; passed?: boolean; detail?: string; value?: unknown; threshold?: unknown };

/** Deployment criteria as recorded by the registry: a list of {id, name, passed, detail}. */
function criteria(m: ModelVersionSummary): { name: string; passed: boolean; detail: string }[] {
  const raw = m.decision?.criteria as unknown;
  const list: [string, Criterion][] = Array.isArray(raw)
    ? (raw as Criterion[]).map((c, i) => [`${c.id ? `(${c.id}) ` : ""}${c.name ?? `criterion ${i + 1}`}`, c])
    : Object.entries((raw ?? {}) as Record<string, Criterion>);
  return list
    .filter(([, c]) => c && typeof c === "object" && "passed" in c)
    .map(([name, c]) => ({
      name,
      passed: Boolean(c.passed),
      detail: c.detail ?? (typeof c.value === "number" ? `value ${c.value.toFixed(3)}` : ""),
    }));
}

function trainingSummary(m: ModelVersionSummary): string {
  const t = m.training as Record<string, unknown>;
  const parts: string[] = [];
  const period = t.period as Record<string, string> | string[] | undefined;
  const day = (v: unknown) => (typeof v === "string" ? v.slice(0, 10) : "?");
  if (Array.isArray(period) && period.length === 2) parts.push(`${day(period[0])} → ${day(period[1])}`);
  else if (period && typeof period === "object") {
    const p = period as Record<string, string>;
    parts.push(`cycles ${day(p.cycle_start)} → ${day(p.cycle_end)}`);
  }
  const rows = t.rows as Record<string, number> | number | undefined;
  if (typeof rows === "number") parts.push(`${rows.toLocaleString()} rows`);
  else if (rows && typeof rows === "object")
    parts.push(Object.entries(rows).map(([k, v]) => `${k} ${Number(v).toLocaleString()}`).join(" / ") + " rows");
  const stations = t.stations;
  if (Array.isArray(stations)) parts.push(`stations ${stations.join(", ")}`);
  return parts.join(" · ") || "–";
}

/** Held-out test RMSE against the raw forecast, when the registry recorded it. */
function testSummary(m: ModelVersionSummary): string | null {
  const all = (m.test_metrics as Record<string, Record<string, number>> | undefined)?.all;
  if (!all || typeof all.rmse !== "number" || typeof all.rmse_raw !== "number") return null;
  const change = (all.rmse / all.rmse_raw - 1) * 100;
  return `held-out test: RMSE ${all.rmse.toFixed(3)} m vs raw ${all.rmse_raw.toFixed(3)} m (${change >= 0 ? "+" : ""}${change.toFixed(1)}%), n = ${Number(all.n ?? 0).toLocaleString()}`;
}

function ModelRegistry({ models }: { models: ModelVersionSummary[] }) {
  if (!models.length) return <p className="text-sm text-muted-foreground">No model has been trained yet. Without a passing model, forecasts are shown uncorrected.</p>;
  return (
    <div className="space-y-3" data-testid="model-registry">
      {models.map((m) => (
        <div key={m.id} className="rounded-xl border p-3 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium">{m.label}</span>
            <Badge variant={m.status === "active" ? "good" : m.status === "rejected" ? "warn" : "default"}>{m.status}</Badge>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {m.model_type} · target {m.target} · trained {relativeTime(m.created_at)}
            {m.activated_at && ` · active since ${relativeTime(m.activated_at)}`}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            Training dataset: {m.dataset_version ?? "no fingerprint recorded"} · {trainingSummary(m)}
            {m.experiment_id != null && ` · experiment #${m.experiment_id}`}
          </div>
          {testSummary(m) && <div className="mt-1 text-xs">{testSummary(m)}</div>}
          {criteria(m).length > 0 && (
            <ul className="mt-2 grid gap-1 text-xs sm:grid-cols-2">
              {criteria(m).map((c) => (
                <li key={c.name} className="flex items-start gap-1.5">
                  {c.passed ? <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-emerald-700" aria-label="passed" /> : <XCircle className="mt-0.5 size-3.5 shrink-0 text-destructive" aria-label="failed" />}
                  <span>
                    {c.name.replaceAll("_", " ")} {c.detail && <span className="text-muted-foreground">({c.detail})</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}

function Dashboard({ overview }: { overview: AccuracyOverview }) {
  const [variable, setVariable] = useState<Variable>("hs");
  const sources = [...new Set(overview.pair_counts.map((p) => p.source_code))];
  const [source, setSource] = useState<string>(sources[0] ?? ALL);
  const [windowDays, setWindowDays] = useState("90");
  const [station, setStation] = useState(ALL);
  const versions = useMemo(() => [...new Set(["raw", ...overview.pair_counts.map((p) => p.model_version)])], [overview.pair_counts]);
  const [version, setVersion] = useState("raw");
  const colors = useMemo(() => colorMap([...versions, ...overview.models.map((m) => m.label)]), [versions, overview.models]);
  const unit = VARIABLES[variable].unit;

  const metricParams = new URLSearchParams({ variable, window_days: windowDays });
  if (source !== ALL) metricParams.set("source", source);
  const metrics = useAccuracyMetrics(metricParams, true);
  const pairParams = new URLSearchParams({ variable, days: windowDays, model_version: version });
  if (source !== ALL) pairParams.set("source", source);
  if (station !== ALL) pairParams.set("station", station);
  const pairs = useAccuracyPairs(pairParams, true);

  const rows = metrics.data ?? [];
  const globalRows = rows.filter((r) => r.scope === "global");
  const leadRows = globalRows.length ? globalRows : rows.filter((r) => r.scope === "station" && (station === ALL || r.scope_key === station));
  const stationRows = rows.filter((r) => r.scope === "station" && r.model_version === version);
  const active = overview.models.find((m) => m.status === "active");
  const hsPairs = overview.pair_counts.filter((p) => p.variable === "hs" && p.model_version === "raw").reduce((n, p) => n + p.n, 0);

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Latest validation" value={overview.latest_validation_at ? relativeTime(overview.latest_validation_at) : "never"} sub={overview.latest_metrics_at ? `metrics ${relativeTime(overview.latest_metrics_at)}` : "no metrics yet"} />
        <Stat label="Hs pairs (raw model)" value={hsPairs.toLocaleString()} sub={`${sources.length} source${sources.length === 1 ? "" : "s"}`} />
        <Stat label="Stations with pairs" value={`${overview.stations.filter((s) => s.pairs > 0).length} / ${overview.stations.length}`} />
        <Stat label="Active correction" value={active ? active.label : "none deployed"} sub={active ? active.model_type : "raw model output is used"} />
      </div>

      <div className="flex flex-wrap items-center gap-2" data-testid="accuracy-filters">
        <div className="flex gap-1 rounded-xl bg-muted p-1 text-xs" role="tablist" aria-label="Variable">
          {(Object.keys(VARIABLES) as Variable[]).map((v) => (
            <button key={v} role="tab" aria-selected={variable === v} onClick={() => setVariable(v)} className={variable === v ? "rounded-lg bg-card px-3 py-1.5 font-medium shadow-sm" : "rounded-lg px-3 py-1.5 text-muted-foreground"}>
              {VARIABLES[v].label}
            </button>
          ))}
        </div>
        <Select value={source} onValueChange={setSource}>
          <SelectTrigger className="w-48" aria-label="Forecast source">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All sources</SelectItem>
            {sources.map((s) => (
              <SelectItem key={s} value={s}>
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={windowDays} onValueChange={setWindowDays}>
          <SelectTrigger className="w-32" aria-label="Window">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="30">30 days</SelectItem>
            <SelectItem value="90">90 days</SelectItem>
          </SelectContent>
        </Select>
        <Select value={station} onValueChange={setStation}>
          <SelectTrigger className="w-56" aria-label="Station">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All stations</SelectItem>
            {overview.stations.map((s) => (
              <SelectItem key={s.code} value={s.code}>
                {s.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={version} onValueChange={setVersion}>
          <SelectTrigger className="w-56" aria-label="Model version">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {versions.map((v) => (
              <SelectItem key={v} value={v}>
                {v === "raw" ? "raw model (uncorrected)" : v}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>RMSE by lead time</CardTitle>
            <CardDescription>{globalRows.length ? "All stations pooled" : "Per-station rows (no pooled metrics yet)"}, per model version</CardDescription>
          </CardHeader>
          <CardContent>{leadRows.length ? <LeadMetricChart rows={leadRows} metric="rmse" unit={unit} colors={colors} testId="accuracy-rmse-by-lead" /> : <p className="text-sm text-muted-foreground">No metrics yet.</p>}</CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Bias by lead time</CardTitle>
            <CardDescription>Forecast minus observation; below zero means the forecast is too low</CardDescription>
          </CardHeader>
          <CardContent>{leadRows.length ? <LeadMetricChart rows={leadRows} metric="bias" unit={unit} colors={colors} testId="accuracy-bias-by-lead" /> : <p className="text-sm text-muted-foreground">No metrics yet.</p>}</CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>By model and lead time</CardTitle>
        </CardHeader>
        <CardContent>
          <MetricsTable rows={leadRows} keyLabel={(r) => `${r.model_version}${r.scope === "station" ? ` · ${r.scope_key}` : ""}`} testId="accuracy-model-table" />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>By station</CardTitle>
          <CardDescription>Surf spots paired with each buoy are listed under Stations below</CardDescription>
        </CardHeader>
        <CardContent>
          <MetricsTable rows={stationRows} keyLabel={(r) => r.scope_key} testId="accuracy-station-table" />
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Forecast vs observed</CardTitle>
            <CardDescription>{pairs.data ? `${pairs.data.sample.length} of ${pairs.data.n_total.toLocaleString()} pairs (reproducible sample); dashed line is a perfect forecast` : "Loading…"}</CardDescription>
          </CardHeader>
          <CardContent>{pairs.data && pairs.data.sample.length > 0 ? <PairScatter pairs={pairs.data} color={colors[version] ?? colors.raw ?? "#2a78d6"} /> : <p className="text-sm text-muted-foreground">No pairs for this selection.</p>}</CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Error distribution</CardTitle>
            <CardDescription>Forecast minus observation ({unit}); tails clipped at the 0.5 and 99.5 percentiles</CardDescription>
          </CardHeader>
          <CardContent>{pairs.data && pairs.data.histogram.length > 0 ? <ErrorHistogram pairs={pairs.data} color={colors[version] ?? colors.raw ?? "#2a78d6"} /> : <p className="text-sm text-muted-foreground">No pairs for this selection.</p>}</CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>History</CardTitle>
          <CardDescription>Daily RMSE and bias by valid date, all leads in the selection</CardDescription>
        </CardHeader>
        <CardContent>{pairs.data && pairs.data.daily.length > 0 ? <DailyErrorChart pairs={pairs.data} /> : <p className="text-sm text-muted-foreground">No pairs for this selection.</p>}</CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <FlaskConical className="size-4 text-ocean" /> Model registry
            </CardTitle>
            <CardDescription>Every trained candidate with its held-out decision; only a model that passes every criterion can be active</CardDescription>
          </CardHeader>
          <CardContent>
            <ModelRegistry models={overview.models} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Gauge className="size-4 text-ocean" /> Stations
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-xs" data-testid="accuracy-stations">
            {overview.stations.length === 0 && <p className="text-sm text-muted-foreground">No observation stations seeded.</p>}
            {overview.stations.map((s) => (
              <div key={s.code} className="rounded-lg border p-2">
                <div className="font-medium">
                  {s.name} <span className="text-muted-foreground">({s.code})</span>
                </div>
                <div className="text-muted-foreground">
                  {s.observations.toLocaleString()} observations{s.latest_observation && `, latest ${relativeTime(s.latest_observation)}`} · {s.pairs.toLocaleString()} pairs
                  {s.paired_spots.length > 0 && ` · spots: ${s.paired_spots.join(", ")}`}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>

      {overview.experiments.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Experiments</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {overview.experiments.map((e) => (
              <div key={e.id} className="rounded-lg border p-2">
                <div className="font-medium">
                  #{e.id} {e.name} <span className="text-xs text-muted-foreground">· {e.status} · {relativeTime(e.created_at)}</span>
                </div>
                {e.conclusion && <p className="mt-1 text-xs text-muted-foreground">{e.conclusion}</p>}
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

export default function AccuracyPage() {
  return (
    <AppShell>
      <RequireAuth>
        {(user) => (
          <>
            <PageHeader eyebrow="Developers" title="Forecast accuracy" description="Offshore forecasts verified against buoy observations, and the post-processing models trained on them." />
            {user.is_admin ? <AdminView /> : (
              <Alert>
                <AlertDescription>The accuracy dashboard is for admin accounts (python -m app.cli make-admin EMAIL).</AlertDescription>
              </Alert>
            )}
          </>
        )}
      </RequireAuth>
    </AppShell>
  );
}

function AdminView() {
  const overview = useAccuracyOverview(true);
  if (overview.isLoading) return <Skeleton className="h-96" />;
  if (!overview.data) return <Alert variant="danger"><AlertDescription>Could not load accuracy data.</AlertDescription></Alert>;
  return (
    <div className="space-y-4">
      <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted-foreground" data-testid="accuracy-notes">
        {overview.data.notes.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
      <Dashboard overview={overview.data} />
    </div>
  );
}
