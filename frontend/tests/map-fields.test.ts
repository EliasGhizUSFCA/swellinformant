// @vitest-environment node
import { deflateSync } from "node:zlib";

import { describe, expect, it, vi } from "vitest";

import {
  decodeField,
  directionFromUV,
  FieldStore,
  inflate,
  LruCache,
  parseFieldMeta,
  sampleDirection,
  sampleGrid,
  waveEnergyGrid,
  waveEnergyKwPerM,
  waveFlowGrids,
  windSpeedGrid,
  wrapsLongitude,
  type FieldGrid,
  type FieldMeta,
} from "@/lib/map-fields";

// All grids below are SYNTHETIC test data, not model output.
const META: FieldMeta = {
  variable: "hs",
  unit: "m",
  valid_time: "2026-10-11T12:00:00Z",
  run_key: "synthetic",
  nx: 4,
  ny: 3,
  lat0: 10,
  lon0: -180,
  dlat: 10,
  dlon: 90,
  scale: 0.1,
  offset: 0,
  nodata: 255,
};

function grid(values: number[], over: Partial<FieldGrid> = {}): FieldGrid {
  return { nx: 4, ny: 3, lat0: 10, lon0: -180, dlat: 10, dlon: 90, values: Float32Array.from(values), ...over };
}

describe("field decoding", () => {
  it("parses and validates X-Field-Meta", () => {
    expect(parseFieldMeta(JSON.stringify(META)).nx).toBe(4);
    const withoutNodata: Partial<FieldMeta> = { ...META };
    delete withoutNodata.nodata;
    expect(parseFieldMeta(JSON.stringify(withoutNodata)).nodata).toBe(255);
    expect(() => parseFieldMeta(null)).toThrow(/Missing/);
    expect(() => parseFieldMeta(JSON.stringify({ ...META, nx: "4" }))).toThrow(/nx/);
    expect(() => parseFieldMeta(JSON.stringify({ ...META, dlat: 0 }))).toThrow(/spacing/);
    expect(() => parseFieldMeta("{not json")).toThrow();
  });

  it("maps bytes to offset + scale * byte, nodata to NaN, row 0 north", () => {
    const bytes = Uint8Array.from([0, 10, 255, 20, 1, 2, 3, 4, 250, 251, 252, 253]);
    const g = decodeField(bytes, { ...META, offset: 1, scale: 0.5 });
    expect(g.values[0]).toBe(1);
    expect(g.values[1]).toBe(6);
    expect(g.values[2]).toBeNaN();
    expect(g.values[3]).toBe(11);
    expect(g.values[8]).toBe(126);
    expect(() => decodeField(new Uint8Array(5), META)).toThrow(/expected 12/);
  });

  it("inflates zlib bytes with DecompressionStream('deflate')", async () => {
    const raw = Uint8Array.from({ length: 1000 }, (_, i) => i % 251);
    const out = await inflate(deflateSync(raw));
    expect(Array.from(out)).toEqual(Array.from(raw));
  });
});

describe("bilinear sampling", () => {
  // columns at lon -180, -90, 0, 90 (wraps: 4 * 90 = 360); rows at lat 10, 0, -10
  const g = grid([0, 1, 2, 3, 10, 11, 12, 13, 20, 21, 22, 23]);

  it("detects global grids", () => {
    expect(wrapsLongitude(g)).toBe(true);
    expect(wrapsLongitude({ nx: 3, dlon: 90 })).toBe(false);
  });

  it("interpolates inside the grid", () => {
    expect(sampleGrid(g, 10, -180)).toBe(0);
    expect(sampleGrid(g, 5, -135)).toBeCloseTo(5.5);
    expect(sampleGrid(g, -10, 90)).toBe(23);
  });

  it("wraps longitude across the antimeridian", () => {
    // halfway between lon 90 (value 3) and lon 180 = -180 (value 0)
    expect(sampleGrid(g, 10, 135)).toBeCloseTo(1.5);
    expect(sampleGrid(g, 10, -225)).toBeCloseTo(1.5);
    expect(sampleGrid(g, 10, 495)).toBeCloseTo(1.5);
  });

  it("returns NaN outside the latitude range and outside a regional grid", () => {
    expect(sampleGrid(g, 11, 0)).toBeNaN();
    const regional = grid([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], { nx: 4, dlon: 10, lon0: 0 });
    expect(sampleGrid(regional, 0, 31)).toBeNaN();
    expect(sampleGrid(regional, 0, 30)).toBe(7);
  });

  it("drops no-data corners and renormalises, down to minWeight", () => {
    const holey = grid([NaN, 4, 2, 3, 10, 11, 12, 13, 20, 21, 22, 23]);
    expect(sampleGrid(holey, 10, -180)).toBeNaN(); // the cell itself has no data
    // 3/4 of the weight valid: average of the valid corners
    expect(sampleGrid(holey, 5, -135)).toBeCloseTo((4 + 10 + 11) / 3);
    // mostly over the missing cell: valid weight 0.19 < 0.5 → NaN, unless minWeight is low
    expect(sampleGrid(holey, 9, -171)).toBeNaN();
    expect(sampleGrid(holey, 9, -171, 0.01)).toBeGreaterThan(4);
  });

  it("interpolates directions as unit vectors (350° and 10° → 0°, never 180°)", () => {
    const dirs = grid([350, 10, 90, 90, 350, 10, 90, 90, 350, 10, 90, 90]);
    expect(sampleDirection(dirs, 10, -135)).toBeCloseTo(0, 5);
    expect(sampleDirection(dirs, 10, -180)).toBeCloseTo(350, 5);
    // exactly opposite corners cancel: no meaningful direction
    const opposite = grid([0, 180, 0, 0, 0, 180, 0, 0, 0, 180, 0, 0]);
    expect(sampleDirection(opposite, 10, -135)).toBeNaN();
  });
});

