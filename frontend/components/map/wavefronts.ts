/**
 * Wavefronts for the map's wave layers: stylised crest lines drawn from the wave model's
 * height, period and direction of each wave system (primary swell, secondary swell, wind
 * sea, or the combined sea when the run has no partitions). Nothing here is shared with the
 * wind particles: crests are lines across the direction of travel, grouped in packets, and
 * move slowly; the physics and scales are in lib/map-waves.ts and docs/map-rendering.md.
 *
 * Per view (after a pan or zoom, or new fields) a screen LATTICE is built every LATTICE_PX
 * on a grid fixed in world pixels: for each system the propagation vector (TOWARD), crest
 * spacing, crest speed, height and a validity flag. A node is invalid without data, below
 * MIN_DRAW_HS_M, where the four grid corners of the direction disagree by more than
 * MAX_CORNER_SPREAD_DEG (a partition swap between cells: no averaged direction is invented),
 * or over land. Packets sit on a jittered grid in WORLD pixels, so they stay put while the
 * map pans and the timeline steps. Each frame, every packet places its crests along its ray
 * at the crest lattice u = n s + v t (phase speed), under a raised-cosine envelope that moves
 * at v / 2 (group speed) and fades in and out over its travel, and traces each crest along
 * the local crest-tangent field (curved where the field turns, straight where it is uniform,
 * stopped where the lattice is invalid or the crest turns more than MAX_TURN_DEG per step).
 * Strokes are batched per system, width and opacity; the canvas is cleared every frame (no
 * trails), then masked to the ocean: data validity AND NOT Natural Earth land, on both
 * basemaps. Reduced motion draws the same crests once, still, with arrowheads.
 */
import { sampleDirection, sampleGrid, type Grid } from "@/lib/map-fields";
import { mercLat, mercLon, type MercPath, type PixelView } from "@/lib/map-geo";
import {
  crestAlpha,
  crestSpacingPx,
  crestSpeedPxPerS,
  FAMILY_STYLES,
  lineWidth,
  metresPerPixel,
  MIN_DRAW_HS_M,
  type FamilyStyle,
  type WaveSystem,
} from "@/lib/map-waves";

import { tracePaths } from "./basemap-draw";

/** One wave system's fields: significant height (m), period (s), direction FROM (deg). */
export interface WaveFamilyGrids {
  system: WaveSystem;
  hs: Grid;
  tp: Grid;
  dir: Grid;
}

/** Lattice node spacing, px. */
export const LATTICE_PX = 8;
/** Largest disagreement of the four grid corners of a direction before the node is invalid. */
export const MAX_CORNER_SPREAD_DEG = 60;
/** Crest tracing step, px. */
export const TRACE_STEP_PX = 4;
/** A crest stops where it would turn more than this in one step. */
export const MAX_TURN_DEG = 25;
/** Animation frame interval (30 fps cap: crests move a few px per second). */
export const FRAME_INTERVAL_MS = 1000 / 30;
/** Vertices per frame (a 1440 x 900 desktop view / a phone). */
export const VERTEX_BUDGET = 20_000;
export const PHONE_VERTEX_BUDGET = 6_000;
/** Opacity of a trough line relative to its crests (solid systems only). */
const TROUGH_ALPHA = 0.22;
/** Envelope weight below which a crest is not drawn. */
const MIN_WEIGHT = 0.04;
/** Opacity levels and line widths the strokes are batched into. */
const ALPHA_LEVELS = [0.12, 0.3, 0.52, 0.78];
const WIDTH_LEVELS = [0.75, 1.25, 1.75, 2.25, 2.75];
/** Solid crests are drawn brighter in the middle: the outer part of each half at this opacity. */
const END_FADE = 0.45;
const END_FROM = 0.55;

const RAD = Math.PI / 180;
const COS_MAX_TURN = Math.cos(MAX_TURN_DEG * RAD);

