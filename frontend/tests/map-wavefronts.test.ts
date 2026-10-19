import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { systemsIn, waveFamilies } from "@/components/map/map-canvas-layers";
import { buildLattice, dashBudget, dashCellSize, dashGrid, LATTICE_PX, MAX_CORNER_SPREAD_DEG, type WaveFamilyGrids } from "@/components/map/wavefronts";
import { sampleDirection, type FieldGrid, type Grid } from "@/lib/map-fields";
import { mercLon } from "@/lib/map-geo";
import { crestSpacingPx, crestSpeedPxPerS, FAMILY_STYLES } from "@/lib/map-waves";

import { constant, cores, dashesOf, draw, family, globalGrid, installPath2D, orientation, rgbOf, viewAt } from "./map-dash-fixtures";

// Every grid below is SYNTHETIC test data, not model output. The dash geometry, motion,
// spacing, masking, budget and reduced-motion checks of D11 are in map-dashes.test.ts.

beforeEach(() => {
  // jsdom has no 2D canvas (the land and ocean masks are skipped) and no Path2D.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  installPath2D();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("dashes follow the local field", () => {
  it("turns the dashes with a field that turns: each lies along its local crest", () => {
    // From 270 in the west of the view, turning to 230 in the east (SYNTHETIC).
    const dir = globalGrid((_lat, lon) => 270 - 40 * Math.min(1, Math.max(0, (lon + 60) / 60)));
    const dashes = dashesOf(draw(viewAt(3, -40, -30), [family("primary", 3, 12, dir)], 2).lines);
    const mean = (ds: typeof dashes) => ds.reduce((a, d) => a + orientation(d), 0) / ds.length;
    const west = dashes.filter((d) => d.x < 300);
    const east = dashes.filter((d) => d.x > 1100);
    expect(west.length).toBeGreaterThan(50);
    expect(east.length).toBeGreaterThan(50);
    expect(Math.abs(mean(west) - 90)).toBeLessThan(3); // crests of waves from the W: vertical
    expect(Math.abs(mean(east) - 50)).toBeLessThan(3); // from 230: crests along 50° on screen
  });

  it("leaves a lattice cell invalid where the grid corners hold different systems (100° vs 200°)", () => {
    // Partitions are numbered per cell, so neighbouring cells can hold different swells: no
    // averaged direction (150°) that no wave has is invented.
    const g: Grid = { nx: 2, ny: 2, lat0: 1, lon0: 0, dlat: 1, dlon: 1, values: Float32Array.from([100, 200, 200, 100]) };
    expect(sampleDirection(g, 0.5, 0.5, 0.5, MAX_CORNER_SPREAD_DEG)).toBeNaN();
    const close: Grid = { ...g, values: Float32Array.from([100, 140, 140, 100]) };
    expect(sampleDirection(close, 0.5, 0.5, 0.5, MAX_CORNER_SPREAD_DEG)).toBeCloseTo(120, 6);
    const checker = globalGrid((lat, lon) => ((lat + lon) & 1 ? 100 : 200));
    const swapped = buildLattice(viewAt(3, -40, -30), [family("primary", 3, 12, checker)]);
    const plain = buildLattice(viewAt(3, -40, -30), [family("primary", 3, 12, 100)]);
    expect(plain.families[0]!.validCount).toBeGreaterThan(10_000);
    // Only nodes exactly on a grid node (one corner) keep a direction; no dash is drawn.
    expect(swapped.families[0]!.validCount).toBeLessThan(plain.families[0]!.validCount / 1000);
    expect(draw(viewAt(3, -40, -30), [family("primary", 3, 12, checker)]).stats.crests).toBe(0);
  });

  it("puts dashes only where their system has data, also in cells that are partly without data", () => {
    // SYNTHETIC: data in 10° bands of longitude, none in the 10° bands between them.
    const view = viewAt(3, -40, -30);
    const banded = (v: number) => globalGrid((_lat, lon) => (Math.floor((lon + 180) / 10) % 2 ? NaN : v));
    const full = draw(view, [family("primary", 3, 12, 250)], 3).stats.crests;
    const { lines, stats } = draw(view, [family("primary", banded(3), banded(12), banded(250))], 3);
    // Half the bands have data, and a dash moves about 76 px east in its life across 57 px
    // bands, hidden wherever it is over a band without data: about a fifth of the dashes of
    // the full sea are drawn, in every band with data.
    expect(stats.crests).toBeGreaterThan(0.15 * full);
    const bands = new Set<number>();
    for (const d of dashesOf(lines)) {
      const lon = mercLon((view.originX + d.x) / view.worldPx);
      // (within half a grid cell of a band's edge the bilinear sample still has data)
      const dataNear = [lon - 0.5, lon, lon + 0.5].some((x) => Math.floor((x + 180) / 10) % 2 === 0);
      expect(dataNear, `dash at ${lon.toFixed(2)}°`).toBe(true);
      bands.add(Math.floor((lon + 180) / 10));
    }
    const inView = [mercLon(view.originX / view.worldPx), mercLon((view.originX + view.width) / view.worldPx)].map((lon) => Math.floor((lon + 180) / 10));
    for (let b = inView[0]! + 1; b < inView[1]!; b++) if (b % 2 === 0) expect(bands.has(b), `band ${b}`).toBe(true);
  });
});

