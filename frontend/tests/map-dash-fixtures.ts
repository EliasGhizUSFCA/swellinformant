/**
 * Shared helpers of the crest-dash unit tests (map-dashes.test.ts, map-wavefronts.test.ts):
 * SYNTHETIC grids (made-up uniform or patterned seas, not model output), views, and a 2D
 * context stand-in that records every filled quad (the unit rectangle under its transform,
 * with its fill and opacity) and every stroked polyline (the reduced-motion arrowheads).
 */
import { buildLattice, drawDashes, type DashState, type DashTintSource, type WaveFamilyGrids } from "@/components/map/wavefronts";
import type { Grid } from "@/lib/map-fields";
import type { PixelView } from "@/lib/map-geo";
import { maxDashes, type WaveSystem } from "@/lib/map-waves";

/** A global 1° grid with value f(lat, lon) (NaN = no data). SYNTHETIC. */
export function globalGrid(f: (lat: number, lon: number) => number): Grid {
  const nx = 360;
  const ny = 181;
  const values = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) values[j * nx + i] = f(90 - j, -180 + i);
  return { nx, ny, lat0: 90, lon0: -180, dlat: 1, dlon: 1, values };
}
export const constant = (v: number) => globalGrid(() => v);

export function family(system: WaveSystem, hs: number | Grid, tp: number | Grid, dir: number | Grid): WaveFamilyGrids {
  const g = (x: number | Grid) => (typeof x === "number" ? constant(x) : x);
  return { system, hs: g(hs), tp: g(tp), dir: g(dir) };
}

/** Unit screen vector (x east, y down) of the travel of waves FROM `fromDeg`: (-sin F, cos F). */
export function propagation(fromDeg: number): [number, number] {
  const f = (fromDeg * Math.PI) / 180;
  return [-Math.sin(f) + 0, Math.cos(f) + 0];
}

/** A world view at zoom `z`, `width` x `height` CSS px, centred on (lat, lon). */
export function viewAt(z: number, lat: number, lon: number, width = 1440, height = 900): PixelView {
  const worldPx = 256 * 2 ** z;
  const mx = ((lon + 180) / 360) * worldPx;
  const my = (0.5 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / (2 * Math.PI)) * worldPx;
  return { originX: mx - width / 2, originY: my - height / 2, width, height, worldPx };
}

export interface Polyline {
  style: string;
  width: number;
  cap: string;
  pts: Array<[number, number]>;
}

/** A recorded linear gradient: its line (in the quad's own coordinates) and colour stops. */
export interface RecordedGradient {
  kind: "gradient";
  line: [number, number, number, number];
  stops: Array<{ at: number; color: string }>;
}

/** One filled quad: the transform (a, b, c, d, e, f) at the fill, the rectangle, its fill and opacity. */
export interface Quad {
  m: [number, number, number, number, number, number];
  rect: [number, number, number, number];
  fill: string | RecordedGradient;
  alpha: number;
}

/** The current path of the recorder: its subpaths. */
class RecordedPath {
  polys: Array<Array<[number, number]>> = [];
  moveTo(x: number, y: number) {
    this.polys.push([[x, y]]);
  }
  lineTo(x: number, y: number) {
    this.polys[this.polys.length - 1]!.push([x, y]);
  }
}

/** A 2D context stand-in that records every filled quad and every stroked polyline with its style. */
export function recorder() {
  const lines: Polyline[] = [];
  const quads: Quad[] = [];
  let path = new RecordedPath();
  let m: Quad["m"] = [1, 0, 0, 1, 0, 0];
  const ctx = {
    lineJoin: "miter",
    lineCap: "butt",
    lineWidth: 1,
    strokeStyle: "",
    fillStyle: "" as string | RecordedGradient,
    globalAlpha: 1,
    globalCompositeOperation: "source-over",
    beginPath: () => {
      path = new RecordedPath();
    },
    moveTo: (x: number, y: number) => path.moveTo(x, y),
    lineTo: (x: number, y: number) => path.lineTo(x, y),
    stroke: () => {
      for (const pts of path.polys) lines.push({ style: ctx.strokeStyle, width: ctx.lineWidth, cap: ctx.lineCap, pts });
    },
    getTransform: () => ({ a: m[0], b: m[1], c: m[2], d: m[3], e: m[4], f: m[5] }),
    setTransform: (a: number, b: number, c: number, d: number, e: number, f: number) => {
      m = [a, b, c, d, e, f];
    },
    createLinearGradient: (x0: number, y0: number, x1: number, y1: number): RecordedGradient & { addColorStop: (at: number, color: string) => void } => {
      const g = { kind: "gradient" as const, line: [x0, y0, x1, y1] as [number, number, number, number], stops: [] as RecordedGradient["stops"] };
      return Object.assign(g, { addColorStop: (at: number, color: string) => void g.stops.push({ at, color }) });
    },
    fillRect: (x: number, y: number, w: number, h: number) => {
      quads.push({ m: [...m], rect: [x, y, w, h], fill: ctx.fillStyle, alpha: ctx.globalAlpha });
    },
    clearRect: () => undefined,
    drawImage: () => undefined,
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, lines, quads };
}