/** Per-system lattice: values at nodes (x = (gx0 + i) * LATTICE_PX - originX, likewise y). */
export interface FamilyLattice {
  system: WaveSystem;
  style: FamilyStyle;
  /** Propagation unit vector (screen, x east, y down). */
  px: Float32Array;
  py: Float32Array;
  spacing: Float32Array;
  speed: Float32Array;
  hs: Float32Array;
  valid: Uint8Array;
  validCount: number;
  medianSpacing: number;
}

export interface Lattice {
  nx: number;
  ny: number;
  /** View px of node (0, 0). */
  x0: number;
  y0: number;
  families: FamilyLattice[];
  /** Union of all systems' valid nodes (for the ocean mask). */
  anyValid: Uint8Array;
  buildMs: number;
}

/**
 * Builds the lattice for a view. `land` (optional) flags nodes over land: an array of nx * ny
 * from `landAtNodes`, 1 = land. The view's zoom comes from its world width.
 */
export function buildLattice(view: PixelView, families: WaveFamilyGrids[], land?: Uint8Array | null): Lattice {
  const t0 = performance.now();
  const gx0 = Math.floor(view.originX / LATTICE_PX);
  const gy0 = Math.floor(view.originY / LATTICE_PX);
  const x0 = gx0 * LATTICE_PX - view.originX;
  const y0 = gy0 * LATTICE_PX - view.originY;
  const nx = Math.ceil((view.width - x0) / LATTICE_PX) + 1;
  const ny = Math.ceil((view.height - y0) / LATTICE_PX) + 1;
  const n = nx * ny;
  const zoom = Math.log2(view.worldPx / 256);
  const lons = new Float64Array(nx);
  for (let i = 0; i < nx; i++) lons[i] = mercLon((gx0 + i) * LATTICE_PX / view.worldPx);
  const lats = new Float64Array(ny);
  const mpp = new Float64Array(ny);
  for (let j = 0; j < ny; j++) {
    lats[j] = mercLat((gy0 + j) * LATTICE_PX / view.worldPx);
    mpp[j] = metresPerPixel(zoom, lats[j]!);
  }
  const anyValid = new Uint8Array(n);
  const out: FamilyLattice[] = [];
  for (const f of families) {
    const fl: FamilyLattice = {
      system: f.system,
      style: FAMILY_STYLES[f.system],
      px: new Float32Array(n),
      py: new Float32Array(n),
      spacing: new Float32Array(n),
      speed: new Float32Array(n),
      hs: new Float32Array(n),
      valid: new Uint8Array(n),
      validCount: 0,
      medianSpacing: NaN,
    };
    const spacings: number[] = [];
    for (let j = 0; j < ny; j++) {
      const lat = lats[j]!;
      if (!(lat > -85 && lat < 85)) continue;
      for (let i = 0; i < nx; i++) {
        const k = j * nx + i;
        if (land?.[k]) continue;
        const lon = lons[i]!;
        const hs = sampleGrid(f.hs, lat, lon, 0.5);
        if (!(hs >= MIN_DRAW_HS_M)) continue;
        const tp = sampleGrid(f.tp, lat, lon, 0.5);
        if (!(tp > 0)) continue;
        const from = sampleDirection(f.dir, lat, lon, 0.5, MAX_CORNER_SPREAD_DEG);
        if (!Number.isFinite(from)) continue;
        const s = crestSpacingPx(tp, mpp[j]);
        fl.px[k] = -Math.sin(from * RAD);
        fl.py[k] = Math.cos(from * RAD);
        fl.spacing[k] = s;
        fl.speed[k] = crestSpeedPxPerS(tp, s);
        fl.hs[k] = hs;
        fl.valid[k] = 1;
        fl.validCount++;
        anyValid[k] = 1;
        if ((i & 3) === 0 && (j & 3) === 0) spacings.push(s);
      }
    }
    spacings.sort((a, b) => a - b);
    fl.medianSpacing = spacings.length ? spacings[spacings.length >> 1]! : NaN;
    out.push(fl);
  }
  return { nx, ny, x0, y0, families: out, anyValid, buildMs: performance.now() - t0 };
}