describe("one crest lattice everywhere", () => {
  it("keeps one crest lattice across the antimeridian and world copies (no seam)", () => {
    // A 1440 px view at zoom 2 shows 1.4 worlds. With the phase measured in one world copy,
    // the dashes east of 180° would sit 1024 mod 48 = 17 px off those west of it.
    const tp = 16;
    const s = crestSpacingPx(tp);
    const v = crestSpeedPxPerS(tp, s);
    const t = 1.3;
    const view = viewAt(2, -45, 180);
    const dashes = dashesOf(draw(view, [family("primary", 3, tp, 270)], t, false, 1e6).lines);
    expect(dashes.some((d) => d.x < 300)).toBe(true);
    expect(dashes.some((d) => d.x > 1140)).toBe(true);
    for (const d of dashes) {
      const m = (((d.x + view.originX - v * t) % s) + s) % s;
      expect(Math.min(m, s - m)).toBeLessThan(0.01 * s);
    }
  });

  it("puts lattice nodes and dash cells on grids fixed in world pixels", () => {
    const a = viewAt(3, -40, -30);
    const l = buildLattice(a, [family("primary", 3, 12, 250)]);
    expect(((l.x0 + a.originX) / LATTICE_PX) % 1).toBeCloseTo(0, 9);
    expect(((l.y0 + a.originY) / LATTICE_PX) % 1).toBeCloseTo(0, 9);
    // Cells divide the world exactly (no seam), close to the stated size at every zoom.
    for (const z of [2, 3, 5.5, 9]) {
      const view = viewAt(z, 0, 0);
      for (const system of ["primary", "secondary", "windsea"] as const) {
        const cell = dashCellSize(FAMILY_STYLES[system], view);
        expect((view.worldPx / cell) % 1).toBeCloseTo(0, 6);
        expect(Math.abs(cell - FAMILY_STYLES[system].cellPx) / FAMILY_STYLES[system].cellPx).toBeLessThan(0.03);
      }
    }
    // The cells cover the view plus a margin of three crest spacings (how far a dash travels).
    const g = dashGrid(l, l.families[0]!, a);
    const xs = g.cells.map((c) => c.ci * g.cell - a.originX);
    expect(Math.min(...xs)).toBeLessThan(-3 * crestSpacingPx(12) + 1);
    expect(Math.max(...xs)).toBeGreaterThan(a.width + 3 * crestSpacingPx(12) - g.cell - 1);
  });
});

