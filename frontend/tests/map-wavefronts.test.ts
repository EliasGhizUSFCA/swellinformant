import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { systemsIn, waveFamilies } from "@/components/map/map-canvas-layers";
import {
  buildLattice,
  drawCrests,
  LATTICE_PX,
  MAX_CORNER_SPREAD_DEG,
  packetCrests,
  vertexBudget,
  WaveAnimator,
  type Packet,
  type WaveFamilyGrids,
} from "@/components/map/wavefronts";
import { sampleDirection, type FieldGrid, type Grid } from "@/lib/map-fields";
import { mercLat, mercLon, type PixelView } from "@/lib/map-geo";
import { crestSpacingPx, crestSpeedPxPerS, TIME_SCALE, type WaveSystem } from "@/lib/map-waves";

// Every grid below is SYNTHETIC test data, not model output.

/** A global 1° grid with value f(lat, lon) (NaN = no data). */
function globalGrid(f: (lat: number, lon: number) => number): Grid {
  const nx = 360;
  const ny = 181;
  const values = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) values[j * nx + i] = f(90 - j, -180 + i);
  return { nx, ny, lat0: 90, lon0: -180, dlat: 1, dlon: 1, values };
}
const constant = (v: number) => globalGrid(() => v);

function family(system: WaveSystem, hs: number | Grid, tp: number | Grid, dir: number | Grid): WaveFamilyGrids {
  const g = (x: number | Grid) => (typeof x === "number" ? constant(x) : x);
  return { system, hs: g(hs), tp: g(tp), dir: g(dir) };
}

/** A world view at zoom `z`, `width` x `height` CSS px, centred on (lat, lon). */
function viewAt(z: number, lat: number, lon: number, width = 1440, height = 900): PixelView {
  const worldPx = 256 * 2 ** z;
  const mx = ((lon + 180) / 360) * worldPx;
  const my = (0.5 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / (2 * Math.PI)) * worldPx;
  return { originX: mx - width / 2, originY: my - height / 2, width, height, worldPx };
}

interface Polyline {
  style: string;
  width: number;
  dash: number[];
  pts: Array<[number, number]>;
}

/** A 2D context stand-in that records every stroked polyline with its style. */
function recorder() {
  const lines: Polyline[] = [];
  let path: Array<Array<[number, number]>> = [];
  let dash: number[] = [];
  const ctx = {
    lineJoin: "miter",
    lineCap: "butt",
    lineWidth: 1,
    strokeStyle: "",
    globalCompositeOperation: "source-over",
    beginPath: () => {
      path = [];
    },
    moveTo: (x: number, y: number) => {
      path.push([[x, y]]);
    },
    lineTo: (x: number, y: number) => {
      path[path.length - 1]!.push([x, y]);
    },
    setLineDash: (d: number[]) => {
      dash = [...d];
    },
    stroke: () => {
      for (const pts of path) lines.push({ style: ctx.strokeStyle, width: ctx.lineWidth, dash, pts });
    },
    clearRect: () => undefined,
    drawImage: () => undefined,
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, lines };
}

const isTrough = (l: Polyline) => l.style.startsWith("rgba(0, 8, 20");
const segments = (lines: Polyline[]) =>
  lines.flatMap((l) => l.pts.slice(1).map((p, i) => [l.pts[i]![0], l.pts[i]![1], p[0], p[1]] as const)).filter(([x0, y0, x1, y1]) => Math.hypot(x1 - x0, y1 - y0) > 0.5);

function draw(view: PixelView, families: WaveFamilyGrids[], t = 0, still = false) {
  const { ctx, lines } = recorder();
  const lattice = buildLattice(view, families);
  const stats = drawCrests(ctx, view, lattice, { t, still, budget: vertexBudget(view.width, view.height) });
  return { lines, stats, lattice };
}

