"use client";

import { ExternalLink } from "lucide-react";

import { InfoPopover } from "@/components/info-popover";
import { Disclosure } from "@/components/ui/disclosure";
import {
  asConfidenceClass,
  confidenceLabel,
  FORECAST_CONFIDENCE_MEANING,
  leadTimeText,
  legacyIndexText,
  LOCAL_CONFIDENCE_MEANING,
  pickLocalReason,
  RESEARCH_CONFIDENCE_MEANING,
  safeExternalUrl,
  SOURCE_TYPE_LABEL,
  type ConfidenceClass,
} from "@/lib/confidence";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Research } from "@/types/api";

/**
 * Forecast confidence (per forecast hour) and research confidence (per spot) are separate
 * assessments: separate components, separate labels, never combined into one number.
 * Colour always comes with text: high = green, moderate = amber, low = red-orange and
 * "Not assessed yet" = neutral grey.
 */

export type ConfidenceKind = "forecast" | "local" | "research";

const KIND_LABEL: Record<ConfidenceKind, string> = {
  forecast: "Forecast confidence",
  local: "Confidence at the break",
  research: "Spot research confidence",
};

const STYLE: Record<ConfidenceClass | "none", { badge: string; dot: string }> = {
  high: { badge: "border-emerald-200 bg-emerald-50 text-emerald-800", dot: "bg-emerald-600" },
  moderate: { badge: "border-amber-200 bg-amber-50 text-amber-900", dot: "bg-amber-500" },
  low: { badge: "border-orange-300 bg-orange-50 text-orange-800", dot: "bg-orange-600" },
  none: { badge: "border-slate-200 bg-slate-50 text-slate-600", dot: "bg-slate-400" },
};

/**
 * Tiny "Forecast confidence: Moderate" pill for cards and table cells. ``compact`` shows only
 * the class word (for very narrow tiles); the prefix stays available to screen readers and
 * as a tooltip, so the meaning never rests on colour alone.
 */
export function ConfidenceBadge({
  value,
  kind = "forecast",
  label,
  compact = false,
  className,
  "data-testid": testId,
}: {
  value: string | null | undefined;
  kind?: ConfidenceKind;
  /** Override the "Forecast confidence" prefix (e.g. a shorter one in tight spaces). */
  label?: string;
  compact?: boolean;
  className?: string;
  "data-testid"?: string;
}) {
  const c = asConfidenceClass(value);
  const s = STYLE[c ?? "none"];
  const prefix = label ?? KIND_LABEL[kind];
  return (
    <span
      className={cn("inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs leading-snug", s.badge, className)}
      data-confidence={c ?? "none"}
      data-testid={testId ?? `${kind}-confidence-badge`}
      title={compact ? `${prefix}: ${confidenceLabel(value)}` : undefined}
    >
      <span className={cn("size-1.5 shrink-0 rounded-full", s.dot)} aria-hidden="true" />
      <span>
        <span className={cn(compact && "sr-only")}>{prefix}: </span>
        <span className="font-semibold">{confidenceLabel(value)}</span>
      </span>
    </span>
  );
}

/**
 * Forecast confidence of one forecast hour (or swell): the class and its first reason, with
 * every reason and what the rating means one click away. ``legacyIndex`` (the earlier 0–100
 * index) is only ever shown inside the details, as "index N / 100 (not a probability)".
 */
export function ForecastConfidence({
  value,
  reasons,
  leadHours,
  legacyIndex,
  showReason = true,
  className,
  "data-testid": testId = "forecast-confidence",
}: {
  value: string | null | undefined;
  reasons?: readonly string[] | null;
  leadHours?: number | null;
  legacyIndex?: number | null;
  /** Show the first reason next to the badge (off in very tight spaces). */
  showReason?: boolean;
  className?: string;
  "data-testid"?: string;
}) {
  const c = asConfidenceClass(value);
  const list = (reasons ?? []).filter((r) => r.trim() !== "");
  const lead = leadTimeText(leadHours);
  const legacy = legacyIndexText(legacyIndex);
  return (
    <div className={cn("space-y-1", className)} data-testid={testId} data-confidence={c ?? "none"}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <ConfidenceBadge value={value} kind="forecast" />
        {showReason && list[0] && (
          <span className="min-w-0 text-xs text-muted-foreground" data-testid="forecast-confidence-reason">
            {list[0]}
          </span>
        )}
      </div>
      <Disclosure summary="Confidence details" contentClassName="space-y-1.5 rounded-lg bg-muted/50 p-2.5 text-xs text-muted-foreground">
        {list.length > 0 ? (
          <ul className="list-disc space-y-1 pl-4 text-ink" data-testid="forecast-confidence-reasons">
            {list.map((r, i) => (
              <li key={`${i}-${r}`}>{r}</li>
            ))}
          </ul>
        ) : (
          <p>{c ? "No reasons were provided with this rating." : "Forecast confidence has not been assessed for this forecast yet."}</p>
        )}
        {lead && <p>This forecast is for {lead}.</p>}
        {legacy && <p>Confidence {legacy}.</p>}
        <p>
          <span className="font-medium text-ink">What this means: </span>
          {FORECAST_CONFIDENCE_MEANING}
        </p>
      </Disclosure>
    </div>
  );
}

/**
 * Confidence in the breaking-wave estimate at the break. Shown only when it differs from the
 * forecast class; the reason is the API's own (passed in, or the first reason about the break).
 */
