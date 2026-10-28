"use client";

/**
 * Sections of the admin freshness dashboard (spec section 16): every figure is what
 * GET /api/admin/freshness reports, nothing is computed or estimated here beyond formatting.
 */
import { AlertTriangle, CheckCircle2, Clock, Gauge, Radio, ServerCog, Timer, Waves, Wind, XCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { relativeTime, titleCase } from "@/lib/format";
import { cn } from "@/lib/utils";

import {
  count,
  cycle,
  duration,
  durationMin,
  durationMs,
  overallTone,
  percent,
  staleReason,
  statusTone,
  usageLine,
  usageTone,
  utc,
} from "./freshness-format";
import type { AdminComponent, AdminDurationStats, AdminEvent, AdminFreshness, AdminProblem, AdminUsageWindow } from "./freshness-types";

const TH = "px-2 py-1.5 font-medium whitespace-nowrap";
const TD = "px-2 py-1.5 align-top";

function Ago({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="text-muted-foreground">—</span>;
  return (
    <span title={utc(iso)}>
      {utc(iso)} <span className="text-muted-foreground">({relativeTime(iso)})</span>
    </span>
  );
}

function Row({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="flex flex-col gap-0.5 border-b py-1.5 last:border-0 sm:flex-row sm:gap-3" data-testid={testId}>
      <dt className="shrink-0 text-muted-foreground sm:w-44">{label}</dt>
      <dd className="min-w-0 wrap-anywhere">{children}</dd>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

// --------------------------------------------------------------------------- problems
export function ProblemsCard({ data }: { data: AdminFreshness }) {
  const problems: { p: AdminProblem; admin: boolean }[] = [
    ...data.problems.map((p) => ({ p, admin: true })),
    ...data.health_problems.map((p) => ({ p, admin: false })),
  ].sort((a, b) => Number(b.p.severity === "failing") - Number(a.p.severity === "failing"));
  return (
    <Card data-testid="freshness-problems">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {data.status === "ok" ? <CheckCircle2 className="size-4 text-emerald-600" /> : <AlertTriangle className="size-4 text-amber-600" />}
          Problems
          <Badge variant={overallTone(data.status)} data-testid="freshness-status">
            {titleCase(data.status)}
          </Badge>
        </CardTitle>
        <CardDescription>Admin alerts (emailed to verified admins only) and the public data-health checks, the most severe first.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {problems.length === 0 && <Empty>No problems: every admin and data-health check passes.</Empty>}
        {problems.map(({ p, admin }) => (
          <div key={p.key} className="rounded-xl border p-3" data-testid="freshness-problem">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={p.severity === "failing" ? "danger" : "warn"}>
                {p.severity === "failing" ? <XCircle /> : <AlertTriangle />}
                {p.severity}
              </Badge>
              <code className="text-xs wrap-anywhere text-muted-foreground">{p.key}</code>
              <Badge variant="muted">{admin ? "admin alert" : "data health"}</Badge>
            </div>
            <p className="mt-1.5 wrap-anywhere">{p.message}</p>
            {p.detail && <p className="mt-1 font-mono text-xs wrap-anywhere text-muted-foreground">Last error: {p.detail}</p>}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- provider + models
function ComponentBlock({ c, icon }: { c: AdminComponent; icon: React.ReactNode }) {
  return (
    <div className="rounded-xl border p-3" data-testid={`freshness-component-${c.component}`}>
      <div className="flex flex-wrap items-center gap-2 font-medium">
        {icon}
        {c.component === "wave" ? "Wave" : "Atmosphere (wind)"}
        <code className="text-xs font-normal wrap-anywhere text-muted-foreground">{c.model ?? "no model yet"}</code>
        {c.estimated && <Badge variant="info">run time estimated</Badge>}
        {c.changed === false && <Badge variant="muted">carried forward</Badge>}
        {c.late ? <Badge variant="warn">next run late</Badge> : c.expected_next_at && <Badge variant="good">on time</Badge>}
      </div>
      <dl className="mt-2 text-sm">
        <Row label="Served run">{c.run ? `${cycle(c.run)}${c.carried_from_run_id ? ` (from run ${c.carried_from_run_id})` : ""}` : "—"}</Row>
        <Row label="Available upstream">
          <Ago iso={c.available_at} />
        </Row>
        <Row label="Detected">
          <Ago iso={c.last_detected_at} />
        </Row>
        <Row label="Acquired">
          <Ago iso={c.last_acquired_at} />
        </Row>
        <Row label="Typical lag">
          {c.typical_lag_minutes == null
            ? "not measured yet"
            : `${durationMin(c.typical_lag_minutes)} after the cycle (median of ${c.lag_samples} run${c.lag_samples === 1 ? "" : "s"})`}
        </Row>
        <Row label="Next run expected">
          {c.expected_next_at ? (
            <>
              {utc(c.expected_next_at)}
              <span className="text-muted-foreground">
                {" "}
                (late after {utc(c.late_after)}; updates every {durationMin(c.update_interval_minutes)}
                {c.update_interval_recorded ? ", meta.json" : ", assumed"})
              </span>
            </>
          ) : (
            "—"
          )}
        </Row>
      </dl>
    </div>
  );
}

export function ProviderCard({ data }: { data: AdminFreshness }) {
  const p = data.active_provider;
  return (
    <Card data-testid="freshness-provider">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Radio className="size-4 text-ocean" /> Provider and models
        </CardTitle>
        <CardDescription>
          Active provider <span className="font-medium text-foreground" data-testid="freshness-active-provider">{p.primary || "—"}</span> ({p.source}
          {p.source === "WEATHER_PROVIDER" ? `=${p.weather_provider}` : ""})
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex flex-wrap gap-2">
          {p.fallback_active ? (
            <Badge variant="warn">
              {p.fallback ?? "fallback"} stands in for {p.fallback_from}
              {p.fallback_until ? ` until ${utc(p.fallback_until)}` : ""}
            </Badge>
          ) : (
            <Badge variant="good">no fallback active</Badge>
          )}
          <Badge variant={p.windy_eligible ? "good" : "muted"}>Windy {p.windy_eligible ? "eligible" : "not eligible"}</Badge>
          {p.configured.join(",") !== p.providers.join(",") && <Badge variant="info">configured: {p.configured.join(", ")}</Badge>}
        </div>
        {p.reasons.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-5 text-xs wrap-anywhere text-muted-foreground">
            {p.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        )}
        {p.health.map((h) => (
          <div key={h.provider} className="text-xs text-muted-foreground">
            {h.provider}: last success {h.last_success_at ? relativeTime(h.last_success_at) : "never recorded"}
            {h.last_failure_at && `; last failure ${relativeTime(h.last_failure_at)} (${h.last_failure_kind})`}
            {h.blocking && " — blocking"}
          </div>
        ))}
        <ComponentBlock c={data.atmosphere} icon={<Wind className="size-4 text-ocean" />} />
        <ComponentBlock c={data.wave} icon={<Waves className="size-4 text-ocean" />} />
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- pipeline events
function Event({ label, e, testId }: { label: string; e: AdminEvent; testId: string }) {
  return (
    <Row label={label} testId={testId}>
      <Ago iso={e.at} />
      {e.detail && <div className="text-xs text-muted-foreground">{e.detail}</div>}
    </Row>
  );
}

export function PublicationCard({ data }: { data: AdminFreshness }) {
  const v = data.last_published_version;
  const s = data.scheduler;
  return (
    <Card data-testid="freshness-events">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ServerCog className="size-4 text-ocean" /> Detection to publication
        </CardTitle>
      </CardHeader>
      <CardContent className="text-sm">
        <dl>
          <Event label="Last model update detected" e={data.last_detected_model_update} testId="freshness-last-detected" />
          <Event label="Last acquisition" e={data.last_successful_acquisition} testId="freshness-last-acquired" />
          <Event label="Last scoring" e={data.last_successful_scoring} testId="freshness-last-scored" />
          <Row label="Published version" testId="freshness-published">
            {v ? (
              <>
                <span className="font-medium">#{v.id}</span> <Ago iso={v.published_at} />
                <div className="text-xs text-muted-foreground">
                  {v.provider}
                  {v.fallback_from ? ` (fallback for ${v.fallback_from})` : ""} · wind {cycle(v.wind_model_run)}
                  {v.wind_run_estimated ? " (estimated)" : ""} · waves {cycle(v.wave_model_run)}
                  {v.wave_run_estimated ? " (estimated)" : ""}
                </div>
              </>
            ) : (
              "none yet"
            )}
          </Row>
          <Row label="Spots updated" testId="freshness-spot-counts">
            {v ? (
              <>
                <span className="font-medium">{v.counts.updated}</span> of {v.counts.total}
                {v.counts.stale > 0 && <span className="text-amber-700"> · {v.counts.stale} stale</span>}
                {v.counts.failed > 0 && <span className="text-red-700"> · {v.counts.failed} failed in the last refresh</span>}
              </>
            ) : (
              "—"
            )}
          </Row>
          <Row label="Refresh scheduler" testId="freshness-scheduler">
            {!s.enabled ? (
              <span className="text-muted-foreground">{s.refresh_enabled ? "silence check off (REFRESH_SCHEDULER_EXPECTED_MINUTES=0)" : "refreshes disabled (FORECAST_REFRESH_ENABLED=false)"}</span>
            ) : (
              <>
                <Badge variant={s.silent ? "warn" : s.last_check_at ? "good" : "muted"}>{s.silent ? "silent" : s.last_check_at ? "checking" : "no check yet"}</Badge>{" "}
                {s.last_check_at ? (
                  <>
                    last check <Ago iso={s.last_check_at} /> <span className="text-xs text-muted-foreground">({s.last_check_by})</span>
                  </>
                ) : (
                  "no refresh check recorded"
                )}
                <div className="text-xs text-muted-foreground">expected at least every {s.expected_minutes} min</div>
              </>
            )}
          </Row>
        </dl>
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- timeline
export function TimelineCard({ data }: { data: AdminFreshness }) {
  return (
    <Card data-testid="freshness-timeline">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Clock className="size-4 text-ocean" /> Freshness timeline
        </CardTitle>
        <CardDescription>The newest forecast versions: which model runs each one serves, what changed, and how long after upstream availability it was published.</CardDescription>
      </CardHeader>
      <CardContent>
        {data.versions.length === 0 ? (
          <Empty>No forecast version yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-muted-foreground">
                <tr>
                  {["Version", "Status", "Published", "Wind run", "Wave run", "Changed", "Spots", "Availability → publication"].map((h) => (
                    <th key={h} className={TH}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.versions.map((v) => (
                  <tr key={v.id} className="border-t">
                    <td className={cn(TD, "font-medium")}>
                      #{v.id}
                      {v.kind && v.kind !== "refresh" && <span className="text-muted-foreground"> ({v.kind})</span>}
                    </td>
                    <td className={TD}>
                      <Badge variant={v.status === "published" ? "good" : v.status === "failed" ? "danger" : "muted"}>{v.status}</Badge>
                    </td>
                    <td className={cn(TD, "whitespace-nowrap")}>{utc(v.published_at ?? v.staged_at)}</td>
                    <td className={cn(TD, "whitespace-nowrap")}>{cycle(v.wind_model_run)}</td>
                    <td className={cn(TD, "whitespace-nowrap")}>{cycle(v.wave_model_run)}</td>
                    <td className={TD}>{v.changed.length ? v.changed.join(", ") : "—"}</td>
                    <td className={cn(TD, "whitespace-nowrap")}>
                      {v.spots_updated} updated{v.spots_stale ? `, ${v.spots_stale} stale` : ""}
                    </td>
                    <td className={TD}>{duration(v.upstream_to_publication_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- stale spots
/** Stale spots listed before "Show all". */
export const STALE_SPOTS_SHOWN = 10;

export function StaleSpotsCard({ data }: { data: AdminFreshness }) {
  const [all, setAll] = useState(false);
  const shown = all ? data.stale_spots : data.stale_spots.slice(0, STALE_SPOTS_SHOWN);
  const hidden = data.stale_spots.length - shown.length;
  return (
    <Card data-testid="freshness-stale-spots">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          Stale spots <Badge variant={data.stale_spots.length ? "warn" : "good"}>{data.stale_spots.length}</Badge>
        </CardTitle>
        <CardDescription>Spots of the published version labelled stale on the site (they keep their previous forecast).</CardDescription>
      </CardHeader>
      <CardContent>
        {data.stale_spots.length === 0 ? (
          <Empty>{data.version_id == null ? "No version published yet." : "Every spot is fresh."}</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-muted-foreground">
                <tr>
                  {["Spot", "Last updated", "Why"].map((h) => (
                    <th key={h} className={TH}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {shown.map((s) => (
                  <tr key={s.spot_id} className="border-t" data-testid="freshness-stale-spot">
                    <td className={TD}>
                      {s.slug ? (
                        <Link href={`/spots/${s.slug}`} className="font-medium text-ocean hover:underline">
                          {s.name ?? s.slug}
                        </Link>
                      ) : (
                        `#${s.spot_id}`
                      )}
                    </td>
                    <td className={cn(TD, "whitespace-nowrap")}>{s.last_updated_at ? relativeTime(s.last_updated_at) : "never"}</td>
                    <td className={TD}>
                      {staleReason(s.reason)}
                      {s.error && <div className="font-mono wrap-anywhere text-muted-foreground">{s.error}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {(hidden > 0 || all) && data.stale_spots.length > STALE_SPOTS_SHOWN && (
              <Button variant="ghost" size="sm" className="mt-2" onClick={() => setAll((v) => !v)} data-testid="freshness-stale-toggle">
                {all ? "Show fewer" : `Show all ${data.stale_spots.length}`}
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- failures
export function FailuresCard({ data }: { data: AdminFreshness }) {
  const failedJobs = data.refresh_jobs.filter((j) => j.status === "failed" || j.status === "abandoned").length;
  return (
    <Card data-testid="freshness-failed-requests">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <XCircle className="size-4 text-ocean" /> Failures
        </CardTitle>
        <CardDescription>
          Failed upstream requests of the last 24 h by provider and status; {failedJobs} of the newest {data.refresh_jobs.length} syncs failed.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data.failed_requests.length === 0 ? (
          <Empty>No failed request in the last 24 h.</Empty>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className={TH}>Provider</th>
                <th className={TH}>Status</th>
                <th className={cn(TH, "text-right")}>Count</th>
              </tr>
            </thead>
            <tbody>
              {data.failed_requests.map((f) => (
                <tr key={`${f.provider}-${f.status}`} className="border-t">
                  <td className={cn(TD, "font-mono wrap-anywhere")}>{f.provider}</td>
                  <td className={TD}>{/^\d+$/.test(f.status) ? `HTTP ${f.status}` : f.status}</td>
                  <td className={cn(TD, "text-right")}>{count(f.count)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

export function RefreshJobsCard({ data }: { data: AdminFreshness }) {
  return (
    <Card data-testid="freshness-refresh-jobs">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Timer className="size-4 text-ocean" /> Refresh jobs
        </CardTitle>
        <CardDescription>The newest syncs: who started them, their stages and durations, and the redacted error of a failed one.</CardDescription>
      </CardHeader>
      <CardContent>
        {data.refresh_jobs.length === 0 ? (
          <Empty>No sync has run yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-muted-foreground">
                <tr>
                  {["Sync", "Trigger", "Status", "Started", "Took", "Stages", "Versions"].map((h) => (
                    <th key={h} className={TH}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.refresh_jobs.map((j) => (
                  <tr key={j.id} className="border-t" data-testid="freshness-refresh-job">
                    <td className={cn(TD, "font-medium")}>
                      #{j.id}
                      <div className="font-normal text-muted-foreground">{j.kinds.join(", ") || (j.refresh ? "spots" : "—")}</div>
                    </td>
                    <td className={TD}>{j.trigger}</td>
                    <td className={TD}>
                      <Badge variant={statusTone(j.status)}>{j.status}</Badge>
                    </td>
                    <td className={cn(TD, "whitespace-nowrap")}>{utc(j.started_at)}</td>
                    <td className={cn(TD, "whitespace-nowrap")}>{durationMs(j.duration_ms)}</td>
                    <td className={TD}>
                      {j.stages.map((s) => `${s.name} ${durationMs(s.duration_ms)}`).join(" · ") || "—"}
                      {j.error && <div className="mt-0.5 font-mono wrap-anywhere text-red-700">{j.error}</div>}
                    </td>
                    <td className={TD}>{j.version_ids.length ? j.version_ids.map((v) => `#${v}`).join(", ") : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- usage
function Meter({ label, window, alert, testId }: { label: string; window: AdminUsageWindow | null | undefined; alert: number; testId: string }) {
  const tone = usageTone(window, alert);
  const value = Math.min(100, Math.max(0, (window?.fraction ?? 0) * 100));
  const indicator = { good: "bg-emerald-600", warn: "bg-amber-500", danger: "bg-red-600", muted: "bg-muted-foreground/40", info: "bg-sky-600" }[tone];
  return (
    <div className="space-y-1" data-testid={testId}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-sm">
        <span>{label}</span>
        <span className="text-xs text-muted-foreground">
          {usageLine(window)}
          {tone === "warn" && " · near the limit"}
          {tone === "danger" && " · used up"}
        </span>
      </div>
      <Progress value={value} indicatorClassName={indicator} aria-label={`${label}: ${usageLine(window)}`} />
      {window?.resets_at && <div className="text-[11px] text-muted-foreground">resets {utc(window.resets_at)}</div>}
    </div>
  );
}

export function UsageCard({ data }: { data: AdminFreshness }) {
  const u = data.api_usage;
  const alert = u.alert_fraction;
  return (
    <Card data-testid="freshness-usage">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Gauge className="size-4 text-ocean" /> API usage against the limits
        </CardTitle>
        <CardDescription>Counters of the current UTC windows; an admin alert at {percent(alert)} (QUOTA_ALERT_FRACTION).</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-3">
          <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Open-Meteo (weighted calls)</div>
          {!u.open_meteo.available && <Empty>The counters could not be read (Redis).</Empty>}
          <Meter label="This minute" window={u.open_meteo.minute} alert={alert} testId="usage-open-meteo-minute" />
          <Meter label="This hour" window={u.open_meteo.hour} alert={alert} testId="usage-open-meteo-hour" />
          <Meter label="Today" window={u.open_meteo.day} alert={alert} testId="usage-open-meteo-day" />
          <Meter label="This month" window={u.open_meteo.month} alert={alert} testId="usage-open-meteo-month" />
        </div>
        <div className="space-y-3">
          <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Windy (requests)</div>
          {u.windy.counted ? <Meter label="Today" window={u.windy.day} alert={alert} testId="usage-windy-day" /> : <Empty>Not counted by this build.</Empty>}
        </div>
        <div className="space-y-3">
          <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Flights ({u.flights.provider})</div>
          {u.flights.quota ? (
            <Meter label="Provider requests today (FLIGHT_DAILY_QUOTA)" window={u.flights.quota} alert={alert} testId="usage-flights-quota" />
          ) : (
            <Empty>The demo provider has no quota.</Empty>
          )}
          <div className="text-sm" data-testid="usage-flights-today">
            {count(u.flights.today)} searches stored today
            {Object.keys(u.flights.per_provider).length > 0 && ` (${Object.entries(u.flights.per_provider).map(([p, n]) => `${p} ${n}`).join(", ")})`}
            {u.flights.failed_today > 0 && <span className="text-red-700"> · {u.flights.failed_today} failed</span>}
            <span className="text-muted-foreground"> · at most {u.flights.user_daily_quota} per user a day</span>
          </div>
        </div>
        <div className="space-y-1 text-sm" data-testid="usage-emails">
          <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Emails ({u.emails.provider})</div>
          {count(u.emails.today)} today ({u.emails.alerts} alerts, {u.emails.account} account)
          {u.emails.failed > 0 && <span className="text-red-700"> · {u.emails.failed} failed</span>}
        </div>
        {u.requests_24h.length > 0 && (
          <div className="overflow-x-auto">
            <div className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">Forecast requests, last 24 h</div>
            <table className="w-full text-xs" data-testid="usage-requests-24h">
              <thead className="text-left text-muted-foreground">
                <tr>
                  {["Source", "Runs", "Requests", "Weighted calls", "Failed responses"].map((h) => (
                    <th key={h} className={TH}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {u.requests_24h.map((r) => (
                  <tr key={r.source} className="border-t">
                    <td className={cn(TD, "font-mono wrap-anywhere")}>{r.source}</td>
                    <td className={TD}>
                      {r.runs}
                      {r.failed_runs > 0 && <span className="text-red-700"> ({r.failed_runs} failed)</span>}
                    </td>
                    <td className={TD}>{count(r.requests)}</td>
                    <td className={TD}>{count(r.weighted_calls)}</td>
                    <td className={TD}>{r.failed_requests}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- times
function Stats({ label, s, unit = "s", testId }: { label: string; s: AdminDurationStats; unit?: "s" | "ms"; testId?: string }) {
  const f = unit === "ms" ? durationMs : duration;
  return (
    <Row label={label} testId={testId}>
      {s.samples === 0 ? (
        <span className="text-muted-foreground">no sample yet</span>
      ) : (
        <>
          p50 <span className="font-medium">{f(s.p50)}</span> · p95 <span className="font-medium">{f(s.p95)}</span> · last {f(s.last)}
          <span className="text-muted-foreground">
            {" "}
            ({s.samples} sample{s.samples === 1 ? "" : "s"})
          </span>
        </>
      )}
    </Row>
  );
}

export function TimesCard({ data }: { data: AdminFreshness }) {
  const p = data.processing_time;
  const l = data.latency;
  return (
    <Card data-testid="freshness-processing">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Timer className="size-4 text-ocean" /> Processing times and latencies
        </CardTitle>
        <CardDescription>Measured on the newest refreshes and versions; browser latencies are what open tabs report after a version change.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <dl data-testid="freshness-latency">
          <Stats label="Availability → publication" s={l.upstream_to_publication_s} testId="latency-upstream" />
          <Stats label="Publication → browsers" s={l.publication_to_frontend_s} testId="latency-frontend" />
          <Stats label="Publication → first served" s={l.first_served_s} testId="latency-first-served" />
          <Stats label="Detection → acquisition" s={p.detected_to_acquired_s} />
          <Stats label="Acquisition → scoring" s={p.acquired_to_scored_s} />
          <Stats label="Scoring → publication" s={p.scored_to_published_s} />
          <Stats label="Whole refresh" s={p.total_ms} unit="ms" testId="processing-total" />
        </dl>
        {p.stages.length > 0 ? (
          <div className="overflow-x-auto">
            <div className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">By stage ({p.refreshes} refreshes)</div>
            <table className="w-full text-xs" data-testid="processing-stages">
              <thead className="text-left text-muted-foreground">
                <tr>
                  {["Stage", "Runs", "Average", "p95", "Max", "CPU (avg)"].map((h) => (
                    <th key={h} className={TH}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {p.stages.map((s) => (
                  <tr key={s.stage} className="border-t">
                    <td className={cn(TD, "font-medium")}>{s.stage}</td>
                    <td className={TD}>{s.runs}</td>
                    <td className={TD}>{durationMs(s.avg_ms)}</td>
                    <td className={TD}>{durationMs(s.p95_ms)}</td>
                    <td className={TD}>{durationMs(s.max_ms)}</td>
                    <td className={TD}>{s.avg_cpu_s == null ? "—" : duration(s.avg_cpu_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No stage timings recorded yet.</Empty>
        )}
      </CardContent>
    </Card>
  );
}
