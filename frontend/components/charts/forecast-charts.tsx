"use client";

import {
  Area,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { ConfidenceBadge } from "@/components/confidence";
import { confidenceLabel, hasField } from "@/lib/confidence";
import { compass, formatCalendarDate, formatDateTime, heightRange, toUnits, type Units } from "@/lib/format";
import { colorForScore, labelForScore, QUALITY_COLORS, QUALITY_LABELS, WIND_RELATION_LABEL } from "@/lib/quality";
import { score10 } from "@/lib/rating";
import type { DailySummary, ForecastPoint } from "@/types/api";

type Row = {
  t: number;
  iso: string;
  score: number;
  /** Score out of 10 (what the quality bars plot). */
  score10: number;
  color: string;
  hmin: number;
  hmax: number;
  range: [number, number];
  band: [number, number] | null;
  uncertaintyKind: string | null;
  swell: number | null;
  period: number | null;
  swellDir: number | null;
  wind: number | null;
  gust: number | null;
  windDir: number | null;
  relation: string;
  daylight: boolean;
  forecastConfidence: string | null;
  explanation: string;
};

export function toRows(points: ForecastPoint[], units: Units): Row[] {
  return points.map((p) => {
    const round = (ft: number) => Math.round(toUnits(ft, units) * 10) / 10;
    const hmin = round(p.breaking_height_min_ft);
    const hmax = round(p.breaking_height_max_ft);
    const p10 = p.breaking_p10_ft;
    const p90 = p.breaking_p90_ft;
    return {
      t: new Date(p.valid_time).getTime(),
      iso: p.valid_time,
      score: p.score,
      score10: Math.round(p.score) / 10,
      color: colorForScore(p.score),
      hmin,
      hmax,
      range: [hmin, hmax],
      band: p10 != null && p90 != null ? [round(p10), round(p90)] : null,
      uncertaintyKind: p.uncertainty_kind ?? null,
      swell: p.primary_swell_height_m ?? p.sig_wave_height_m ?? null,
      period: p.primary_swell_period_s ?? null,
      swellDir: p.primary_swell_direction_deg ?? null,
      wind: p.wind_speed_kmh ?? null,
      gust: p.wind_gust_kmh ?? null,
      windDir: p.wind_direction_deg ?? null,
      relation: p.wind_relation,
      daylight: p.is_daylight,
      forecastConfidence: p.forecast_confidence_class ?? null,
      explanation: p.explanation,
    };
  });
}

/** Contiguous night periods, drawn as shaded bands. */
function nightBands(rows: Row[]): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  let start: number | null = null;
  for (const r of rows) {
    if (!r.daylight && start == null) start = r.t;
    if (r.daylight && start != null) {
      out.push([start, r.t]);
      start = null;
    }
  }
  if (start != null && rows.length) out.push([start, rows[rows.length - 1]!.t]);
  return out;
}

function dayTicks(rows: Row[], tz: string): number[] {
  const fmt = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "numeric", hour12: false });
  return rows.filter((r) => Number(fmt.format(new Date(r.t))) % 24 === 0).map((r) => r.t);
}

function tickLabel(tz: string) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", day: "numeric" });
  return (t: number) => f.format(new Date(t));
}

function ChartTooltip({
  active,
  payload,
  tz,
  units,
}: {
  active?: boolean;
  payload?: ReadonlyArray<{ payload?: Row }>;
  tz: string;
  units: Units;
}) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  const label = labelForScore(row.score);
  return (
    <div className="max-w-[260px] rounded-xl border bg-card p-3 text-xs shadow-lg">
      <div className="font-semibold">{formatDateTime(row.iso, tz)}</div>
      <div className="mt-1 flex items-center gap-2">
        <span className="size-2 rounded-full" style={{ backgroundColor: QUALITY_COLORS[label] }} />
        {QUALITY_LABELS[label]} Surf · {score10(row.score)}/10 {!row.daylight && <span className="text-muted-foreground">(night)</span>}
      </div>
      <div className="mt-1">
        Breaking ≈ {row.hmin}–{row.hmax} {units} <span className="text-muted-foreground">(our estimate, typical–sets)</span>
      </div>
      {row.band && (
        <div className="text-muted-foreground">
          80% range {row.band[0]}–{row.band[1]} {units} ({uncertaintyLabel(row.uncertaintyKind)})
        </div>
      )}
      <div>
        Offshore swell {row.swell?.toFixed(1) ?? "—"} m @ {row.period?.toFixed(0) ?? "—"} s {compass(row.swellDir)}
      </div>
      <div>
        Wind {row.wind?.toFixed(0) ?? "—"} km/h {compass(row.windDir)} · {WIND_RELATION_LABEL[row.relation] ?? row.relation}
      </div>
      <div className="text-muted-foreground">Forecast confidence: {confidenceLabel(row.forecastConfidence)}</div>
    </div>
  );
}

