import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { systemsIn, waveFamilies } from "@/components/map/map-canvas-layers";
import { buildLattice, dashBudget, dashCellSize, dashGrid, LATTICE_PX, MAX_CORNER_SPREAD_DEG, type WaveFamilyGrids } from "@/components/map/wavefronts";
import { sampleDirection, type FieldGrid, type Grid } from "@/lib/map-fields";
import { mercLon } from "@/lib/map-geo";
import { crestSpacingPx, crestSpeedPxPerS, FAMILY_STYLES } from "@/lib/map-waves";

import { constant, dashesOf, draw, family, globalGrid, installPath2D, orientation, rgbOfQuad, UNBOUNDED, viewAt } from "./map-dash-fixtures";

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
    const dashes = dashesOf(draw(viewAt(3, -40, -30), [family("primary", 3, 12, dir)], 2).quads);
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
    const { quads, stats } = draw(view, [family("primary", banded(3), banded(12), banded(250))], 3);
    // Half the bands have data, and a dash moves about 76 px east in its life across 57 px
    // bands, hidden wherever it is over a band without data: about a fifth of the dashes of
    // the full sea are drawn, in every band with data.
    expect(stats.crests).toBeGreaterThan(0.15 * full);
    const bands = new Set<number>();
    for (const d of dashesOf(quads)) {
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
    const dashes = dashesOf(draw(view, [family("primary", 3, tp, 270)], t, false, UNBOUNDED).quads);
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

  it("gives every dash cell its own state key, also at the street basemap's zoom 19", () => {
    // At zoom 19 the world has about 11.2 million rows of 12 px cells. Two views exactly one
    // column and 2^21 rows apart: a key packed for fewer rows would give their cells the same
    // dash states (the cell one column east and 2^21 rows south shares the key).
    const worldPx = 256 * 2 ** 19;
    const cell = dashCellSize(FAMILY_STYLES.combined, { originX: 0, originY: 0, width: 400, height: 300, worldPx });
    const grid = (originX: number, originY: number) => {
      const view = { originX, originY, width: 400, height: 300, worldPx };
      const l = buildLattice(view, [family("combined", 2, 10, 250)]);
      return dashGrid(l, l.families[0]!, view);
    };
    const a = grid(0.3 * worldPx, 0.25 * worldPx);
    const b = grid(0.3 * worldPx + cell, 0.25 * worldPx + 2 ** 21 * cell);
    expect(a.cells.length).toBeGreaterThan(100);
    expect(b.cells.length).toBe(a.cells.length);
    const keys = new Map<number, string>();
    for (const c of [...a.cells, ...b.cells]) {
      const id = `${c.ci},${c.cj}`;
      expect(keys.get(c.key) ?? id, `key ${c.key}`).toBe(id);
      keys.set(c.key, id);
    }
    expect(keys.size).toBe(2 * a.cells.length);
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
    const { stats, quads } = draw(viewAt(3, -40, -30), [family("secondary", NaN, NaN, NaN), family("primary", 3, 12, 250)]);
    expect(stats.families).toEqual(["primary"]);
    expect(quads.some((q) => rgbOfQuad(q) === "255, 207, 133")).toBe(false);
    expect(quads.some((q) => rgbOfQuad(q) === "255, 255, 255")).toBe(true);
  });

  it("draws each system with its own dash shape and colour", () => {
    const fams: WaveFamilyGrids[] = [family("windsea", 1.5, 6, 280), family("secondary", 1.5, 10, 200), family("primary", 3, 14, 250)];
    const { quads, stats } = draw(viewAt(3, -40, -30), fams, 2);
    const byColour = new Map<string, ReturnType<typeof dashesOf>>();
    for (const d of dashesOf(quads)) byColour.set(d.rgb, [...(byColour.get(d.rgb) ?? []), d]);
    const max = (c: string) => Math.max(...byColour.get(c)!.map((d) => d.length));
    // (No colour field under them here: each system in its own colour.) Primary: one soft blob
    // per dash; secondary: amber bars, a long dash with a gap of 0.2 of its length in the middle
    // of its fill; wind sea: short cyan blobs.
    expect(max("255, 255, 255")).toBeCloseTo(8.4, 6); // 6 + 0.8 x 3 m
    expect(max("255, 207, 133")).toBeCloseTo(1.8 * 7.2, 6);
    expect(max("156, 236, 255")).toBeCloseTo(0.65 * 7.2, 6);
    expect(byColour.size).toBe(3);
    expect(byColour.get("255, 255, 255")!.every((d) => d.kind === "blob" && d.gap === 0)).toBe(true);
    expect(byColour.get("156, 236, 255")!.every((d) => d.kind === "blob" && d.gap === 0)).toBe(true);
    const sec = byColour.get("255, 207, 133")!;
    for (const d of sec) {
      expect(d.kind).toBe("bar");
      expect(d.gap).toBeCloseTo(FAMILY_STYLES.secondary.gap, 9);
      // Each piece either side of the gap is less than half the dash.
      expect((d.length * (1 - d.gap)) / 2).toBeLessThan(0.5 * d.length);
    }
    // One quad per dash, whatever its shape.
    expect(stats.quads).toBe(stats.crests);
  });

  it("gives more ink to a higher sea", () => {
    // Blob area x opacity (every blob has the same opacity profile across its depth).
    const ink = (hs: number) => dashesOf(draw(viewAt(3, -40, -30), [family("primary", hs, 12, 250)], 3).quads).reduce((a, d) => a + d.length * d.depth * d.alpha, 0);
    expect(ink(4)).toBeGreaterThan(2 * ink(1));
  });

  it("budgets 8,000 dashes on desktops and 3,000 on phones", () => {
    expect(dashBudget(1440)).toBe(8000);
    expect(dashBudget(390)).toBe(3000);
  });
});
