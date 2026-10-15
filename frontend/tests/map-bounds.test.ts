// @vitest-environment node
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  BAND_HEIGHT,
  LAT_NORTH,
  LAT_SOUTH,
  LNG_LIMIT,
  MAP_MAX_BOUNDS,
  minZoomFor,
  nearestCopy,
  RECENTRE_LNG,
  wrapCentre,
  wrapKey,
} from "@/lib/map-bounds";
import { MAX_LAT, mercX, mercY } from "@/lib/map-geo";

// The 50 monitored spots, from the seed data the backend loads (vitest runs in frontend/).
const spots = JSON.parse(readFileSync(resolve(process.cwd(), "../data/surf_spots/spots.json"), "utf8")) as Array<{
  slug: string;
  latitude: number;
  longitude: number;
}>;
const bySlug = (slug: string) => spots.find((s) => s.slug === slug)!;

describe("map bounds", () => {
  it("keeps the band inside the Mercator world", () => {
    expect(LAT_NORTH).toBe(84);
    expect(LAT_SOUTH).toBe(-78.5);
    expect(LAT_NORTH).toBeLessThan(MAX_LAT);
    expect(LAT_SOUTH).toBeGreaterThan(-MAX_LAT);
    expect(MAP_MAX_BOUNDS).toEqual([
      [-78.5, -1e5],
      [84, 1e5],
    ]);
    expect(LNG_LIMIT).toBe(1e5);
    expect(BAND_HEIGHT).toBeCloseTo(0.8347, 4);
  });

  it.each([
    [1440, 807],
    [1920, 987],
    [2560, 1311],
    [1024, 703],
    [768, 959],
    [390, 735],
    [360, 575],
    [844, 325],
  ])("at the minimum zoom a %ix%i map shows each longitude once and no rows outside the band", (w, h) => {
    const z = minZoomFor(w, h);
    const worldPx = 256 * 2 ** z;
    expect(worldPx).toBeGreaterThanOrEqual(w - 1e-6);
    expect(worldPx * BAND_HEIGHT).toBeGreaterThanOrEqual(h - 1e-6);
    // It is the smallest such zoom: one of the two limits is tight.
    expect(Math.min(Math.abs(worldPx - w), Math.abs(worldPx * BAND_HEIGHT - h))).toBeLessThan(1e-6);
  });

  it("gives the zooms the audit measured and the floor for an unmeasured map", () => {
    expect(minZoomFor(1440, 807)).toBeCloseTo(2.49, 2);
    expect(minZoomFor(1920, 987)).toBeCloseTo(2.91, 2);
    expect(minZoomFor(390, 735)).toBeCloseTo(1.78, 2);
    expect(minZoomFor(0, 0)).toBe(1);
    expect(minZoomFor(Number.NaN, 500, 2)).toBe(2);
    expect(minZoomFor(100, 100)).toBe(1); // a tiny map stops at the floor
  });

  it("has every spot inside the band with at least 25 degrees to spare", () => {
    expect(spots).toHaveLength(50);
    for (const s of spots) {
      expect(s.latitude, s.slug).toBeLessThan(LAT_NORTH - 25);
      expect(s.latitude, s.slug).toBeGreaterThan(LAT_SOUTH + 25);
    }
  });

  it("puts all 50 spots on one desktop screen at the minimum zoom (one world wide)", () => {
    const [w, h] = [1440, 807];
    const worldPx = 256 * 2 ** minZoomFor(w, h);
    const ys = spots.map((s) => mercY(s.latitude) * worldPx);
    expect(Math.max(...ys) - Math.min(...ys)).toBeLessThan(h); // fits vertically
    const centre = 10; // the initial view's centre longitude
    const xs = spots.map((s) => (mercX(nearestCopy(s.longitude, centre)) - mercX(centre)) * worldPx);
    expect(Math.max(...xs.map(Math.abs))).toBeLessThanOrEqual(w / 2); // each in the one visible copy
  });

  it("draws date-line neighbours next to each other", () => {
    const fiji = bySlug("cloudbreak");
    const hawaii = bySlug("pipeline");
    const tahiti = bySlug("teahupoo");
    expect(nearestCopy(hawaii.longitude, fiji.longitude)).toBeCloseTo(201.947, 3);
    expect(nearestCopy(tahiti.longitude, 180) - nearestCopy(fiji.longitude, 180)).toBeCloseTo(33.5605, 3);
    // The Fiji -> Hawaii flight crosses 24.8 degrees of longitude, not 335.
    expect(Math.abs(nearestCopy(hawaii.longitude, fiji.longitude) - fiji.longitude)).toBeCloseTo(24.75, 1);
  });

  it.each([-540, -181, -180, -179.99, 0, 179.99, 180, 181, 725.5])("nearestCopy stays within 180 degrees of centre %d on the same meridian", (c) => {
    for (const s of spots) {
      const l = nearestCopy(s.longitude, c);
      expect(Math.abs(l - c)).toBeLessThanOrEqual(180);
      expect((((l - s.longitude) % 360) + 360) % 360).toBeCloseTo(0, 9);
    }
  });

  it("wrapCentre maps any longitude to the same meridian in [-180, 180)", () => {
    for (const lng of [-1e5, -900, -540.5, -181, -180, -179.99, 0, 179.99, 180, 181, 541, 725.5, 99_999]) {
      const w = wrapCentre(lng);
      expect(w).toBeGreaterThanOrEqual(-180);
      expect(w).toBeLessThan(180);
      expect((((w - lng) % 360) + 360) % 360).toBeCloseTo(0, 6);
    }
    expect(wrapCentre(201.95)).toBeCloseTo(-158.05, 6);
    expect(wrapCentre(-200)).toBeCloseTo(160, 6);
    expect(RECENTRE_LNG).toBe(540);
  });

  it("wrapKey changes only when a longitude crosses the centre ± 180", () => {
    const lons = spots.map((s) => s.longitude);
    // Panning east from 10E: Waimea (-158.07) moves to the next copy when the centre passes
    // 21.93E (= -158.07 + 180), and no sooner.
    const waimea = bySlug("waimea-bay").longitude;
    const flip = waimea + 180;
    expect(wrapKey(lons, flip - 0.01)).toBe(wrapKey(lons, 10));
    expect(wrapKey(lons, flip + 0.01)).not.toBe(wrapKey(lons, flip - 0.01));
    // Between two crossings the key holds still, however far the view moves.
    const crossings = lons.map((l) => l + 180).map(wrapCentre).sort((a, b) => a - b);
    for (let i = 0; i + 1 < crossings.length; i++) {
      const [a, b] = [crossings[i]!, crossings[i + 1]!];
      if (b - a < 1e-6) continue;
      expect(wrapKey(lons, a + 1e-6)).toBe(wrapKey(lons, b - 1e-6));
    }
    // A whole world later, every copy index is one higher.
    expect(wrapKey(lons, 370)).toBe(lons.map((l) => Math.round((10 - l) / 360) + 1).join(","));
  });
});
