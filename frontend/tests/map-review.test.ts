// @vitest-environment node
import { describe, expect, it, vi } from "vitest";

import { buildLut, LAYER_SCALES } from "@/lib/map-colors";
import { FieldStore, fieldUrl, parseFieldMeta, type FieldGrid } from "@/lib/map-fields";
import { mercX, mercY, type PixelView } from "@/lib/map-geo";
import { LAYERS } from "@/lib/map-layers";
import { renderField } from "@/lib/map-render";
import { bubbleLabel, timelineDays, weekdaysRepeat } from "@/lib/map-time";

// All grids below are SYNTHETIC test data, not model output.

/** Global 0.5° grid (720 × 361, row 0 = 90°N, column 0 = 180°W): 9 at one node, 0.5 elsewhere. */
function hotNode(lat: number, lon: number): FieldGrid {
  const nx = 720;
  const ny = 361;
  const values = new Float32Array(nx * ny).fill(0.5);
  values[Math.round((90 - lat) / 0.5) * nx + Math.round((lon + 180) / 0.5)] = 9;
  return { nx, ny, lat0: 90, lon0: -180, dlat: 0.5, dlon: 0.5, values };
}

/** Renders `g` at `zoom` centred on (lat, lon) and returns the brightest pixel's offset from the centre, in CSS px. */
function peakOffset(g: FieldGrid, lat: number, lon: number, zoom: number, k = 2) {
  const worldPx = 256 * 2 ** zoom;
  const width = 400;
  const height = 400;
  const view: PixelView = { originX: mercX(lon) * worldPx - width / 2, originY: mercY(lat) * worldPx - height / 2, width, height, worldPx };
  const W = Math.ceil(width / k);
  const H = Math.ceil(height / k);
  const out = { width: W, height: H, data: new Uint8ClampedArray(W * H * 4) };
  // A black-to-red ramp, so the red channel rises with the value (the map's ramp is not monotonic in red).
  const lut = buildLut([
    { at: 0, color: "#000000" },
    { at: 1, color: "#ff0000" },
  ]);
  renderField(out, view, k, { grid: g, scale: LAYER_SCALES.hs, lut, minWeight: 0.5, alpha: 255 });
  let best = -1;
  let bx = 0;
  let by = 0;
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const r = out.data[(y * W + x) * 4]!;
      if (r > best) {
        best = r;
        bx = x;
        by = y;
      }
    }
  }
  return { dx: (bx + 0.5) * k - width / 2, dy: (by + 0.5) * k - height / 2 };
}

describe("field rendering geography", () => {
  it.each([
    [60, -30],
    [-20, 150],
    [0, -180],
    [45.5, 179.5],
  ])("puts a single hot node at %s, %s exactly where Web Mercator puts that position", (lat, lon) => {
    // zoom 8: a 0.5° cell is 91 px wide, so a half-cell (or one-row) error would be ≥ 45 px.
    const p = peakOffset(hotNode(lat, lon), lat, lon, 8);
    expect(Math.abs(p.dx)).toBeLessThanOrEqual(2);
    expect(Math.abs(p.dy)).toBeLessThanOrEqual(2);
  });
});

describe("field cache per model run", () => {
  it("never reuses a field of another run for the same variable and time", async () => {
    const loader = vi.fn(async (_v: string, _at: string, run?: string | null) => hotNode(run === "b" ? 10 : 0, 0));
    const s = new FieldStore(24, loader);
    const a = await s.load("hs", "t1", "a");
    expect(await s.load("hs", "t1", "a")).toBe(a);
    const b = await s.load("hs", "t1", "b");
    expect(b).not.toBe(a);
    expect(loader).toHaveBeenCalledTimes(2);
    expect(s.has("hs", "t1", "a") && s.has("hs", "t1", "b")).toBe(true);
  });

  it("keeps the decoded fields within the byte budget as well as the count", async () => {
    // 720 × 361 Float32 = 1.04 MB a field; a 3 MB budget keeps the newest two.
    const s = new FieldStore(24, async () => hotNode(0, 0), 3e6);
    for (const t of ["t1", "t2", "t3", "t4", "t5"]) await s.load("hs", t);
    expect(s.heldBytes).toBeLessThanOrEqual(3e6);
    expect(s.size).toBe(2);
    expect(s.has("hs", "t5") && s.has("hs", "t4")).toBe(true);
    expect(s.has("hs", "t1")).toBe(false);
    // Count limit as before, and the bytes of evicted fields are released.
    const c = new FieldStore(3, async () => hotNode(0, 0));
    for (const t of ["t1", "t2", "t3", "t4", "t5"]) await c.load("hs", t);
    expect(c.size).toBe(3);
    expect(c.heldBytes).toBe(3 * 720 * 361 * 4);
  });

  it("rejects absurd grid sizes before allocating them", () => {
    const meta = { nx: 72_000, ny: 36_001, lat0: 90, lon0: -180, dlat: 0.005, dlon: 0.005, scale: 1, offset: 0 };
    expect(() => parseFieldMeta(JSON.stringify(meta))).toThrow("too large");
    expect(() => parseFieldMeta(JSON.stringify({ ...meta, nx: 720, ny: 361, nodata: "x" }))).toThrow("nodata");
    expect(parseFieldMeta(JSON.stringify({ ...meta, nx: 720, ny: 361 })).nodata).toBe(255);
  });

  it("names the run in the URL only as a cache key", () => {
    expect(fieldUrl("hs", "2026-10-12T00:00:00Z")).toBe("/api/map/fields/hs?at=2026-10-12T00%3A00%3A00Z");
    expect(fieldUrl("hs", "2026-10-12T00:00:00Z", "noaa-gfs:2026101200")).toBe(
      "/api/map/fields/hs?at=2026-10-12T00%3A00%3A00Z&run=noaa-gfs%3A2026101200",
    );
  });
});

describe("timeline bubble over more than a week", () => {
  const steps = (days: number) => Array.from({ length: days * 8 }, (_, i) => new Date(Date.UTC(2026, 9, 11, 0) + i * 3 * 3_600_000).toISOString());

  it("adds the date once weekday names repeat", () => {
    expect(weekdaysRepeat(timelineDays(steps(7), "UTC"))).toBe(false);
    expect(weekdaysRepeat(timelineDays(steps(10), "UTC"))).toBe(true);
    expect(bubbleLabel("2026-10-18T15:00:00Z", "America/Los_Angeles")).toBe("8am Sun PDT");
    expect(bubbleLabel("2026-10-18T15:00:00Z", "America/Los_Angeles", true)).toBe("8am Sun Oct 18 PDT");
    expect(bubbleLabel("2026-11-01T17:30:00Z", "America/Los_Angeles", true)).toBe("9:30am Sun Nov 1 PST");
  });
});

describe("layer labels", () => {
  it("say 'offshore' on every sea-state scale (never to be read as surf height)", () => {
    for (const l of LAYERS) {
      if (l.id === "wind") expect(l.scaleTitle).toMatch(/^Wind speed/);
      else expect(l.scaleTitle).toMatch(/^Offshore /);
    }
  });
});