/** Bilinear lattice sample at a view point; false outside the lattice or next to an invalid node. */
interface Sample {
  px: number;
  py: number;
  spacing: number;
  speed: number;
  hs: number;
}
const sample: Sample = { px: 0, py: 0, spacing: 0, speed: 0, hs: 0 };

function sampleLattice(l: Lattice, f: FamilyLattice, x: number, y: number): boolean {
  const fx = (x - l.x0) / LATTICE_PX;
  const fy = (y - l.y0) / LATTICE_PX;
  if (!(fx >= 0 && fy >= 0 && fx < l.nx - 1 && fy < l.ny - 1)) return false;
  const i = fx | 0;
  const j = fy | 0;
  const a = j * l.nx + i;
  const b = a + 1;
  const c = a + l.nx;
  const d = c + 1;
  const v = f.valid;
  if (!(v[a] && v[b] && v[c] && v[d])) return false;
  const tx = fx - i;
  const ty = fy - j;
  const wa = (1 - tx) * (1 - ty);
  const wb = tx * (1 - ty);
  const wc = (1 - tx) * ty;
  const wd = tx * ty;
  let px = f.px[a]! * wa + f.px[b]! * wb + f.px[c]! * wc + f.px[d]! * wd;
  let py = f.py[a]! * wa + f.py[b]! * wb + f.py[c]! * wc + f.py[d]! * wd;
  const m = Math.hypot(px, py);
  if (!(m > 0.5)) return false; // the nodes disagree too much to give a direction
  px /= m;
  py /= m;
  sample.px = px;
  sample.py = py;
  sample.spacing = f.spacing[a]! * wa + f.spacing[b]! * wb + f.spacing[c]! * wc + f.spacing[d]! * wd;
  sample.speed = f.speed[a]! * wa + f.speed[b]! * wb + f.speed[c]! * wc + f.speed[d]! * wd;
  sample.hs = f.hs[a]! * wa + f.hs[b]! * wb + f.hs[c]! * wc + f.hs[d]! * wd;
  return true;
}

