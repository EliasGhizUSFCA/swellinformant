import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { colorForScore, NO_FORECAST_COLOR, QUALITY_COLORS, QUALITY_COLORS_DARK, QUALITY_RANK, QUALITY_SURFACES, QUALITY_TEXT, QUALITY_TEXT_DARK } from "@/lib/quality";
import type { QualityLabel } from "@/types/api";

/** WCAG 2.x relative luminance and contrast ratio. */
function luminance(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((x) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0]! + 0.7152 * c[1]! + 0.0722 * c[2]!;
}
function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x! + 0.05) / (y! + 0.05);
}

const ORDER = (Object.keys(QUALITY_RANK) as QualityLabel[]).sort((a, b) => QUALITY_RANK[a] - QUALITY_RANK[b]);
// Vitest runs from frontend/ (vitest.config.mts).
const CSS = readFileSync(join(process.cwd(), "app/globals.css"), "utf8");
const cssVar = (block: string, name: string) => new RegExp(`--q-${name}:\\s*(#[0-9a-f]{6})`, "i").exec(block)?.[1]?.toLowerCase();

describe("surf quality palette", () => {
  it("is ordered: poor to exceptional", () => {
    expect(ORDER).toEqual(["poor", "fair", "good", "very_good", "excellent", "exceptional"]);
  });

  it("has at least 3:1 contrast against the card surface, light and dark", () => {
    for (const q of ORDER) {
      expect(contrast(QUALITY_COLORS[q], QUALITY_SURFACES.light), `${q} on light`).toBeGreaterThanOrEqual(3);
      expect(contrast(QUALITY_COLORS_DARK[q], QUALITY_SURFACES.dark), `${q} on dark`).toBeGreaterThanOrEqual(3);
    }
  });

  it("orders lightness strictly (deeper = better on light, brighter = better on dark)", () => {
    const light = ORDER.map((q) => luminance(QUALITY_COLORS[q]));
    const dark = ORDER.map((q) => luminance(QUALITY_COLORS_DARK[q]));
    for (let i = 1; i < ORDER.length; i++) {
      expect(light[i]!, ORDER[i]).toBeLessThan(light[i - 1]!);
      expect(dark[i]!, ORDER[i]).toBeGreaterThan(dark[i - 1]!);
    }
  });

  it("keeps its labels readable (4.5:1) on every class colour", () => {
    for (const q of ORDER) {
      expect(contrast(QUALITY_TEXT[q], QUALITY_COLORS[q]), q).toBeGreaterThanOrEqual(4.5);
      expect(contrast(QUALITY_TEXT_DARK[q], QUALITY_COLORS_DARK[q]), q).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("matches the --q-* tokens in globals.css", () => {
    const root = CSS.slice(CSS.indexOf(":root"), CSS.indexOf("}", CSS.indexOf(":root")));
    const dark = CSS.slice(CSS.indexOf(".dark {"), CSS.indexOf("}", CSS.indexOf(".dark {")));
    for (const q of ORDER) {
      const name = q.replace("_", "-");
      expect(cssVar(root, name), q).toBe(QUALITY_COLORS[q].toLowerCase());
      expect(cssVar(dark, name), q).toBe(QUALITY_COLORS_DARK[q].toLowerCase());
    }
  });

  it("colours markers by score, grey without a forecast", () => {
    expect(colorForScore(null)).toBe(NO_FORECAST_COLOR);
    expect(colorForScore(95)).toBe(QUALITY_COLORS.exceptional);
    expect(colorForScore(10)).toBe(QUALITY_COLORS.poor);
    expect(colorForScore(95, "dark")).toBe(QUALITY_COLORS_DARK.exceptional);
  });
});
