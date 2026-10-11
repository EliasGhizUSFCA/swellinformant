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

/**
 * Quality colours (our app's categories, not a surf-forecasting standard): grey = no usable
 * forecast, blue = poor, yellow = fair, green = good, teal = very good, orange = excellent,
 * purple = exceptional. Keep in sync with --q-* in app/globals.css.
 */
export const QUALITY_COLORS: Record<QualityLabel, string> = {
  poor: "#2563eb",
  fair: "#eab308",
  good: "#16a34a",
  very_good: "#0f766e",
  excellent: "#c2410c",
  exceptional: "#7c3aed",
};

/** Text colour readable on each quality colour. */
export const QUALITY_TEXT: Record<QualityLabel, string> = {
  poor: "#ffffff",
  fair: "#422006",
  good: "#ffffff",
  very_good: "#ffffff",
  excellent: "#ffffff",
  exceptional: "#ffffff",
};

export const NO_FORECAST_COLOR = "#9ca3af";

export function labelForScore(score: number): QualityLabel {
  for (const [threshold, label] of QUALITY_THRESHOLDS) if (score >= threshold) return label;
  return "poor";
}

export function colorForScore(score: number | null | undefined): string {
  return score == null ? NO_FORECAST_COLOR : QUALITY_COLORS[labelForScore(score)];
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