/** Deterministic hash of integers to [0, 1). */
function hash01(a: number, b: number, c: number): number {
  let h = Math.imul(a | 0, 0x27d4eb2d) ^ Math.imul(b | 0, 0x165667b1) ^ Math.imul(c | 0, 0x9e3779b1);
  h = Math.imul(h ^ (h >>> 15), 0x85ebca6b);
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

/** A packet of one system: anchored in world pixels, with the values at its anchor. */
export interface Packet {
  /** Anchor, view px. */
  ax: number;
  ay: number;
  /** World px of the anchor (for the phase reference). */
  wx: number;
  wy: number;
  px: number;
  py: number;
  spacing: number;
  speed: number;
  /** Envelope travel per cycle, px, and the cycle phase offset (0..1). */
  travel: number;
  phase: number;
}

/**
 * Crest offsets u (px along the packet ray from its anchor) and envelope weights at time t
 * (seconds of animation). Crests lie on the lattice u = n s + v t - p . W (W the anchor in
 * world px), the same lattice for every packet with the same direction, period and zoom, so
 * crests of neighbouring packets line up in a uniform sea. The envelope (width crests x s)
 * moves at v / 2 over `travel` px and fades in and out over that cycle. `still` = centred,
 * full envelope (reduced motion).
 */
export function packetCrests(p: Packet, crests: number, t: number, still = false): Array<{ u: number; w: number }> {
  const s = p.spacing;
  const width = crests * s;
  let centre = 0;
  let fade = 1;
  if (!still) {
    const e = (((p.speed / 2) * t + p.phase * p.travel) % p.travel + p.travel) % p.travel;
    centre = e - p.travel / 2;
    const sf = Math.sin((Math.PI * e) / p.travel);
    fade = sf * sf;
  }
  const ref = p.px * p.wx + p.py * p.wy;
  const shift = still ? 0 : p.speed * t;
  // First lattice crest at or after the envelope's back edge.
  const back = centre - width / 2;
  const n0 = Math.ceil((back + ref - shift) / s);
  const out: Array<{ u: number; w: number }> = [];
  for (let n = n0; ; n++) {
    const u = n * s + shift - ref;
    if (u > centre + width / 2) break;
    const c = Math.cos((Math.PI * (u - centre)) / width);
    const w = c * c * fade;
    if (w >= MIN_WEIGHT) out.push({ u, w });
  }
  return out;
}

/** Stroke batches: one per (system, width, opacity, trough) key. */
interface Batch {
  style: FamilyStyle;
  width: number;
  alpha: number;
  /** Polylines: start offsets into `pts` (pairs), each ended by the next start. */
  starts: number[];
  pts: number[];
}

export interface FrameStats {
  crests: number;
  vertices: number;
  strokes: number;
  packets: number;
  arrows: number;
  families: WaveSystem[];
}

function hexRgb(hex: string): string {
  const v = parseInt(hex.slice(1), 16);
  return `${(v >> 16) & 255}, ${(v >> 8) & 255}, ${v & 255}`;
}

const nearestLevel = (levels: number[], x: number) => {
  let best = 0;
  for (let i = 1; i < levels.length; i++) if (Math.abs(levels[i]! - x) < Math.abs(levels[best]! - x)) best = i;
  return best;
};

/** Packet cell size for a system and view (world px; divides the world width exactly). */
function cellSize(f: FamilyLattice, view: PixelView, scale: number): number {
  const s = Number.isFinite(f.medianSpacing) ? f.medianSpacing : 20;
  // About 4 crest spacings per cell (2.5 for the short wind-sea packets), in 0.25 log2 steps
  // so that a small change of period between forecast steps keeps the same packets.
  const k = f.system === "windsea" ? 2.6 : f.system === "secondary" ? 3.6 : 4.2;
  const raw = Math.min(260, Math.max(f.style.cellPx * 0.55, k * s)) * scale;
  const q = 2 ** (Math.round(Math.log2(raw) * 4) / 4);
  const nWorld = Math.max(1, Math.round(view.worldPx / q));
  return view.worldPx / nWorld;
}

const packetCache = new WeakMap<FamilyLattice, { scale: number; packets: Packet[] }>();

/** Packets of a system in view (anchors on valid lattice points only); cached per lattice. */
export function packetsFor(l: Lattice, f: FamilyLattice, view: PixelView, scale = 1): Packet[] {
  const hit = packetCache.get(f);
  if (hit && hit.scale === scale) return hit.packets;
  const packets = computePackets(l, f, view, scale);
  packetCache.set(f, { scale, packets });
  return packets;
}

function computePackets(l: Lattice, f: FamilyLattice, view: PixelView, scale: number): Packet[] {
  const cell = cellSize(f, view, scale);
  const nWorld = Math.round(view.worldPx / cell);
  const zoomKey = Math.round(Math.log2(view.worldPx / 256) * 100);
  const sys = ["primary", "secondary", "windsea", "combined"].indexOf(f.system) + 1;
  const margin = cell;
  const ci0 = Math.floor((view.originX - margin) / cell);
  const ci1 = Math.floor((view.originX + view.width + margin) / cell);
  const cj0 = Math.max(0, Math.floor((view.originY - margin) / cell));
  const cj1 = Math.floor((view.originY + view.height + margin) / cell);
  const out: Packet[] = [];
  for (let cj = cj0; cj <= cj1; cj++) {
    for (let ci = ci0; ci <= ci1; ci++) {
      const wrapped = ((ci % nWorld) + nWorld) % nWorld;
      const wx = (ci + 0.15 + 0.7 * hash01(wrapped, cj, zoomKey * 8 + sys)) * cell;
      const wy = (cj + 0.15 + 0.7 * hash01(cj, wrapped, zoomKey * 8 + sys + 4)) * cell;
      const ax = wx - view.originX;
      const ay = wy - view.originY;
      if (!sampleLattice(l, f, ax, ay)) continue;
      out.push({
        ax,
        ay,
        wx: wx - Math.floor(ci / nWorld) * view.worldPx,
        wy,
        px: sample.px,
        py: sample.py,
        spacing: sample.spacing,
        speed: sample.speed,
        travel: cell,
        phase: hash01(wrapped * 7 + 3, cj * 13 + 5, sys),
      });
    }
  }
  return out;
}

/**
 * Traces a crest from (cx, cy) along the local crest tangent for `half` px each way, appending
 * the two halves (each starting at the centre, so dash patterns stay anchored on the crest)
 * to `emit`. Returns the number of vertices.
 */
function traceCrest(l: Lattice, f: FamilyLattice, cx: number, cy: number, half: number, emit: (pts: number[], fromIndex: number) => void): number {
  if (!sampleLattice(l, f, cx, cy)) return 0;
  const tx0 = -sample.py;
  const ty0 = sample.px;
  let vertices = 0;
  for (const sign of [1, -1]) {
    const pts = [cx, cy];
    let x = cx;
    let y = cy;
    let tx = tx0 * sign;
    let ty = ty0 * sign;
    let travelled = 0;
    while (travelled < half) {
      const h = Math.min(TRACE_STEP_PX, half - travelled);
      // Midpoint (RK2) step along the tangent field, keeping the orientation continuous.
      if (!sampleLattice(l, f, x + (tx * h) / 2, y + (ty * h) / 2)) break;
      let mx = -sample.py;
      let my = sample.px;
      if (mx * tx + my * ty < 0) {
        mx = -mx;
        my = -my;
      }
      const nx = x + mx * h;
      const ny = y + my * h;
      if (!sampleLattice(l, f, nx, ny)) break;
      let ex = -sample.py;
      let ey = sample.px;
      if (ex * tx + ey * ty < 0) {
        ex = -ex;
        ey = -ey;
      }
      if (ex * tx + ey * ty < COS_MAX_TURN) break;
      x = nx;
      y = ny;
      tx = ex;
      ty = ey;
      travelled += h;
      pts.push(x, y);
    }
    if (pts.length >= 4) {
      emit(pts, 0);
      vertices += pts.length / 2;
    }
  }
  return vertices;
}

export type WaveMode = "animated" | "static" | "off";

export interface DrawOptions {
  /** Animation time, s. */
  t: number;
  still: boolean;
  /** Vertex budget for this frame. */
  budget: number;
  /** Packet-density scale (> 1 spreads packets out). */
  cellScale?: number;
}

/**
 * Draws one frame of crests for the lattice into `ctx` (already cleared) and returns what it
 * drew. Pure apart from the canvas calls, so tests can record them.
 */
export function drawCrests(ctx: CanvasRenderingContext2D, view: PixelView, l: Lattice, o: DrawOptions): FrameStats {
  const batches = new Map<string, Batch>();
  const stats: FrameStats = { crests: 0, vertices: 0, strokes: 0, packets: 0, arrows: 0, families: [] };
  const batch = (style: FamilyStyle, sys: number, width: number, alpha: number, trough: boolean): Batch => {
    const wl = nearestLevel(WIDTH_LEVELS, width);
    const al = nearestLevel(ALPHA_LEVELS, alpha);
    const key = `${sys}|${wl}|${al}|${trough ? 1 : 0}`;
    let b = batches.get(key);
    if (!b) {
      b = { style: trough ? { ...style, dash: [] } : style, width: WIDTH_LEVELS[wl]!, alpha: ALPHA_LEVELS[al]!, starts: [], pts: [] };
      batches.set(key, b);
    }
    return b;
  };
  const arrows: number[] = [];
  let over = false;
  l.families.forEach((f, sys) => {
    if (!f.validCount) return;
    stats.families.push(f.system);
    if (over) return;
    const style = f.style;
    const solid = style.dash.length === 0;
    for (const p of packetsFor(l, f, view, o.cellScale ?? 1)) {
      if (stats.vertices >= o.budget) {
        over = true;
        break;
      }
      stats.packets++;
      const crests = packetCrests(p, style.crests, o.t, o.still);
      let lead: { x: number; y: number } | null = null;
      let leadU = -Infinity;
      for (const { u, w } of crests) {
        const cx = p.ax + p.px * u;
        const cy = p.ay + p.py * u;
        if (!sampleLattice(l, f, cx, cy)) continue;
        const hs = sample.hs;
        const alpha = crestAlpha(hs) * w;
        const width = lineWidth(hs);
        const half = style.halfLength * p.spacing;
        const main = batch(style, sys, width, alpha, false);
        const dim = solid ? batch(style, sys, width, alpha * END_FADE, false) : main;
        const v = traceCrest(l, f, cx, cy, half, (pts) => {
          if (!solid) {
            main.starts.push(main.pts.length);
            main.pts.push(...pts);
            return;
          }
          // Brighter middle, dimmer ends: split each half at END_FROM of its length.
          const split = Math.max(2, Math.round((pts.length / 2) * END_FROM)) * 2;
          main.starts.push(main.pts.length);
          main.pts.push(...pts.slice(0, Math.min(pts.length, split)));
          if (split < pts.length) {
            dim.starts.push(dim.pts.length);
            dim.pts.push(...pts.slice(split - 2));
          }
        });
        if (!v) continue;
        stats.crests++;
        stats.vertices += v;
        if (u > leadU) {
          leadU = u;
          lead = { x: cx, y: cy };
        }
        if (style.troughs && alpha * TROUGH_ALPHA >= 0.04) {
          const tb = batch(style, sys, width * 0.6, alpha * TROUGH_ALPHA, true);
          const trough = { x: cx + (p.px * p.spacing) / 2, y: cy + (p.py * p.spacing) / 2 };
          stats.vertices += traceCrest(l, f, trough.x, trough.y, half * 0.7, (pts) => {
            tb.starts.push(tb.pts.length);
            tb.pts.push(...pts);
          });
        }
      }
      // Reduced motion: an arrowhead on the leading crest shows which way the packet travels.
      if (o.still && lead && (f.system === "primary" || f.system === "combined")) {
        arrows.push(lead.x, lead.y, p.px, p.py);
        stats.arrows++;
      }
    }
  });

  ctx.lineJoin = "round";
  for (const b of batches.values()) {
    if (!b.starts.length) continue;
    ctx.beginPath();
    for (let s = 0; s < b.starts.length; s++) {
      const from = b.starts[s]!;
      const to = s + 1 < b.starts.length ? b.starts[s + 1]! : b.pts.length;
      ctx.moveTo(b.pts[from]!, b.pts[from + 1]!);
      for (let k = from + 2; k < to; k += 2) ctx.lineTo(b.pts[k]!, b.pts[k + 1]!);
    }
    ctx.setLineDash(b.style.dash);
    ctx.lineCap = b.style.look === "dotted" ? "round" : "butt";
    ctx.lineWidth = b.width;
    ctx.strokeStyle = `rgba(${hexRgb(b.style.color)}, ${b.alpha})`;
    ctx.stroke();
    stats.strokes++;
  }
  if (arrows.length) {
    ctx.setLineDash([]);
    ctx.lineCap = "round";
    ctx.beginPath();
    for (let i = 0; i < arrows.length; i += 4) {
      const [x, y, px, py] = [arrows[i]!, arrows[i + 1]!, arrows[i + 2]!, arrows[i + 3]!];
      const tipX = x + px * 7;
      const tipY = y + py * 7;
      ctx.moveTo(tipX - px * 5 - py * 4, tipY - py * 5 + px * 4);
      ctx.lineTo(tipX, tipY);
      ctx.lineTo(tipX - px * 5 + py * 4, tipY - py * 5 - px * 4);
    }
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
    ctx.stroke();
    stats.strokes++;
  }
  ctx.setLineDash([]);
  return stats;
}

/** Vertex budget for a view (desktop budget for a 1440 x 900 view, scaled by area, phones lower). */
export function vertexBudget(width: number, height: number): number {
  if (width < 640) return PHONE_VERTEX_BUDGET;
  return Math.round(Math.min(1.6, Math.max(0.5, (width * height) / (1440 * 900))) * VERTEX_BUDGET);
}

function canvas2d(width: number, height: number): CanvasRenderingContext2D | null {
  if (typeof document === "undefined") return null;
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.ceil(width));
  c.height = Math.max(1, Math.ceil(height));
  return c.getContext("2d", { willReadFrequently: true });
}