// jsdom has no 2D canvas: the land and ocean masks are skipped (getContext gives null).
beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("crest geometry", () => {
  it("draws crests across the travel direction: vertical for waves from the west", () => {
    const { lines, stats } = draw(viewAt(3, -40, -30), [family("primary", 3, 12, 270)]);
    expect(stats.crests).toBeGreaterThan(20);
    const segs = segments(lines);
    expect(segs.length).toBeGreaterThan(200);
    for (const [x0, y0, x1, y1] of segs) expect(Math.abs(x1 - x0)).toBeLessThan(Math.tan(Math.PI / 180) * Math.abs(y1 - y0));
  });

  it("draws crests along (0.707, 0.707) for a swell from the SW", () => {
    const { lines } = draw(viewAt(3, -40, -30), [family("primary", 3, 12, 225)]);
    const segs = segments(lines);
    expect(segs.length).toBeGreaterThan(200);
    for (const [x0, y0, x1, y1] of segs) {
      const a = ((Math.atan2(y1 - y0, x1 - x0) * 180) / Math.PI + 360) % 180; // line orientation, 0..180
      expect(Math.abs(a - 45)).toBeLessThan(1);
    }
  });

  it("follows a field that turns: crests curve with the local direction", () => {
    // From 270 in the west of the view, turning to 230 in the east (SYNTHETIC).
    const dir = globalGrid((_lat, lon) => 270 - 40 * Math.min(1, Math.max(0, (lon + 60) / 60)));
    const { lines } = draw(viewAt(3, -40, -30), [family("primary", 3, 12, dir)]);
    const west = segments(lines).filter(([x0]) => x0 < 300);
    const east = segments(lines).filter(([x0]) => x0 > 1100);
    const meanAngle = (s: typeof west) => s.reduce((a, [x0, y0, x1, y1]) => a + (((Math.atan2(y1 - y0, x1 - x0) * 180) / Math.PI + 360) % 180), 0) / s.length;
    expect(Math.abs(meanAngle(west) - 90)).toBeLessThan(6);
    expect(Math.abs(meanAngle(east) - 50)).toBeLessThan(6);
  });

  it("draws nothing where the grid has no data (land, ice)", () => {
    // No data in 20°S..20°N, 40°W..40°E (a SYNTHETIC "continent").
    const hole = (lat: number, lon: number) => Math.abs(lat) <= 20 && Math.abs(lon) <= 40;
    const hs = globalGrid((lat, lon) => (hole(lat, lon) ? NaN : 2.5));
    const view = viewAt(3, 0, 0);
    const { lines, stats } = draw(view, [family("primary", hs, 11, 250)]);
    expect(stats.crests).toBeGreaterThan(10);
    for (const l of lines) {
      for (const [x, y] of l.pts) {
        const lat = mercLat((view.originY + y) / view.worldPx);
        const lon = mercLon((view.originX + x) / view.worldPx);
        // within half a grid cell of the edge the bilinear sample still has data
        expect(Math.abs(lat) < 19 && Math.abs(lon) < 39, `vertex at ${lat.toFixed(2)}, ${lon.toFixed(2)}`).toBe(false);
      }
    }
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
    // Only nodes exactly on a grid node (one corner) keep a direction; no crest is drawn.
    expect(swapped.families[0]!.validCount).toBeLessThan(plain.families[0]!.validCount / 1000);
    expect(draw(viewAt(3, -40, -30), [family("primary", 3, 12, checker)]).stats.crests).toBe(0);
  });
});