/** "r, g, b" of an rgb() / rgba() colour. */
const rgbPart = (color: string) => {
  const m = /^rgba?\((\d+), (\d+), (\d+)/.exec(color);
  if (!m) throw new Error(`not an rgb() colour: ${color}`);
  return `${m[1]}, ${m[2]}, ${m[3]}`;
};
/** Opacity of an rgba() colour (1 for rgb()). */
const alphaOfColor = (color: string) => (color.startsWith("rgba(") ? Number(color.slice(color.lastIndexOf(",") + 1, -1)) : 1);
/** Colour (r, g, b) of a quad's fill (a gradient's first stop). */
export const rgbOfQuad = (q: Quad) => rgbPart(typeof q.fill === "string" ? q.fill : q.fill.stops[0]!.color);

/** A dash budget that never cuts (with `draw`, the quad budget is lifted too). */
export const UNBOUNDED = 1e6;

/**
 * One drawn dash, from its quad: a BLOB (unit rectangle y 0..1: its leading edge on the crest
 * line, its body behind it) or a BAR (unit rectangle centred on the crest line; a split bar's
 * gap is in its fill, a gradient along the crest).
 */
export interface Dash {
  kind: "blob" | "bar";
  /** Blob: the middle of its leading edge (on the crest line); bar: its centre. View px. */
  x: number;
  y: number;
  /** Unit vector along the crest and the extent along it, px. */
  ux: number;
  uy: number;
  length: number;
  /** Blob: depth behind the crest line and the unit vector it extends in (against the travel); bar: width. */
  depth: number;
  bx: number;
  by: number;
  alpha: number;
  rgb: string;
  /** Blob: its gradient (in its own coordinates); split bar: its gradient along the crest. */
  gradient: RecordedGradient | null;
  /** Bar: share of its length left out in the middle (0 for a solid bar or a blob). */
  gap: number;
}

/** The transparent middle of a split bar's fill, as a share of its length (0 without one). */
function gapOf(fill: Quad["fill"]): number {
  if (typeof fill === "string") return 0;
  const clear = fill.stops.filter((st) => alphaOfColor(st.color) === 0).map((st) => st.at);
  return clear.length && fill.line[0] !== fill.line[2] ? Math.max(...clear) - Math.min(...clear) : 0;
}

/** The dashes (blobs and bar pieces) of the recorded quads. */
export function dashesOf(quads: Quad[]): Dash[] {
  return quads.map((q) => {
    const [a, b, c, d, e, f] = q.m;
    const length = Math.hypot(a, b);
    const depth = Math.hypot(c, d);
    const blob = q.rect[1] === 0;
    return {
      kind: blob ? "blob" : "bar",
      x: e,
      y: f,
      ux: a / length,
      uy: b / length,
      length,
      depth,
      bx: depth > 0 ? c / depth : 0,
      by: depth > 0 ? d / depth : 0,
      alpha: q.alpha,
      rgb: rgbOfQuad(q),
      gradient: typeof q.fill === "string" ? null : q.fill,
      gap: blob ? 0 : gapOf(q.fill),
    };
  });
}

/** Only the blobs. */
export const blobsOf = (quads: Quad[]) => dashesOf(quads).filter((d) => d.kind === "blob");

/** Line orientation of a dash in screen degrees, 0..180 (x east, y down). */
export const orientation = (d: Pick<Dash, "ux" | "uy">) => (((Math.atan2(d.uy, d.ux) * 180) / Math.PI) % 180 + 180) % 180;

/** Draws one frame. A `budget` of UNBOUNDED lifts both budgets (dashes and quads), so no dash is cut. */
export function draw(view: PixelView, families: WaveFamilyGrids[], t = 0, still = false, budget = maxDashes(view.width), state?: DashState, land?: Uint8Array, tint?: DashTintSource | null) {
  const { ctx, lines, quads } = recorder();
  const lattice = buildLattice(view, families, land, tint);
  const stats = drawDashes(ctx, view, lattice, { t, still, budget, maxQuads: budget >= UNBOUNDED ? Infinity : undefined, state });
  return { lines, quads, stats, lattice };
}