/**
 * Lattice nodes over land (1 = mostly land), rasterised from the Natural Earth rings at
 * lattice resolution. Coastal nodes stay ocean, so crests reach the coast, where the ocean
 * mask clips them exactly. Null without a 2D canvas (tests).
 */
export function landAtNodes(view: PixelView, grid: Pick<Lattice, "nx" | "ny" | "x0" | "y0">, land: MercPath[]): Uint8Array | null {
  const ctx = canvas2d(grid.nx, grid.ny);
  if (!ctx) return null;
  // View px (x0 + i * LATTICE_PX) lands on the centre of pixel i.
  ctx.setTransform(1 / LATTICE_PX, 0, 0, 1 / LATTICE_PX, 0.5 - grid.x0 / LATTICE_PX, 0.5 - grid.y0 / LATTICE_PX);
  ctx.beginPath();
  tracePaths(ctx, land, view);
  ctx.fillStyle = "#000";
  ctx.fill("evenodd");
  const d = ctx.getImageData(0, 0, grid.nx, grid.ny).data;
  const out = new Uint8Array(grid.nx * grid.ny);
  for (let k = 0; k < out.length; k++) out[k] = d[k * 4 + 3]! > 200 ? 1 : 0;
  return out;
}

/**
 * The ocean mask for a view (CSS px): where any system has data (soft-edged from the
 * lattice) AND not Natural Earth land (crisp). Applied each frame with "destination-in".
 */
