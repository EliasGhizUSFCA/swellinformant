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
 * Quality colours: one ordinal ramp in the app's ocean hue (OKLCH hue 224-260), so the order
 * of the classes reads from the colour itself: on light surfaces a deeper blue is better surf;
 * on dark surfaces (QUALITY_COLORS_DARK) a brighter one is. Every step has at least 3:1
 * contrast against its surface, lightness is strictly monotone with visible gaps (OKLCH
 * delta L >= 0.06) and the hue spread stays within 40 degrees (tests/quality-palette.test.ts).
 * Colour never carries the class alone: every quality mark has its label as text, a tooltip
 * or a table row next to it. It replaces the earlier hue-per-class legend (blue, yellow,
 * green, teal, orange, purple), whose "fair" had 1.92:1 contrast on white and whose top three
 * classes were nearly the same lightness. Keep in sync with --q-* in app/globals.css.
 */
export const QUALITY_COLORS: Record<QualityLabel, string> = {
  poor: "#6995a7",
  fair: "#417d98",
  good: "#25698e",
  very_good: "#085486",
  excellent: "#013e77",
  exceptional: "#002767",
};

/** The same ramp stepped for dark surfaces (the map's dark panels, #171b20): brighter is
 * better there. */
export const QUALITY_COLORS_DARK: Record<QualityLabel, string> = {
  poor: "#4b869c",
  fair: "#589bb7",
  good: "#6dadd2",
  very_good: "#8bc0e8",
  excellent: "#acd2fb",
  exceptional: "#d6e6ff",
};

/** The surfaces the two ramps are checked against: --card, and the map UI's dark --card. */
export const QUALITY_SURFACES = { light: "#ffffff", dark: "#171b20" } as const;

/** Text colour readable (>= 4.5:1) on each quality colour. */
export const QUALITY_TEXT: Record<QualityLabel, string> = {
  poor: "#0b2233",
  fair: "#ffffff",
  good: "#ffffff",
  very_good: "#ffffff",
  excellent: "#ffffff",
  exceptional: "#ffffff",
};

/** Text colour readable (>= 4.5:1) on each dark-surface quality colour. */
export const QUALITY_TEXT_DARK: Record<QualityLabel, string> = {
  poor: "#101418",
  fair: "#101418",
  good: "#101418",
  very_good: "#101418",
  excellent: "#101418",
  exceptional: "#101418",
};

/** Short class names for narrow chart segments (the legend spells them out). */
export const QUALITY_SHORT: Record<QualityLabel, string> = {
  poor: "P",
  fair: "F",
  good: "G",
  very_good: "VG",
  excellent: "Ex",
  exceptional: "Ex+",
};

/** Rank of each class, 0 (poor) to 5 (exceptional). */
export const QUALITY_RANK: Record<QualityLabel, number> = {
  poor: 0,
  fair: 1,
  good: 2,
  very_good: 3,
  excellent: 4,
  exceptional: 5,
};

export const NO_FORECAST_COLOR = "#9ca3af";

export function labelForScore(score: number): QualityLabel {
  for (const [threshold, label] of QUALITY_THRESHOLDS) if (score >= threshold) return label;
  return "poor";
}

/** Marker colour for a 0-100 score (``surface`` "dark" on dark backgrounds such as the dark
 * map basemap); grey without a forecast. */
export function colorForScore(score: number | null | undefined, surface: "light" | "dark" = "light"): string {
  if (score == null) return NO_FORECAST_COLOR;
  return (surface === "dark" ? QUALITY_COLORS_DARK : QUALITY_COLORS)[labelForScore(score)];
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