const axisStyle = { fontSize: 11, fill: "var(--muted-foreground)" };

const UNCERTAINTY_LABEL: Record<string, string> = {
  heuristic: "assumed error model, not yet verified",
  ensemble_heuristic: "from ensemble spread, not yet verified",
  calibrated: "verified against observations",
};

export function uncertaintyLabel(kind?: string | null): string {
  return (kind && UNCERTAINTY_LABEL[kind]) ?? "not available";
}

export function QualityChart({ points, tz, units }: { points: ForecastPoint[]; tz: string; units: Units }) {
  const rows = toRows(points, units);
  const ticks = dayTicks(rows, tz);
  return (
    <div className="w-full" data-testid="quality-chart">
      <div className="h-64 w-full sm:h-72">
        <ResponsiveContainer>
          <ComposedChart data={rows} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            {nightBands(rows).map(([a, b]) => (
              <ReferenceArea key={a} x1={a} x2={b} yAxisId="score" fill="#06263a" fillOpacity={0.05} ifOverflow="extendDomain" />
            ))}
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} tickFormatter={tickLabel(tz)} tick={axisStyle} />
            <YAxis yAxisId="score" domain={[0, 10]} ticks={[0, 2, 4, 6, 8, 10]} tick={axisStyle} width={40} />
            <YAxis yAxisId="height" orientation="right" tick={axisStyle} width={40} unit={units} />
            <Tooltip content={(p) => <ChartTooltip active={p.active} payload={p.payload as ReadonlyArray<{ payload?: Row }>} tz={tz} units={units} />} />
            <Bar yAxisId="score" dataKey="score10" barSize={3} radius={[2, 2, 0, 0]} isAnimationActive={false}>
              {rows.map((r) => (
                <Cell key={r.t} fill={r.color} fillOpacity={r.daylight ? 0.9 : 0.35} />
              ))}
            </Bar>
            <Area yAxisId="height" type="monotone" dataKey="band" stroke="none" fill="#0e7490" fillOpacity={0.08} isAnimationActive={false} />
            <Area yAxisId="height" type="monotone" dataKey="range" stroke="#0e7490" fill="#0e7490" fillOpacity={0.2} strokeWidth={1.5} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" data-testid="quality-chart-legend">
        <span>bars: surf quality (0–10, left)</span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-4 rounded-sm bg-[#0e7490]/40" /> breaking waves, our estimate (right)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-4 rounded-sm bg-[#0e7490]/15" /> 80% forecast range ({uncertaintyLabel(rows.find((r) => r.uncertaintyKind)?.uncertaintyKind)})
        </span>
      </div>
    </div>
  );
}

export function SwellChart({ points, tz }: { points: ForecastPoint[]; tz: string }) {
  const rows = toRows(points, "ft");
  const ticks = dayTicks(rows, tz);
  return (
    <div className="w-full" data-testid="swell-chart">
      <div className="h-56 w-full">
        <ResponsiveContainer>
          <ComposedChart data={rows} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} tickFormatter={tickLabel(tz)} tick={axisStyle} />
            <YAxis yAxisId="h" tick={axisStyle} width={52} tickFormatter={(v: number) => `${v.toFixed(1)} m`} />
            <YAxis yAxisId="p" orientation="right" tick={axisStyle} width={40} unit="s" domain={[0, "dataMax + 2"]} />
            <Tooltip content={(p) => <ChartTooltip active={p.active} payload={p.payload as ReadonlyArray<{ payload?: Row }>} tz={tz} units="ft" />} />
            <Area yAxisId="h" type="monotone" dataKey="swell" name="Swell height" stroke="#13a39a" fill="#13a39a" fillOpacity={0.25} strokeWidth={2} isAnimationActive={false} />
            <Line yAxisId="p" type="monotone" dataKey="period" name="Period" stroke="#06263a" strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-4 rounded-sm bg-[#13a39a]/60" /> Primary swell height (offshore, m)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-0.5 w-4 bg-ink" /> Period (s)
        </span>
      </div>
    </div>
  );
}

