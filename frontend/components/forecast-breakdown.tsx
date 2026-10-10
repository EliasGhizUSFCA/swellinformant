import { Bot, Waves, Wind } from "lucide-react";

import { ConfidenceBadge, DemoBadge, QualityBadge } from "@/components/quality";
import { compass, formatDateTime, heightRange, kmh, metres, seconds, type Units } from "@/lib/format";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import type { Conditions } from "@/types/api";

const METHOD_LABEL: Record<string, string> = {
  komar_gaughan_v1_uncalibrated: "Komar–Gaughan breaker height with an uncalibrated spot factor",
  calibrated_power_law: "Spot model calibrated against observations",
  linear_wave_v2: "Linear wave theory: shoaling, refraction and depth-limited breaking",
};

export function methodLabel(method?: string | null): string {
  if (!method) return "—";
  return METHOD_LABEL[method] ?? method.replaceAll("_", " ");
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <dt className="text-muted-foreground">{k}</dt>
      <dd className="text-right font-medium tabular-nums">{v}</dd>
    </div>
  );
}

/**
 * One forecast hour, split into the raw numerical-model data and our own prediction.
 * They are different physical quantities, so their difference is never shown as an error.
 */
export function ForecastBreakdown({ c, tz, units }: { c: Conditions; tz: string; units: Units }) {
  const dir = c.swell_direction_deg;
  return (
    <div className="space-y-3" data-testid="forecast-breakdown">
      <div className="text-xs text-muted-foreground">
        {formatDateTime(c.valid_time, tz)} local · {c.is_daylight ? "daylight" : "night (not surfable)"}
      </div>

      <section className="rounded-xl border p-3" data-testid="raw-model">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            <Waves className="size-3.5" /> Offshore forecast · raw model data
          </div>
          {c.is_demo && <DemoBadge />}
        </div>
        <div className="mt-0.5 text-[11px] text-muted-foreground">
          {c.source_name ?? "Forecast source"}
          {c.run_issued_at ? ` · model run ${formatDateTime(c.run_issued_at, "UTC")} UTC` : ""}
        </div>
        <dl className="mt-2 text-sm">
          <Row k="Significant wave height (offshore)" v={metres(c.sig_wave_height_m)} />
          <Row k="Primary swell" v={`${metres(c.swell_height_m)} @ ${seconds(c.swell_period_s)}`} />
          <Row k="Swell direction" v={dir != null ? `${compass(dir)} (${Math.round(dir)}°)` : "—"} />
          <Row
            k="Wind"
            v={
              <span className="inline-flex items-center gap-1">
                <Wind className="size-3.5" />
                {kmh(c.wind_speed_kmh)} from {compass(c.wind_direction_deg)}
              </span>
            }
          />
        </dl>
      </section>

      <section className="rounded-xl border border-ocean/30 bg-ocean/5 p-3" data-testid="our-prediction">
        <div className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-ocean uppercase">
          <Bot className="size-3.5" /> Our prediction · Swell Travel Agent
          {c.algorithm_version ? ` v${c.algorithm_version}` : ""}
        </div>
        <div className="mt-1 font-display text-2xl font-semibold" data-testid="breaking-height">
          {heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)}
          <span className="ml-1 align-middle text-xs font-normal text-muted-foreground">est. breaking faces</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <QualityBadge label={c.label} score={c.score} />
          <ConfidenceBadge label={c.confidence_label} value={c.confidence} />
          <span className="text-xs text-muted-foreground">{WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation} wind</span>
        </div>
        {c.explanation && <p className="mt-2 text-sm text-muted-foreground">{c.explanation}</p>}
        <p className="mt-2 text-[11px] text-muted-foreground">Method: {methodLabel(c.method)}</p>
      </section>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        Offshore height is the open-water sea state from the numerical model; breaking height is our estimate of wave faces at this break. They are
        different quantities, so their difference is not a forecast error.
      </p>
    </div>
  );
}