export function oceanMask(view: PixelView, l: Lattice, land: MercPath[] | null): HTMLCanvasElement | null {
  const ctx = canvas2d(view.width, view.height);
  const small = canvas2d(l.nx, l.ny);
  if (!ctx || !small) return null;
  const img = small.createImageData(l.nx, l.ny);
  for (let k = 0; k < l.anyValid.length; k++) if (l.anyValid[k]) img.data[k * 4 + 3] = 255;
  small.putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = true;
  // Pixel i of the small canvas is centred on view px x0 + i * LATTICE_PX.
  ctx.drawImage(small.canvas, l.x0 - LATTICE_PX / 2, l.y0 - LATTICE_PX / 2, l.nx * LATTICE_PX, l.ny * LATTICE_PX);
  if (land) {
    ctx.globalCompositeOperation = "destination-out";
    ctx.beginPath();
    tracePaths(ctx, land, view);
    ctx.fillStyle = "#000";
    ctx.fill("evenodd");
    ctx.globalCompositeOperation = "source-over";
  }
  return ctx.canvas;
}

export interface WaveStats extends FrameStats {
  drawMs: number;
  /** 95th percentile of the frame draw time over the last 120 frames. */
  drawMsP95: number;
  latticeMs: number;
  frames: number;
}

/**
 * Runs the crest animation on one canvas: lattice and mask per view, a 30 fps time-based
 * loop (the clock only runs while animating, so crests resume where they stopped after a
 * pan), or one still frame for reduced motion.
 */