describe("systems", () => {
  it("draws no secondary dashes when the run has no secondary swell", () => {
    const grid = (g: Grid, variable: FieldGrid["variable"]): FieldGrid => ({ ...g, validTime: "2026-10-14T12:00:00Z", runKey: "synthetic", variable });
    const fields = {
      hs: grid(constant(3), "hs"),
      tp: grid(constant(12), "tp"),
      wave_dir: grid(constant(250), "wave_dir"),
      swell1_hs: grid(constant(2.5), "swell1_hs"),
      swell1_tp: grid(constant(14), "swell1_tp"),
      swell1_dir: grid(constant(250), "swell1_dir"),
    };
    expect([...systemsIn(fields)].sort()).toEqual(["combined", "primary"]);
    expect(waveFamilies("hs", fields, ["primary", "secondary"]).map((f) => f.system)).toEqual(["primary"]);
    expect(waveFamilies("hs", fields, ["secondary"]).map((f) => f.system)).toEqual(["combined"]); // never a made-up secondary
    expect(waveFamilies("hs", fields, ["combined"]).map((f) => f.system)).toEqual(["combined"]); // the default
    expect(waveFamilies("wind", fields, ["primary"])).toEqual([]); // the Wind layer draws particles, not dashes
    // A secondary grid without any data gives no secondary dashes at all.
    const { stats, lines } = draw(viewAt(3, -40, -30), [family("secondary", NaN, NaN, NaN), family("primary", 3, 12, 250)]);
    expect(stats.families).toEqual(["primary"]);
    expect(lines.some((l) => rgbOf(l) === "255, 213, 154")).toBe(false);
    expect(lines.some((l) => rgbOf(l) === "255, 255, 255")).toBe(true);
  });

  it("draws each system with its own dash shape and colour", () => {
    const fams: WaveFamilyGrids[] = [family("windsea", 1.5, 6, 280), family("secondary", 1.5, 10, 200), family("primary", 3, 14, 250)];
    const { lines } = draw(viewAt(3, -40, -30), fams, 2);
    const byColour = new Map<string, number[]>();
    for (const l of cores(lines)) {
      const len = Math.hypot(l.pts[1]![0] - l.pts[0]![0], l.pts[1]![1] - l.pts[0]![1]);
      byColour.set(rgbOf(l), [...(byColour.get(rgbOf(l)) ?? []), len]);
    }
    const max = (c: string) => Math.max(...byColour.get(c)!);
    // primary: one solid white segment per dash; secondary: amber, a long dash split into two
    // pieces with a gap; wind sea: short cyan dashes.
    expect(max("255, 255, 255")).toBeCloseTo(9.5, 6); // 5 + 1.5 x 3 m
    expect(max("255, 213, 154")).toBeLessThan(0.5 * 1.4 * 7.25); // each piece is less than half the dash
    expect(max("184, 240, 255")).toBeCloseTo(0.55 * 7.25, 6);
    expect(byColour.size).toBe(3);
    // Twice as many secondary core segments as dashes drawn of it (each split in two).
    const sec = cores(lines).filter((l) => rgbOf(l) === "255, 213, 154");
    expect(sec.length % 2).toBe(0);
  });

  it("gives more ink to a higher sea", () => {
    const ink = (hs: number) =>
      draw(viewAt(3, -40, -30), [family("primary", hs, 12, 250)], 3).lines.reduce((a, l) => {
        const len = Math.hypot(l.pts[1]![0] - l.pts[0]![0], l.pts[1]![1] - l.pts[0]![1]);
        return a + (len + l.width) * l.width * Number(l.style.slice(l.style.lastIndexOf(",") + 1, -1));
      }, 0);
    expect(ink(4)).toBeGreaterThan(2 * ink(1));
  });

  it("budgets 4,000 dashes on desktops and 1,500 on phones", () => {
    expect(dashBudget(1440)).toBe(4000);
    expect(dashBudget(390)).toBe(1500);
  });
});