describe("crest motion", () => {
  const packet: Packet = { ax: 400, ay: 300, wx: 1234.5, wy: 678.25, px: 0.6, py: -0.8, spacing: 18.7, speed: crestSpeedPxPerS(10, 18.7), travel: 120, phase: 0.37, keep: 0.5 };

  it("keeps the crest lattice: positions at t and t + T / tau coincide", () => {
    const s = packet.spacing;
    const t1 = 3.1;
    const t2 = t1 + 10 / TIME_SCALE; // one crest passage later
    const mod = (u: number) => {
      const m = ((u % s) + s) % s;
      return Math.min(m, s - m);
    };
    const a = packetCrests(packet, 6, t1);
    const b = packetCrests(packet, 6, t2);
    expect(a.length).toBeGreaterThan(2);
    expect(b.length).toBeGreaterThan(2);
    for (const c of [...a, ...b]) expect(mod(c.u - a[0]!.u)).toBeCloseTo(0, 6);
    // The envelope (the packet) moved on by half as much: group speed = phase speed / 2.
    const centroid = (cs: typeof a) => cs.reduce((x, c) => x + c.u * c.w, 0) / cs.reduce((x, c) => x + c.w, 0);
    expect(centroid(b) - centroid(a)).toBeGreaterThan(0.2 * s);
    expect(centroid(b) - centroid(a)).toBeLessThan(0.8 * s);
  });

  it("moves crests at the stated speed and never jumps between close times", () => {
    const a = packetCrests(packet, 6, 2);
    const b = packetCrests(packet, 6, 2.1);
    const moved = b.map((c) => Math.min(...a.map((d) => Math.abs(c.u - d.u)))).filter((d) => d < packet.spacing / 2);
    for (const d of moved) expect(d).toBeCloseTo(packet.speed * 0.1, 6);
  });

  it("draws the same crests at the same geographic place after a 100 px pan", () => {
    const fam = [family("primary", 3, 12, 250), family("secondary", 1.2, 9, 200)];
    const a = viewAt(3, -40, -30);
    const b = { ...a, originX: a.originX + 100 };
    const world = (v: PixelView, lines: Polyline[]) =>
      new Set(
        lines.flatMap((l) =>
          l.pts.filter(([x, y]) => x > 300 + (v === b ? 0 : 100) && x < 900 + (v === b ? 0 : 100) && y > 200 && y < 700).map(([x, y]) => `${(x + v.originX).toFixed(2)},${(y + v.originY).toFixed(2)}`),
        ),
      );
    const pa = world(a, draw(a, fam, 5).lines);
    const pb = world(b, draw(b, fam, 5).lines);
    expect(pb.size).toBeGreaterThan(200);
    expect([...pb].filter((k) => !pa.has(k))).toEqual([]);
    expect([...pa].filter((k) => !pb.has(k))).toEqual([]);
  });

  it("advances by time, not frames: 60 Hz and 120 Hz give the same clock", () => {
    const rafs: Array<() => void> = [];
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => rafs.push(cb));
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    try {
      const run = (hz: number) => {
        const anim = new WaveAnimator();
        anim.setFamilies([family("primary", 3, 12, 250)]);
        anim.start(recorder().ctx, viewAt(3, -40, -30), "animated");
        for (let k = 0; k <= hz; k++) anim.advance((k * 1000) / hz);
        anim.stop();
        return anim.time;
      };
      expect(run(60)).toBeCloseTo(1, 9);
      expect(run(120)).toBeCloseTo(1, 9);
      expect(run(144)).toBeCloseTo(1, 9);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("systems, reduced motion and budget", () => {
  it("draws no secondary crests when the run has no secondary swell", () => {
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
    expect(waveFamilies("wind", fields, ["primary"])).toEqual([]); // the Wind layer draws particles, not crests
    // A secondary grid without any data gives no secondary crests at all.
    const { stats, lines } = draw(viewAt(3, -40, -30), [family("secondary", NaN, NaN, NaN), family("primary", 3, 12, 250)]);
    expect(stats.families).toEqual(["primary"]);
    expect(lines.some((l) => l.style.startsWith("rgba(255, 196, 107"))).toBe(false);
    expect(lines.some((l) => l.style.startsWith("rgba(244, 247, 251"))).toBe(true);
  });

  it("draws each system with its own dash pattern and colour", () => {
    const { lines } = draw(viewAt(3, -40, -30), [family("windsea", 1.5, 6, 280), family("secondary", 1.5, 10, 200), family("primary", 3, 14, 250)]);
    const crestLines = lines.filter((l) => !isTrough(l) && l.width < 3.2);
    const byColour = new Map<string, string>();
    for (const l of crestLines) byColour.set(l.style.slice(0, l.style.lastIndexOf(",")), l.dash.join(","));
    expect(byColour.get("rgba(244, 247, 251")).toBe(""); // primary: solid white
    expect(byColour.get("rgba(255, 196, 107")).toBe("7,5"); // secondary: dashed amber
    expect(byColour.get("rgba(127, 227, 255")).toBe("2,4"); // wind sea: dotted cyan
  });

  it("draws more ink for a higher swell", () => {
    const ink = (hs: number) => draw(viewAt(3, -40, -30), [family("primary", hs, 12, 250)]).lines.filter((l) => !isTrough(l)).reduce((a, l) => a + l.width * Number(l.style.slice(l.style.lastIndexOf(",") + 1, -1)) * (l.pts.length - 1), 0);
    expect(ink(4)).toBeGreaterThan(1.5 * ink(1));
    expect(draw(viewAt(3, -40, -30), [family("primary", 0.1, 12, 250)]).stats.crests).toBe(0); // below 0.2 m: not drawn
  });

  it("spaces the crests of a 16 s swell four times as far apart as an 8 s swell", () => {
    const spacing = (tp: number) => {
      const { lines } = draw(viewAt(3, -40, -30), [family("primary", 3, tp, 270)]);
      // Vertical crests from the west: the distinct x positions of the crest lines in one packet.
      const xs = [...new Set(lines.filter((l) => !isTrough(l)).map((l) => Math.round(l.pts[0]![0] * 10) / 10))].sort((a, b) => a - b);
      const gaps = xs.slice(1).map((x, i) => x - xs[i]!).filter((d) => d > 1);
      return Math.min(...gaps);
    };
    expect(spacing(8)).toBeCloseTo(crestSpacingPx(8), 0);
    expect(spacing(16) / spacing(8)).toBeCloseTo(4, 1);
  });

  it("reduced motion: the same still crests whatever the time, with arrowheads", () => {
    const fam = [family("primary", 3, 12, 250)];
    const a = draw(viewAt(3, -40, -30), fam, 0, true);
    const b = draw(viewAt(3, -40, -30), fam, 37, true);
    expect(a.stats.arrows).toBeGreaterThan(3);
    expect(JSON.stringify(b.lines)).toBe(JSON.stringify(a.lines));
  });

  it("stays within the vertex budget on a busy 1440 x 900 view", () => {
    const fam = [
      family("windsea", 1.8, 5, globalGrid((lat, lon) => 60 + 20 * Math.sin((lon * Math.PI) / 30))),
      family("secondary", 2, 9, globalGrid((lat) => 190 + lat)),
      family("primary", 4, 12, 250),
    ];
    for (const z of [2, 4, 6]) {
      const { stats } = draw(viewAt(z, -30, -30), fam, 4);
      expect(stats.vertices).toBeLessThanOrEqual(vertexBudget(1440, 900));
      expect(stats.families).toEqual(["windsea", "secondary", "primary"]);
    }
    expect(vertexBudget(1440, 900)).toBe(20_000);
    expect(vertexBudget(390, 844)).toBeLessThan(10_000);
  });
});

describe("lattice", () => {
  it("puts nodes on a grid fixed in world pixels", () => {
    const a = viewAt(3, -40, -30);
    const l = buildLattice(a, [family("primary", 3, 12, 250)]);
    expect(((l.x0 + a.originX) / LATTICE_PX) % 1).toBeCloseTo(0, 9);
    expect(((l.y0 + a.originY) / LATTICE_PX) % 1).toBeCloseTo(0, 9);
  });
});