export class WaveAnimator {
  private families: WaveFamilyGrids[] = [];
  private land: MercPath[] | null = null;
  private lattice: Lattice | null = null;
  private mask: HTMLCanvasElement | null = null;
  private view: PixelView | null = null;
  private ctx: CanvasRenderingContext2D | null = null;
  private mode: WaveMode = "off";
  private running = false;
  private frame = 0;
  private last: number | null = null;
  private lastDraw = -Infinity;
  /** Animation time, ms. */
  private clock = 0;
  private times: number[] = [];
  private framesDrawn = 0;
  stats: WaveStats | null = null;
  private listener: ((stats: WaveStats) => void) | null = null;

  /** Called after each drawn frame (the canvas layer writes its data attributes); null stops. */
  setListener(listener: ((stats: WaveStats) => void) | null): void {
    this.listener = listener;
  }

  /** New fields (another forecast step or other systems): same packets, same clock. */
  setFamilies(families: WaveFamilyGrids[]): void {
    this.families = families;
    this.rebuild();
  }

  setLand(land: MercPath[] | null): void {
    this.land = land;
    this.rebuild();
  }

  private rebuild(): void {
    if (!this.view || !this.ctx || this.mode === "off") return;
    this.build(this.view);
    if (this.mode === "static") this.render(true);
  }

