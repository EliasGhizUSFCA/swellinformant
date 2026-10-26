"use client";

import { ExternalLink } from "lucide-react";

import { useNowMs } from "@/components/forecast-provenance";
import { InfoPopover } from "@/components/info-popover";
import { Disclosure } from "@/components/ui/disclosure";
import {
  asConfidenceClass,
  confidenceLabel,
  FORECAST_CONFIDENCE_MEANING,
  forecastOnlyReasons,
  LEGACY_INDEX_LABEL,
  legacyIndexText,
  LOCAL_CONFIDENCE_MEANING,
  modelRunText,
  NOT_VERIFIED_TEXT,
  pickLocalReason,
  RESEARCH_CONFIDENCE_MEANING,
  safeExternalUrl,
  SOURCE_TYPE_LABEL,
  validInText,
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

/** Shorter prefixes for cards (the full name stays in the tooltip and for screen readers). */
export const SHORT_KIND_LABEL: Record<ConfidenceKind, string> = {
  forecast: "Forecast confidence",
  local: "At the break",
  research: "Spot research",
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
 * every reason and what the rating means one click away. Only forecast reasons are listed:
 * `reasons` should be the API's `forecast_confidence_reasons`; research sentences, the
 * disputed-wind note and the constant "not yet checked against buoys" note are filtered out
 * of whatever list is passed (the deprecated mixed `confidence_reasons` included). `notes` (the
 * mixed list) only supplies that last note, shown once below the reasons. With `validTime`
 * the details say how far away the hour is ("in 2 h") and, with `runIssuedAt`, how old the
 * model run is ("model run 16 h ago"). `legacyIndex`, the earlier 0–100 index, measures how
 * settled the offshore forecast is; it is not this class, so the details name it "Offshore
 * spread index … (not a probability)".
 */
export function ForecastConfidence({
  value,
  reasons,
  notes,
  validTime,
  runIssuedAt,
  blend = false,
  nowMs,
  legacyIndex,
  showReason = true,
  className,
  "data-testid": testId = "forecast-confidence",
}: {
  value: string | null | undefined;
  reasons?: readonly string[] | null;
  notes?: readonly string[] | null;
  validTime?: string | null;
  runIssuedAt?: string | null;
  /** The run is a blend: its issue time is only its oldest member's, and is named so. */
  blend?: boolean;
  nowMs?: number;
  /** The legacy 0–100 index, shown in the details under its own name (never as this class). */
  legacyIndex?: number | null;
  /** Show the first reason next to the badge (off in very tight spaces). */
  showReason?: boolean;
  className?: string;
  "data-testid"?: string;
}) {
  const c = asConfidenceClass(value);
  const list = forecastOnlyReasons(reasons);
  const notVerified = [...(reasons ?? []), ...(notes ?? [])].some((r) => r.trim() === NOT_VERIFIED_TEXT);
  const clock = useNowMs();
  const now = nowMs ?? clock;
  const run = runIssuedAt ? modelRunText(runIssuedAt, now) : null;
  const valid = validTime ? validInText(validTime, now) : null;
  const index = legacyIndexText(legacyIndex);
  const timing = [valid ? `valid ${valid}` : null, run && blend ? `oldest ${run}` : run].filter((t): t is string => !!t).join(" · ");
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
        {timing && <p data-testid="forecast-confidence-timing">{`${timing.charAt(0).toUpperCase()}${timing.slice(1)}.`}</p>}
        {notVerified && <p data-testid="forecast-confidence-unverified">{NOT_VERIFIED_TEXT}</p>}
        {index && (
          <p data-testid="forecast-confidence-index">
            {LEGACY_INDEX_LABEL} {index}.
          </p>
        )}
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
 * forecast class; the reason is the API's own (`local_confidence_reason`, passed in), or when
 * the response has none, the last reason about the break (pickLocalReason). `compact` shows a
 * short "At the break: Low" badge with the reason as its tooltip and for screen readers; inside
 * a card link that is all, while `explain` (outside links) adds a "Learn more" popover with the
 * reason, so it can be read on touch screens too.
 */
export function LocalConfidence({
  value,
  forecastValue,
  reasons,
  reason,
  compact = false,
  explain = false,
  dark = false,
  className,
}: {
  value: string | null | undefined;
  forecastValue: string | null | undefined;
  reasons?: readonly string[] | null;
  reason?: string | null;
  compact?: boolean;
  /** Compact only, never inside a link: add a popover with the reason. */
  explain?: boolean;
  dark?: boolean;
  className?: string;
}) {
  const local = asConfidenceClass(value);
  const fc = asConfidenceClass(forecastValue);
  if (local === fc) return null;
  const why = reason ?? pickLocalReason(reasons);
  if (compact) {
    return (
      <span className={cn("inline-flex items-center gap-0.5", className)} data-testid="local-confidence" data-confidence={local ?? "none"} title={why ?? LOCAL_CONFIDENCE_MEANING}>
        <ConfidenceBadge value={value} kind="local" label={SHORT_KIND_LABEL.local} className={dark ? "border-white/20" : undefined} />
        {why && (
          <span className="sr-only" data-testid="local-confidence-reason">
            {" "}
            {why}
          </span>
        )}
        {explain && (
          <InfoPopover title={KIND_LABEL.local} tone={dark ? "dark" : "default"}>
            {why && <p className="text-ink">{why}</p>}
            <p>{LOCAL_CONFIDENCE_MEANING}</p>
          </InfoPopover>
        )}
      </span>
    );
  }
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
 * How well the break's ideal conditions are documented: the class, a one-line summary and
 * the first gap, with "+N more" (the gap count stays visible); every gap, the per-condition
 * breakdown, sources, the research date and the method are in "Research details". `compact`
 * moves the summary into the details too (for narrow panels and short first screens).
 */
export function ResearchConfidence({
  research,
  compact = false,
  className,
  defaultOpen = false,
  "data-testid": testId = "research-confidence",
}: {
  research: Research | null | undefined;
  compact?: boolean;
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
  const [firstGap, ...moreGaps] = research.gaps;
  return (
    <div className={cn("space-y-1.5", className)} data-testid={testId} data-confidence={c ?? "none"}>
      <div className="flex items-center gap-1.5">
        <ConfidenceBadge value={research.research_class} kind="research" />
        <InfoPopover term="researchConfidence" />
      </div>
      {!compact && research.summary && (
        <p className="text-sm text-ink" data-testid="research-summary">
          {research.summary}
        </p>
      )}
      {firstGap && (
        <p className="text-xs text-muted-foreground" data-testid="research-first-gap">
          {firstGap}
          {moreGaps.length > 0 && (
            <span className="ml-1 font-medium whitespace-nowrap text-ink" data-testid="research-more-gaps">
              +{moreGaps.length} more
            </span>
          )}
        </p>
      )}
      <Disclosure
        summary="Research details"
        defaultOpen={defaultOpen}
        contentClassName="space-y-3 rounded-lg bg-muted/50 p-2.5 text-xs"
        data-testid="research-details"
      >
        {compact && research.summary && <p className="text-ink">{research.summary}</p>}
        {research.gaps.length > 0 && (
          <div>
            <div className="mb-1 font-semibold text-ink">Gaps</div>
            <ul className="list-disc space-y-0.5 pl-4 text-muted-foreground" data-testid="research-gaps" aria-label="Research gaps">
              {research.gaps.map((g, i) => (
                <li key={`${i}-${g}`}>{g}</li>
              ))}
            </ul>
          </div>
        )}
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
        {updated && (
          <p className="text-muted-foreground" data-testid="research-updated">
            {updated}
          </p>
        )}
        <p className="text-muted-foreground" data-testid="research-method">
          Methodology {research.methodology_version}
          {research.assessed_at ? ` · assessed ${dateOnly(research.assessed_at)}` : ""}
        </p>
      </Disclosure>
    </div>
  );
}
