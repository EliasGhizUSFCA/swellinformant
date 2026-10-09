import { Plane, Sparkles, Waves } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { asQuality, colorForScore, MATCH_STATUS, QUALITY_COLORS, QUALITY_LABELS } from "@/lib/quality";
import { cn } from "@/lib/utils";

export function QualityBadge({ label, score, className }: { label: string; score?: number; className?: string }) {
  const q = asQuality(label);
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold text-white", className)}
      style={{ backgroundColor: QUALITY_COLORS[q] }}
    >
      {QUALITY_LABELS[q]}
      {score != null && <span className="font-medium opacity-85">{score}</span>}
    </span>
  );
}

export function ScoreRing({ score, size = 56, label }: { score: number; size?: number; label?: string }) {
  const r = (size - 8) / 2;
  const c = 2 * Math.PI * r;
  const color = colorForScore(score);
  return (
    <div className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
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
      <span className="absolute text-sm font-bold tabular-nums" aria-label={label ?? `Score ${score} of 100`}>
        {score}
      </span>
    </div>
  );
}

export function ConfidenceBadge({ label, value }: { label: string; value?: number }) {
  const variant = label === "high" ? "good" : label === "moderate" ? "info" : "warn";
  return (
    <Badge variant={variant} title="Forecast confidence (separate from surf quality)">
      {label.charAt(0).toUpperCase() + label.slice(1)} confidence{value != null ? ` · ${value}` : ""}
    </Badge>
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