function WindArrow(props: { cx?: number; cy?: number; payload?: Row; index?: number }) {
  const { cx, cy, payload, index = 0 } = props;
  if (cx == null || cy == null || !payload || payload.windDir == null || index % 3 !== 0) return <g />;
  // Arrow points where the wind blows TO (from + 180°).
  return (
    <g transform={`translate(${cx},${cy}) rotate(${payload.windDir + 180})`}>
      <path d="M0,-6 L3.5,3 L0,1 L-3.5,3 Z" fill={payload.relation.includes("onshore") ? "#d93a3a" : "#0e7490"} />
    </g>
  );
}

export function WindChart({ points, tz }: { points: ForecastPoint[]; tz: string }) {
  const rows = toRows(points, "ft");
  const ticks = dayTicks(rows, tz);
  return (
    <div className="w-full" data-testid="wind-chart">
      <div className="h-56 w-full">
        <ResponsiveContainer>
          <ComposedChart data={rows} margin={{ top: 12, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} tickFormatter={tickLabel(tz)} tick={axisStyle} />
            <YAxis tick={axisStyle} width={44} />
            <Tooltip content={(p) => <ChartTooltip active={p.active} payload={p.payload as ReadonlyArray<{ payload?: Row }>} tz={tz} units="ft" />} />
            <Area type="monotone" dataKey="gust" stroke="none" fill="#94a3b8" fillOpacity={0.2} isAnimationActive={false} />
            <Line type="monotone" dataKey="wind" stroke="#0b3a52" strokeWidth={1.5} dot={(p: object) => <WindArrow {...(p as Parameters<typeof WindArrow>[0])} />} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>Wind speed (km/h) with direction arrows</span>
        <span className="text-ocean">▲ offshore/cross</span>
        <span className="text-destructive">▲ onshore</span>
        <span>shaded: gusts</span>
      </div>
    </div>
  );
}

export function DailyStrip({
  daily,
  units,
  selected,
  onSelect,
}: {
  daily: DailySummary[];
  units: Units;
  selected?: string | null;
  onSelect?: (d: DailySummary) => void;
}) {
  const withConfidence = daily.some((d) => hasField(d, "forecast_confidence_class"));
  return (
    <div>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(96px,1fr))] gap-2" data-testid="daily-strip">
        {daily.map((d) => {
          const label = labelForScore(d.best_score);
          const Tag = onSelect ? "button" : "div";
          return (
            <Tag
              key={d.date}
              {...(onSelect ? { type: "button" as const, onClick: () => onSelect(d), "aria-pressed": selected === d.date } : {})}
              className={`rounded-xl border bg-card p-2.5 text-center ${onSelect ? "transition-colors hover:border-ocean" : ""} ${selected === d.date ? "border-ocean ring-2 ring-ocean/30" : ""}`}
              data-testid={`day-${d.date}`}
            >
              <div className="text-xs text-muted-foreground">
                {formatCalendarDate(d.date, { weekday: "short", month: undefined, day: undefined })}
              </div>
              <div className="text-xs font-medium">{formatCalendarDate(d.date)}</div>
              <div className="mx-auto mt-2 h-1.5 w-full rounded-full" style={{ backgroundColor: QUALITY_COLORS[label] }} />
              <div className="mt-2 text-sm font-semibold whitespace-nowrap">{heightRange(d.height_min_ft, d.height_max_ft, units)}</div>
              <div className="text-[11px] text-muted-foreground">
                {QUALITY_LABELS[label]} · {d.good_hours}h good
              </div>
              {hasField(d, "forecast_confidence_class") && (
                <ConfidenceBadge value={d.forecast_confidence_class} compact className="mt-1.5 px-1.5 text-[10px]" />
              )}
            </Tag>
          );
        })}
      </div>
      <p className="mt-1.5 text-[11px] text-muted-foreground">
        Daylight hours only: heights are our breaking-wave estimate (lowest to highest), the rating is the day&apos;s best hour.
        {withConfidence ? " The small badge is each day's forecast confidence." : ""}
      </p>
    </div>
  );
}
