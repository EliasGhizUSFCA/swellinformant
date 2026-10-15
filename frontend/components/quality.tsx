import { Plane, Sparkles, Waves } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { asQuality, colorForScore, MATCH_STATUS, QUALITY_COLORS, QUALITY_LABELS, QUALITY_TEXT } from "@/lib/quality";
import { score10 } from "@/lib/rating";
import { cn } from "@/lib/utils";

/** Surf quality label, with the score shown out of 10 (the API's 0–100 score divided by ten). */
export function QualityBadge({ label, score, className }: { label: string; score?: number; className?: string }) {
  const q = asQuality(label);
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap", className)}
      style={{ backgroundColor: QUALITY_COLORS[q], color: QUALITY_TEXT[q] }}
    >
      {QUALITY_LABELS[q]}
      {score != null && (
        <span className="font-medium tabular-nums opacity-85">
          {score10(score)}
          <span className="opacity-75">/10</span>
        </span>
      )}
    </span>
  );
}

export function ScoreRing({ score, size = 56, label, caption }: { score: number; size?: number; label?: string; caption?: string }) {
  const r = (size - 8) / 2;
  const c = 2 * Math.PI * r;
  const color = colorForScore(score);
  return (
    <div className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90" aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r} stroke="var(--muted)" strokeWidth={6} fill="none" />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          stroke={color}
          strokeWidth={6}
          fill="none"
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - Math.max(0, Math.min(100, score)) / 100)}
        />
      </svg>
      <span className="absolute flex flex-col items-center leading-none" aria-label={label ?? `Score ${score} of 100`}>
        <span className="text-sm font-bold tabular-nums">{score}</span>
        {caption && <span className="mt-0.5 text-[9px] font-medium tracking-wide text-muted-foreground uppercase">{caption}</span>}
      </span>
    </div>
  );
}

export function DemoBadge({ mock = false }: { mock?: boolean }) {
  return (
    <Badge variant="demo" title={mock ? "Mock flight fares generated for testing" : "Synthetic demo forecast data"}>
      <Sparkles /> {mock ? "Mock fares" : "Demo data"}
    </Badge>
  );
}

export function MatchStatusBadge({ status }: { status: string }) {
  const meta = MATCH_STATUS[status] ?? { label: status, tone: "muted" as const };
  return (
    <Badge variant={meta.tone}>
      {status === "flight_found" ? <Plane /> : <Waves />}
      {meta.label}
    </Badge>
  );
}