  private build(view: PixelView): void {
    const grid = buildLattice(view, [], null);
    const land = this.land ? landAtNodes(view, grid, this.land) : null;
    this.lattice = buildLattice(view, this.families, land);
    this.mask = oceanMask(view, this.lattice, this.land);
  }

  /** Starts on a freshly reset canvas: animated, one still frame, or nothing. */
  start(ctx: CanvasRenderingContext2D, view: PixelView, mode: WaveMode): void {
    this.stop();
    this.ctx = ctx;
    this.view = view;
    this.mode = mode;
    if (mode === "off") return;
    this.build(view);
    if (mode === "static") {
      this.render(true);
      return;
    }
    this.running = true;
    this.render(false);
    const tick = (now: number) => {
      if (!this.running) return;
      this.advance(now);
      this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }

  stop(): void {
    this.running = false;
    this.last = null;
    cancelAnimationFrame(this.frame);
  }

  get isRunning(): boolean {
    return this.running;
  }

  /** Animation time in seconds. */
  get time(): number {
    return this.clock / 1000;
  }

  /** One animation-frame callback at `now` ms: advances the clock, draws at most 30 fps. */
  advance(now: number): void {
    const dt = this.last == null ? 0 : Math.min(MAX_STEP_MS, Math.max(0, now - this.last));
    this.last = now;
    this.clock += dt;
    if (now - this.lastDraw < FRAME_INTERVAL_MS - 2) return;
    this.lastDraw = now;
    this.render(false);
  }

  private render(still: boolean): void {
    const ctx = this.ctx;
    const view = this.view;
    const l = this.lattice;
    if (!ctx || !view || !l) return;
    const t0 = performance.now();
    ctx.clearRect(0, 0, view.width, view.height);
    const frame = drawCrests(ctx, view, l, { t: still ? 0 : this.time, still, budget: vertexBudget(view.width, view.height) });
    if (this.mask) {
      ctx.globalCompositeOperation = "destination-in";
      ctx.drawImage(this.mask, 0, 0, view.width, view.height);
      ctx.globalCompositeOperation = "source-over";
    }
    const ms = performance.now() - t0;
    this.times.push(ms);
    if (this.times.length > 120) this.times.shift();
    const sorted = [...this.times].sort((a, b) => a - b);
    this.framesDrawn++;
    this.stats = { ...frame, drawMs: ms, drawMsP95: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))]!, latticeMs: l.buildMs, frames: this.framesDrawn };
    this.listener?.(this.stats);
  }
}

/** Longest clock step of one frame (after a stall the crests do not jump). */
const MAX_STEP_MS = 100;
