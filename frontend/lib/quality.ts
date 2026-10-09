import type { QualityLabel } from "@/types/api";

/** Mirrors backend/app/services/surf_quality/scoring.py LABEL_THRESHOLDS. */
export const QUALITY_THRESHOLDS: Array<[number, QualityLabel]> = [
  [95, "exceptional"],
  [85, "excellent"],
  [72, "very_good"],
  [57, "good"],
  [40, "fair"],
  [0, "poor"],
];

export const QUALITY_OPTIONS: Array<{ value: Exclude<QualityLabel, "poor">; label: string; min: number; hint: string }> = [
  { value: "fair", label: "Fair", min: 40, hint: "Rideable, some flaws in size, swell or wind." },
  { value: "good", label: "Good", min: 57, hint: "Worth a session: decent swell and workable wind." },
  { value: "very_good", label: "Very Good", min: 72, hint: "Well-aligned swell, clean conditions." },
  { value: "excellent", label: "Excellent", min: 85, hint: "Strong groundswell in the spot's sweet spot, light or offshore wind." },
  { value: "exceptional", label: "Exceptional", min: 95, hint: "Near-perfect on every measured factor. Rare." },
];

export const QUALITY_LABELS: Record<QualityLabel, string> = {
  poor: "Poor",
  fair: "Fair",
  good: "Good",
  very_good: "Very Good",
  excellent: "Excellent",
  exceptional: "Exceptional",
};

export const QUALITY_COLORS: Record<QualityLabel, string> = {
  poor: "#94a3b8",
  fair: "#e5a213",
  good: "#22a55a",
  very_good: "#13a39a",
  excellent: "#0277c2",
  exceptional: "#7c3aed",
};

export function labelForScore(score: number): QualityLabel {
  for (const [threshold, label] of QUALITY_THRESHOLDS) if (score >= threshold) return label;
  return "poor";
}

export function colorForScore(score: number | null | undefined): string {
  return score == null ? "#cbd5e1" : QUALITY_COLORS[labelForScore(score)];
}

export function asQuality(value: string): QualityLabel {
  return (value in QUALITY_LABELS ? value : "poor") as QualityLabel;
}

export const WIND_RELATION_LABEL: Record<string, string> = {
  glassy: "Glassy",
  offshore: "Offshore",
  "cross-offshore": "Cross-offshore",
  "cross-shore": "Cross-shore",
  "cross-onshore": "Cross-onshore",
  onshore: "Onshore",
  unknown: "Unknown",
};

export const MATCH_STATUS: Record<string, { label: string; tone: "good" | "info" | "warn" | "muted" }> = {
  flight_found: { label: "Flights found", tone: "good" },
  surf_only: { label: "Surf only", tone: "warn" },
  pending_flights: { label: "Checking flights", tone: "info" },
  expired: { label: "Expired", tone: "muted" },
  dismissed: { label: "Dismissed", tone: "muted" },
};
