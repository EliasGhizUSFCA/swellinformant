import { Wind } from "lucide-react";

import { QualityBadge } from "@/components/quality";
import { compass, formatDateTime, heightRange, kmh, metres, seconds, type Units } from "@/lib/format";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import type { Conditions } from "@/types/api";

export function ConditionsSummary({ c, tz, units, title }: { c: Conditions; tz: string; units: Units; title: string }) {
  return (
    <div className="rounded-xl border bg-card p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{title}</div>
        <QualityBadge label={c.label} score={c.score} />
      </div>
      <div className="mt-2 font-display text-2xl font-semibold">
        {heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, units)}
        <span className="ml-1 align-middle text-xs font-normal text-muted-foreground">est. faces</span>
      </div>
      <div className="mt-1 text-sm text-muted-foreground">
        {metres(c.swell_height_m)} @ {seconds(c.swell_period_s)} {compass(c.swell_direction_deg)} swell
      </div>
      <div className="mt-0.5 flex items-center gap-1.5 text-sm text-muted-foreground">
        <Wind className="size-3.5" />
        {kmh(c.wind_speed_kmh)} {compass(c.wind_direction_deg)} · {WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation}
      </div>
      <div className="mt-2 text-xs text-muted-foreground">{formatDateTime(c.valid_time, tz)} local</div>
    </div>
  );
}
