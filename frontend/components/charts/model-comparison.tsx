"use client";

import { useState } from "react";
import { Area, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { cn } from "@/lib/utils";
import type { EnsemblePoint, ModelSeries } from "@/types/api";

type Metric = "hs" | "tp" | "wind";

const METRICS: Record<Metric, { label: string; unit: string; field: "hs_m" | "tp_s" | "wind_kmh"; digits: number }> = {
  hs: { label: "Offshore wave height", unit: "m", field: "hs_m", digits: 2 },
  tp: { label: "Peak period", unit: "s", field: "tp_s", digits: 1 },
  wind: { label: "Wind speed", unit: "km/h", field: "wind_kmh", digits: 0 },
};

const ROLE_LABEL: Record<string, string> = {
  authoritative: "used for our prediction",
  member: "blended in",
  cross_check: "same model family, compared only",
  reference: "shown for comparison",
};

const PALETTE = ["#0e7490", "#c2410c", "#7c3aed", "#16a34a", "#64748b", "#db2777"];

const axisStyle = { fontSize: 11, fill: "var(--muted-foreground)" };

function tickLabel(tz: string) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", day: "numeric" });
  return (t: number) => f.format(new Date(t));
}

/**
 * Raw numerical-model output at the spot's offshore point, one line per model run, plus the
 * GEFS-Wave ensemble mean ± spread when available. These are offshore quantities, not surf
 * heights; different models legitimately disagree, and their spread feeds our uncertainty.
 */
export function ModelComparisonChart({ series, ensemble, tz }: { series: ModelSeries[]; ensemble: EnsemblePoint[]; tz: string }) {
  const [metric, setMetric] = useState<Metric>("hs");
  const m = METRICS[metric];
  const byTime = new Map<number, Record<string, number | [number, number] | null>>();
  const row = (t: number) => {
    let r = byTime.get(t);
    if (!r) {
      r = { t };
      byTime.set(t, r);
    }
    return r;
  };
  series.forEach((s, i) => {
    for (const p of s.points) {
      // Periods are only comparable when they mean the same thing (peak period).
      if (metric === "tp" && s.period_kind && s.period_kind !== "tp") continue;
      row(new Date(p.valid_time).getTime())[`s${i}`] = p[m.field] ?? null;
    }
  });
  if (metric === "hs") {
    for (const e of ensemble) {
      if (e.hs_mean_m == null) continue;
      const r = row(new Date(e.valid_time).getTime());
      r.ensMean = e.hs_mean_m;
      r.ensBand = e.hs_spread_m != null ? [Math.max(0, e.hs_mean_m - e.hs_spread_m), e.hs_mean_m + e.hs_spread_m] : null;
    }
  }
  const rows = [...byTime.values()].sort((a, b) => (a.t as number) - (b.t as number));
  const dayTicks = rows
    .map((r) => r.t as number)
    .filter((t) => Number(new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "numeric", hour12: false }).format(new Date(t))) % 24 === 0);

  return (
    <div data-testid="model-comparison">
      <div className="mb-2 flex flex-wrap gap-1.5" role="tablist" aria-label="Model comparison variable">
        {(Object.keys(METRICS) as Metric[]).map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={metric === k}
            onClick={() => setMetric(k)}
            className={cn(
              "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
              metric === k ? "border-ocean bg-ocean text-white" : "hover:bg-muted",
            )}
          >
            {METRICS[k].label}
          </button>
        ))}
      </div>
      <div className="h-64 w-full">
        <ResponsiveContainer>
          <ComposedChart data={rows} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={dayTicks} tickFormatter={tickLabel(tz)} tick={axisStyle} />
            <YAxis tick={axisStyle} width={48} unit={` ${m.unit}`} />
            <Tooltip
              labelFormatter={(t) => new Intl.DateTimeFormat("en-US", { timeZone: tz, dateStyle: "medium", timeStyle: "short" }).format(new Date(Number(t)))}
              formatter={(v, name) => [Array.isArray(v) ? v.map((x) => Number(x).toFixed(m.digits)).join("–") : Number(v).toFixed(m.digits) + " " + m.unit, name]}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {metric === "hs" && ensemble.length > 0 && (
              <>
                <Area dataKey="ensBand" name="GEFS-Wave mean ± spread" stroke="none" fill="#94a3b8" fillOpacity={0.25} isAnimationActive={false} connectNulls />
                <Line dataKey="ensMean" name="GEFS-Wave ensemble mean" stroke="#475569" strokeDasharray="4 3" dot={false} strokeWidth={1.5} isAnimationActive={false} connectNulls />
              </>
            )}
            {series.map((s, i) => (
              <Line
                key={`${s.source_code}-${i}`}
                dataKey={`s${i}`}
                name={`${s.name}${s.is_demo ? " (DEMO)" : ""}`}
                stroke={PALETTE[i % PALETTE.length]}
                strokeWidth={s.role === "authoritative" ? 2.5 : 1.5}
                strokeDasharray={s.role === "cross_check" ? "2 3" : undefined}
                dot={false}
                isAnimationActive={false}
                connectNulls
              />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <ul className="mt-2 space-y-0.5 text-[11px] text-muted-foreground" data-testid="model-roles">
        {series.map((s, i) => (
          <li key={`${s.source_code}-role-${i}`}>
            <span className="font-medium" style={{ color: PALETTE[i % PALETTE.length] }}>
              {s.name}
            </span>{" "}
            · {ROLE_LABEL[s.role] ?? s.role} · run {new Date(s.run_issued_at).toISOString().slice(0, 13).replace("T", " ")}Z
            {metric === "tp" && s.period_kind && s.period_kind !== "tp" ? " · period is not a peak period, hidden" : ""}
          </li>
        ))}
      </ul>
      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
        Raw output of each numerical model at the offshore forecast point. These are open-water values, not surf heights, and differences between
        models are expected: they are a measure of forecast uncertainty, not errors.
      </p>
    </div>
  );
}