describe("derived quantities", () => {
  it("wind speed is |(u, v)| and direction FROM follows the meteorological convention", () => {
    const u = grid([3, 0, NaN, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
    const v = grid([4, -5, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
    const s = windSpeedGrid(u, v);
    expect(s.values[0]).toBe(5);
    expect(s.values[1]).toBe(5);
    expect(s.values[2]).toBeNaN();
    expect(directionFromUV(0, -5)).toBeCloseTo(0); // blowing south = from the north
    expect(directionFromUV(5, 0)).toBeCloseTo(270); // blowing east = from the west
    expect(directionFromUV(0, 5)).toBeCloseTo(180);
    expect(directionFromUV(-5, 0)).toBeCloseTo(90);
    expect(directionFromUV(0, 0)).toBeNaN();
  });

  it("wave energy: P = 0.49 Hs² · 0.9 Tp kW/m", () => {
    expect(waveEnergyKwPerM(2, 10)).toBeCloseTo(0.49 * 4 * 9);
    expect(waveEnergyKwPerM(0, 12)).toBe(0);
    const e = waveEnergyGrid(grid([1, 2, NaN, 0, 0, 0, 0, 0, 0, 0, 0, 0]), grid([10, 10, 10, 0, 0, 0, 0, 0, 0, 0, 0, 0]));
    expect(e.values[0]).toBeCloseTo(4.41);
    expect(e.values[1]).toBeCloseTo(17.64);
    expect(e.values[2]).toBeNaN();
    expect(() => waveEnergyGrid(grid(new Array(12).fill(1)), grid(new Array(12).fill(1), { dlat: 5 }))).toThrow(/different grids/);
  });

  it("wave flow points TOWARDS direction + 180°, scaled by period", () => {
    // waves from the west (270°) travel east: u > 0, v ≈ 0; from the north travel south
    const f = waveFlowGrids(grid([270, 0, NaN, 0, 0, 0, 0, 0, 0, 0, 0, 0]), grid([10, 20, 10, 0, 0, 0, 0, 0, 0, 0, 0, 0]));
    expect(f.u.values[0]).toBeCloseTo(1);
    expect(f.v.values[0]).toBeCloseTo(0);
    expect(f.u.values[1]).toBeCloseTo(0);
    expect(f.v.values[1]).toBeCloseTo(-2);
    expect(f.u.values[2]).toBeNaN();
  });
});

describe("caching", () => {
  it("LRU evicts the least recently used entry", () => {
    const c = new LruCache<string, number>(2);
    c.set("a", 1);
    c.set("b", 2);
    expect(c.get("a")).toBe(1); // a is now the most recent
    c.set("c", 3);
    expect(c.has("b")).toBe(false);
    expect(c.has("a")).toBe(true);
    expect(c.size).toBe(2);
  });

  it("FieldStore shares in-flight loads and forgets failures", async () => {
    const g = grid(new Array(12).fill(1));
    const loader = vi.fn(async (variable: string) => {
      if (variable === "bad") throw new Error("HTTP 404");
      return g;
    });
    const s = new FieldStore(24, loader);
    const [a, b] = await Promise.all([s.load("hs", "t1"), s.load("hs", "t1")]);
    expect(a).toBe(b);
    expect(loader).toHaveBeenCalledTimes(1);
    await expect(s.load("bad", "t1")).rejects.toThrow("404");
    expect(s.has("bad", "t1")).toBe(false);
    await expect(s.load("bad", "t1")).rejects.toThrow("404");
    expect(loader).toHaveBeenCalledTimes(3);
  });

  it("FieldStore keeps at most `capacity` fields", async () => {
    const s = new FieldStore(3, async () => grid(new Array(12).fill(0)));
    for (const t of ["t1", "t2", "t3", "t4"]) await s.load("hs", t);
    expect(s.has("hs", "t1")).toBe(false);
    expect(s.has("hs", "t4")).toBe(true);
  });
});
