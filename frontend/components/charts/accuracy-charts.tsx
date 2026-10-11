"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

import type { AccuracyMetric, AccuracyPairs } from "@/types/api";

/** Categorical slots (validated reference palette), assigned to model versions in a fixed order. */
export const SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];

const axisStyle = { fontSize: 11, fill: "var(--muted-foreground)" };
const gridStroke = "var(--border)";

/** "24 h", "5 d"; the verification job stores all leads together as bucket -1. */
export function leadLabel(hours: number): string {
  if (hours < 0) return "all leads";
  return hours < 120 ? `${hours} h` : `${Math.round(hours / 24)} d`;
}

/** Stable colour per model version: "raw" first, then the rest in label order; never by rank. */
export function colorMap(versions: string[]): Record<string, string> {
  const ordered = [...new Set(versions)].sort((a, b) => (a === "raw" ? -1 : b === "raw" ? 1 : a.localeCompare(b)));
  return Object.fromEntries(ordered.map((v, i) => [v, SERIES_COLORS[Math.min(i, SERIES_COLORS.length - 1)] ?? "#2a78d6"]));
}

type MetricKey = "rmse" | "bias" | "mae";

/** One line per model version across lead buckets (one metric, one axis). */
export function LeadMetricChart({
  rows,
  metric,
  unit,
  colors,
  testId,
}: {
  rows: AccuracyMetric[];
  metric: MetricKey;
  unit: string;
  colors: Record<string, string>;
  testId: string;
}) {
  const versions = [...new Set(rows.map((r) => r.model_version))];
  // The all-leads aggregate (bucket -1) belongs in the table, not on a lead-time axis.
  const buckets = [...new Set(rows.map((r) => r.lead_bucket_h))].filter((b) => b >= 0).sort((a, b) => a - b);
  const data = buckets.map((b) => {
    const row: Record<string, number | string | null> = { bucket: leadLabel(b) };
    for (const v of versions) {
      const m = rows.find((r) => r.lead_bucket_h === b && r.model_version === v);
      row[v] = m ? m[metric] : null;
    }
    return row;
  });
  return (
    <div className="h-56 w-full" data-testid={testId}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} vertical={false} />
          <XAxis dataKey="bucket" tick={axisStyle} />
          <YAxis tick={axisStyle} width={52} unit={` ${unit}`} />
          {metric === "bias" && <ReferenceLine y={0} stroke="var(--muted-foreground)" />}
          <Tooltip formatter={(v, name) => [v == null ? "–" : `${Number(v).toFixed(3)} ${unit}`, name]} />
          {versions.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
          {versions.map((v) => (
            <Line key={v} dataKey={v} name={v} stroke={colors[v]} strokeWidth={2} dot={{ r: 4 }} isAnimationActive={false} connectNulls />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Forecast against observation, with the 1:1 line. */
export function PairScatter({ pairs, color }: { pairs: AccuracyPairs; color: string }) {
  const values = pairs.sample.flatMap((p) => [p.forecast, p.observed]);
  const lo = values.length ? Math.min(...values) : 0;
  const hi = values.length ? Math.max(...values) : 1;
  return (
    <div className="h-72 w-full" data-testid="accuracy-scatter">
      <ResponsiveContainer>
        <ScatterChart margin={{ top: 8, right: 12, left: -8, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} />
          <XAxis type="number" dataKey="observed" name="Observed" domain={[lo, hi]} tick={axisStyle} unit={` ${pairs.unit}`} tickFormatter={(v) => Number(v).toFixed(1)} />
          <YAxis type="number" dataKey="forecast" name="Forecast" domain={[lo, hi]} tick={axisStyle} width={52} unit={` ${pairs.unit}`} tickFormatter={(v) => Number(v).toFixed(1)} />
          <ZAxis range={[50, 50]} />
          <ReferenceLine segment={[{ x: lo, y: lo }, { x: hi, y: hi }]} stroke="var(--muted-foreground)" strokeDasharray="4 3" />
          <Tooltip
            cursor={{ strokeDasharray: "3 3" }}
            content={({ payload }) => {
              const p = payload?.[0]?.payload as AccuracyPairs["sample"][number] | undefined;
              if (!p) return null;
              return (
                <div className="rounded-lg border bg-card px-2 py-1 text-xs shadow-sm">
                  <div className="font-medium">{p.station}</div>
                  <div>{new Date(p.valid_time).toUTCString().slice(5, 22)} UTC · lead {Math.round(p.lead_hours)} h</div>
                  <div>
                    forecast {p.forecast.toFixed(2)} · observed {p.observed.toFixed(2)} {pairs.unit}
                  </div>
                </div>
              );
            }}
          />
          <Scatter data={pairs.sample} fill={color} fillOpacity={0.4} isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Distribution of forecast − observation. */
export function ErrorHistogram({ pairs, color }: { pairs: AccuracyPairs; color: string }) {
  const data = pairs.histogram.map((b) => ({ mid: (b.lo + b.hi) / 2, label: `${b.lo.toFixed(2)} to ${b.hi.toFixed(2)}`, count: b.count }));
  return (
    <div className="h-56 w-full" data-testid="accuracy-histogram">
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: 0 }} barCategoryGap={1}>
          <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} vertical={false} />
          <XAxis dataKey="mid" tick={axisStyle} tickFormatter={(v) => Number(v).toFixed(2)} unit={` ${pairs.unit}`} />
          <YAxis tick={axisStyle} width={40} allowDecimals={false} />
          <ReferenceLine x={data.reduce((best, d) => (Math.abs(d.mid) < Math.abs(best) ? d.mid : best), Infinity)} stroke="var(--muted-foreground)" />
          <Tooltip labelFormatter={(_, p) => `${p?.[0]?.payload?.label ?? ""} ${pairs.unit}`} formatter={(v) => [v, "pairs"]} />
          <Bar dataKey="count" fill={color} radius={[4, 4, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Daily RMSE and bias over time (same unit, one axis). */
export function DailyErrorChart({ pairs }: { pairs: AccuracyPairs }) {
  const data = pairs.daily.map((d) => ({ t: new Date(d.day).getTime(), rmse: d.rmse, bias: d.bias, n: d.n }));
  const fmt = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric" });
  return (
    <div className="h-56 w-full" data-testid="accuracy-history">
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tick={axisStyle} tickFormatter={(t) => fmt.format(new Date(t))} />
          <YAxis tick={axisStyle} width={52} unit={` ${pairs.unit}`} />
          <ReferenceLine y={0} stroke="var(--muted-foreground)" />
          <Tooltip
            labelFormatter={(t) => `${fmt.format(new Date(Number(t)))} (UTC day)`}
            formatter={(v, name, item) => [`${Number(v).toFixed(3)} ${pairs.unit}${name === "RMSE" ? ` · n=${item.payload.n}` : ""}`, name]}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Line dataKey="rmse" name="RMSE" stroke={SERIES_COLORS[0]} strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line dataKey="bias" name="Bias" stroke={SERIES_COLORS[1]} strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