export function LocalConfidence({
  value,
  forecastValue,
  reasons,
  reason,
  className,
}: {
  value: string | null | undefined;
  forecastValue: string | null | undefined;
  reasons?: readonly string[] | null;
  reason?: string | null;
  className?: string;
}) {
  const local = asConfidenceClass(value);
  const fc = asConfidenceClass(forecastValue);
  if (local === fc) return null;
  const why = reason ?? pickLocalReason(reasons);
  return (
    <div className={cn("flex flex-wrap items-center gap-x-2 gap-y-1", className)} data-testid="local-confidence" data-confidence={local ?? "none"}>
      <span className="inline-flex items-center gap-1">
        <ConfidenceBadge value={value} kind="local" />
        <InfoPopover term="localConfidence">
          <p>{LOCAL_CONFIDENCE_MEANING}</p>
        </InfoPopover>
      </span>
      {why && (
        <span className="min-w-0 text-xs text-muted-foreground" data-testid="local-confidence-reason">
          {why}
        </span>
      )}
    </div>
  );
}

function dateOnly(iso: string | null | undefined): string | null {
  return iso ? formatDate(iso, "UTC", { year: "numeric" }) : null;
}

/**
 * How well the break's ideal conditions are documented: class, one-line summary and the gaps
 * in plain sentences; the per-condition breakdown, sources and method are expandable.
 */
export function ResearchConfidence({
  research,
  className,
  defaultOpen = false,
  "data-testid": testId = "research-confidence",
}: {
  research: Research | null | undefined;
  className?: string;
  defaultOpen?: boolean;
  "data-testid"?: string;
}) {
  if (!research) {
    return (
      <div className={cn("space-y-1", className)} data-testid={testId} data-confidence="none">
        <div className="flex items-center gap-1.5">
          <ConfidenceBadge value={null} kind="research" />
          <InfoPopover term="researchConfidence" />
        </div>
        <p className="text-xs text-muted-foreground">This break&apos;s ideal conditions have not been checked against published sources yet.</p>
      </div>
    );
  }
  const c = asConfidenceClass(research.research_class);
  // research_updated_at is the newest source access date; without it only the assessment
  // date is known, which is labelled as such (it is not when the research changed).
  const updated = research.research_updated_at ? `Research updated ${dateOnly(research.research_updated_at)}` : research.assessed_at ? `Assessed ${dateOnly(research.assessed_at)}` : null;
  return (
    <div className={cn("space-y-1.5", className)} data-testid={testId} data-confidence={c ?? "none"}>
      <div className="flex items-center gap-1.5">
        <ConfidenceBadge value={research.research_class} kind="research" />
        <InfoPopover term="researchConfidence" />
      </div>
      {research.summary && (
        <p className="text-sm text-ink" data-testid="research-summary">
          {research.summary}
        </p>
      )}
      {research.gaps.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4 text-xs text-muted-foreground" data-testid="research-gaps" aria-label="Research gaps">
          {research.gaps.map((g, i) => (
            <li key={`${i}-${g}`}>{g}</li>
          ))}
        </ul>
      )}
      <Disclosure
        summary="Research details"
        defaultOpen={defaultOpen}
        contentClassName="space-y-3 rounded-lg bg-muted/50 p-2.5 text-xs"
        data-testid="research-details"
      >
        {research.variables.length > 0 && (
          <div>
            <div className="mb-1 font-semibold text-ink">By condition</div>
            <ul className="divide-y rounded-lg border bg-card" data-testid="research-variables">
              {research.variables.map((v) => (
                <li key={v.variable} className="space-y-1 p-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium text-ink">{v.label}</span>
                    <ConfidenceBadge value={v.research_class} kind="research" label="Research" data-testid="research-variable-badge" />
                  </div>
                  {v.summary && <p className="text-muted-foreground">{v.summary}</p>}
                  {v.gaps.map((g, i) => (
                    <p key={`${i}-${g}`} className="text-orange-800">
                      Gap: {g}
                    </p>
                  ))}
                  <p className="text-muted-foreground">
                    {v.source_count} source{v.source_count === 1 ? "" : "s"}
                  </p>
                </li>
              ))}
            </ul>
          </div>
        )}
        {research.sources.length > 0 && (
          <div>
            <div className="mb-1 font-semibold text-ink">Sources</div>
            <ul className="space-y-1.5" data-testid="research-sources">
              {research.sources.map((s, i) => {
                const href = safeExternalUrl(s.url);
                const meta = [
                  s.publisher,
                  SOURCE_TYPE_LABEL[s.source_type] ?? s.source_type,
                  s.published_year != null ? String(s.published_year) : null,
                  s.accessed_at ? `accessed ${dateOnly(s.accessed_at)}` : null,
                ]
                  .filter(Boolean)
                  .join(" · ");
                return (
                  <li key={`${i}-${s.title}`}>
                    {href ? (
                      <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-start gap-1 font-medium text-ocean hover:underline">
                        {s.title}
                        <ExternalLink className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
                        <span className="sr-only">(opens in a new tab)</span>
                      </a>
                    ) : (
                      <span className="font-medium text-ink">{s.title}</span>
                    )}
                    {meta && <div className="text-muted-foreground">{meta}</div>}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        <p className="text-muted-foreground">
          <span className="font-medium text-ink">What this means: </span>
          {RESEARCH_CONFIDENCE_MEANING}
        </p>
        <p className="text-muted-foreground" data-testid="research-method">
          Methodology {research.methodology_version}
          {research.assessed_at ? ` · assessed ${dateOnly(research.assessed_at)}` : ""}
        </p>
      </Disclosure>
      {updated && (
        <p className="text-[11px] text-muted-foreground" data-testid="research-updated">
          {updated}
        </p>
      )}
    </div>
  );
}
