"use client";

import { ConfidenceBadge } from "@/components/confidence";
import { hasField } from "@/lib/confidence";
import { formatCalendarDate, heightRange, type Units } from "@/lib/format";
import { labelForScore, QUALITY_COLORS, QUALITY_LABELS } from "@/lib/quality";
import type { DailySummary } from "@/types/api";

/*
 * The spot page's charts are the unified timeline in components/forecast (one time axis for
 * quality, breaking waves, swell, wind and tide). What stays here is shared with other pages:
 * the daily strip of the opportunity page and the uncertainty wording.
 */

const UNCERTAINTY_LABEL: Record<string, string> = {
  heuristic: "assumed error model, not yet verified",
  ensemble_heuristic: "from ensemble spread, not yet verified",
  calibrated: "verified against observations",
};

export function uncertaintyLabel(kind?: string | null): string {
  return (kind && UNCERTAINTY_LABEL[kind]) ?? "not available";
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
                {QUALITY_LABELS[label]} · {Math.round(d.good_hours * 10) / 10}h good
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
