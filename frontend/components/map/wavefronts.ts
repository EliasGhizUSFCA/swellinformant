/**
 * Crest dashes for the map's wave layers: short, soft white dashes drawn from the wave
 * model's height, period and direction of each wave system (the combined sea by default, or
 * the primary swell, secondary swell and wind sea partitions). Each dash is a short piece of
 * a wave crest: it lies across the direction of travel, sits on a crest line of the stated
 * spacing and moves with the crests at the phase speed (time scale TIME_SCALE). The physics,
 * the scales and every dash constant are in lib/map-waves.ts and docs/map-rendering.md.
 *
 * Per view (after a pan or zoom, or new fields) a screen LATTICE is built every LATTICE_PX
 * on a grid fixed in world pixels: for each system the propagation vector (TOWARD), crest
 * spacing, crest speed, height and a validity flag. A node is invalid without data, below
 * MIN_DRAW_HS_M, where the four grid corners of the direction disagree by more than
 * MAX_CORNER_SPREAD_DEG (a partition swap between cells: no averaged direction is invented),
 * or over land.
 *
 * DASHES: one per cell of a grid fixed in WORLD pixels (cell DASH_CELL_PX on screen at every
 * zoom: the same density at every zoom), so dashes stay put geographically while the map pans
 * and the timeline steps. A cell's dash lives DASH_LIFE_PASSAGES crest passages (3 T / tau s)
 * and is then born again at a hashed place in its cell. At birth its centre is moved along
 * the travel vector p onto the nearest crest line of the lattice p . W = n s + v t (W in world
 * px), and it then moves along p at the crest speed v = s tau / T, so every dash of a uniform
 * sea stays on those crest lines. It fades in over the first 20 % of its life and out over the
 * last 30 % (a display choice, not a wave group). A dash is drawn only where its centre and
 * both ends sample valid lattice nodes. Behind it (for the systems whose style has one), a
 * short TRAIL (also a display choice) shows the band its crest swept over in the last
 * TRAIL_SECONDS, fading backwards in TRAIL_STEPS.
 * Strokes (trail bands with butt caps under soft halos and a core line with round caps) are
 * batched per system, opacity level, width or trail-length level and pass, within a dash and
 * a vertex budget per frame; the canvas is cleared every frame, then masked to the ocean on
 * the street basemap. Reduced motion draws one still frame at full opacity with arrowheads.
 */
import { sampleDirection, sampleGrid, type Grid } from "@/lib/map-fields";
import { mercLat, mercLon, type MercPath, type PixelView } from "@/lib/map-geo";
import {
  ARROW_EVERY,
  ARROW_PX,
  crestSpacingPx,
  crestSpeedPxPerS,
  DASH_LIFE_PASSAGES,
  DASH_MAX_PX,
  DASH_PASSES,
  dashAlpha,
  dashFade,
  dashLength,
  dashLifeSeconds,
  dashWidth,
  FAMILY_STYLES,
  maxDashes,
  metresPerPixel,
  MAX_FRAME_VERTICES,
  MIN_DRAW_HS_M,
  TRAIL_STEPS,
  trailLength,
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
/** Animation frame interval (30 fps cap: dashes move a few px per second). */
export const FRAME_INTERVAL_MS = 1000 / 30;
/** Share of the remaining dash budget each system may use, top system first. */
export const BUDGET_SHARE = 0.6;
/** Opacity levels and core widths the strokes are batched into (the halo uses the same levels x its factors). */
export const DASH_ALPHA_LEVELS = [0.06, 0.18, 0.3, 0.42, 0.54, 0.66, 0.78, 0.9];
export const DASH_WIDTH_LEVELS = [1.2, 1.75, 2.3, 2.85, 3.4];
/** Trail lengths the trail strokes are batched into, px (a trail's length is rounded to the nearest). */
export const TRAIL_LEVELS = [4, 6, 8, 10, 12, 14, 16];
/** Trails shorter than this are not drawn, px. */
const MIN_TRAIL_PX = 3.5;
/** Most vertices one dash can take (an upper bound: two pieces and one trail). */
const MAX_DASH_VERTICES = 4 + 2 * TRAIL_STEPS.length;
/** Dashes fainter than this (early or late in their life) are not drawn. */
const MIN_ALPHA = 0.04;
/** A dash whose clock is this many lives behind is placed afresh (after a long pause elsewhere). */
const MAX_CATCH_UP_LIVES = 64;
/** Hashed places tried in a cell for a new dash before the cell stays empty for a life. */
export const BIRTH_TRIES = 4;
/** Reference period for the first epoch of a cell without data at its centre, s. */
const REFERENCE_PERIOD_S = 10;

const RAD = Math.PI / 180;

/** Per-system lattice: values at nodes (x = (gx0 + i) * LATTICE_PX - originX, likewise y). */
export interface FamilyLattice {
  system: WaveSystem;
  style: FamilyStyle;
  grids: WaveFamilyGrids;
  /** Propagation unit vector (screen, x east, y down). */
  px: Float32Array;
  py: Float32Array;
  spacing: Float32Array;
  speed: Float32Array;
  hs: Float32Array;
  valid: Uint8Array;
  validCount: number;
  /** Largest crest spacing at a valid node, px: a dash travels at most 3 of them in its life. */
  maxSpacing: number;
  /** Cells visited at most (in their fixed hashed order), for a steady budget cut. */
  limit: number;
}

export interface Lattice {
  nx: number;
  ny: number;
  /** View px of node (0, 0). */
  x0: number;
  y0: number;
  /** Map zoom of the view (from its world width). */
  zoom: number;
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
  for (let i = 0; i < nx; i++) lons[i] = mercLon(((gx0 + i) * LATTICE_PX) / view.worldPx);
  const lats = new Float64Array(ny);
  const mpp = new Float64Array(ny);
  for (let j = 0; j < ny; j++) {
    lats[j] = mercLat(((gy0 + j) * LATTICE_PX) / view.worldPx);
    mpp[j] = metresPerPixel(zoom, lats[j]!);
  }
  const anyValid = new Uint8Array(n);
  const out: FamilyLattice[] = [];
  for (const f of families) {
    const fl: FamilyLattice = {
      system: f.system,
      style: FAMILY_STYLES[f.system],
      grids: f,
      px: new Float32Array(n),
      py: new Float32Array(n),
      spacing: new Float32Array(n),
      speed: new Float32Array(n),
      hs: new Float32Array(n),
      valid: new Uint8Array(n),
      validCount: 0,
      maxSpacing: 0,
      limit: Infinity,
    };
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
        if (s > fl.maxSpacing) fl.maxSpacing = s;
        anyValid[k] = 1;
      }
    }
    out.push(fl);
  }
  return { nx, ny, x0, y0, zoom, families: out, anyValid, buildMs: performance.now() - t0 };
}

/** Lattice sample at a view point (travel vector and height); false outside the lattice or next to an invalid node. */
const sample = { px: 0, py: 0, hs: 0 };

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
  const px = f.px[a]! * wa + f.px[b]! * wb + f.px[c]! * wc + f.px[d]! * wd;
  const py = f.py[a]! * wa + f.py[b]! * wb + f.py[c]! * wc + f.py[d]! * wd;
  const m = Math.hypot(px, py);
  if (!(m > 0.5)) return false; // the nodes disagree too much to give a direction
  sample.px = px / m;
  sample.py = py / m;
  sample.hs = f.hs[a]! * wa + f.hs[b]! * wb + f.hs[c]! * wc + f.hs[d]! * wd;
  return true;
}

/** Whether the four lattice nodes around a view point are all valid (false outside the lattice). */
function nodesValid(l: Lattice, f: FamilyLattice, x: number, y: number): boolean {
  const fx = (x - l.x0) / LATTICE_PX;
  const fy = (y - l.y0) / LATTICE_PX;
  if (!(fx >= 0 && fy >= 0 && fx < l.nx - 1 && fy < l.ny - 1)) return false;
  const a = (fy | 0) * l.nx + (fx | 0);
  const v = f.valid;
  return !!(v[a] && v[a + 1] && v[a + l.nx] && v[a + l.nx + 1]);
}

/**
 * Whether a trail of length `tr` behind a dash (centre cx, cy; travel px, py; half its width
 * along the crest `w`) lies on valid nodes: its back corners, and the ends and middle of the
 * centre line of every trail step.
 */
function trailValid(l: Lattice, f: FamilyLattice, cx: number, cy: number, px: number, py: number, w: number, tr: number): boolean {
  const tx = -py;
  const ty = px;
  const at = (along: number, back: number) => nodesValid(l, f, cx + tx * along - px * back, cy + ty * along - py * back);
  if (!at(-w, tr) || !at(w, tr)) return false;
  for (const step of TRAIL_STEPS) {
    const back = (tr * step.length) / 2;
    if (!at(-w, back) || !at(0, back) || !at(w, back)) return false;
  }
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

const SYSTEM_SEED: Record<WaveSystem, number> = { primary: 1, secondary: 2, windsea: 3, combined: 4 };

/** One dash cell of a system in a view: indices in the view's continuous pixel plane. */
export interface DashCell {
  /** Cell column (not wrapped: the view's continuous pixel plane) and row. */
  ci: number;
  cj: number;
  /** Column wrapped to one world copy (the hash seed: the same place in every copy). */
  wrapped: number;
  /** State key (column and row). */
  key: number;
  /** Phase offset of the cell's life cycle, 0..1. */
  phase: number;
  /** Life (s) used to place the cell's first dash: 3 T / tau with T at the cell centre (else 10 s). */
  lifeRef: number;
  /** The cell's dash in `owner` (a DashState store), held here so a frame needs no lookups. */
  dash?: CellDash;
  owner?: Map<number, CellDash>;
}

export interface DashGrid {
  /** Cell size, px (divides the world width exactly). */
  cell: number;
  /** Cells covering the view and a margin, in their fixed hashed order (the order a frame over budget visits them). */
  cells: DashCell[];
}

/** Cell size for a system and view: DASH_CELL_PX, adjusted so that a whole number of cells spans the world. */
export function dashCellSize(style: FamilyStyle, view: PixelView): number {
  return view.worldPx / Math.max(1, Math.round(view.worldPx / style.cellPx));
}

const gridCache = new WeakMap<FamilyLattice, DashGrid>();

/**
 * The dash cells of a system in view, plus a margin of DASH_LIFE_PASSAGES crest spacings
 * (how far a dash travels in its life), so dashes born just outside the view drift into it.
 * Cached per lattice.
 */
export function dashGrid(l: Lattice, f: FamilyLattice, view: PixelView): DashGrid {
  const hit = gridCache.get(f);
  if (hit) return hit;
  const cell = dashCellSize(f.style, view);
  const nWorld = Math.round(view.worldPx / cell);
  const seed = SYSTEM_SEED[f.system];
  const margin = DASH_LIFE_PASSAGES * Math.max(f.maxSpacing, 8) + DASH_MAX_PX;
  const ci0 = Math.floor((view.originX - margin) / cell);
  const ci1 = Math.floor((view.originX + view.width + margin) / cell);
  const cj0 = Math.max(0, Math.floor((view.originY - margin) / cell));
  const cj1 = Math.min(nWorld - 1, Math.floor((view.originY + view.height + margin) / cell));
  const cells: Array<DashCell & { keep: number }> = [];
  for (let cj = cj0; cj <= cj1; cj++) {
    const lat = mercLat(((cj + 0.5) * cell) / view.worldPx);
    for (let ci = ci0; ci <= ci1; ci++) {
      const wrapped = ((ci % nWorld) + nWorld) % nWorld;
      const tp = Math.abs(lat) < 85 ? sampleGrid(f.grids.tp, lat, mercLon(((wrapped + 0.5) * cell) / view.worldPx), 0.5) : NaN;
      cells.push({
        ci,
        cj,
        wrapped,
        key: (ci + 1048576) * 2097152 + cj,
        phase: hash01(wrapped * 8 + seed, cj, 7),
        lifeRef: dashLifeSeconds(tp > 0 ? tp : REFERENCE_PERIOD_S),
        keep: hash01(wrapped * 8 + seed, cj, 11),
      });
    }
  }
  // In a fixed hashed order: a frame over budget leaves out an even, random share of the
  // cells, never a region, and the same one from frame to frame.
  cells.sort((a, b) => a.keep - b.keep);
  const grid = { cell, cells };
  gridCache.set(f, grid);
  return grid;
}

/** The dash of one cell during one life: born at `tb` (s) on the crest lattice, moving along p at v. */
interface CellDash {
  /** Life number (epoch) and its start, s of animation, and length, s. */
  k: number;
  tb: number;
  life: number;
  /** Whether the birth place had data (else the cell is empty for this life). */
  ok: boolean;
  /** Centre at birth, world px (continuous plane), on the crest lattice. */
  wx: number;
  wy: number;
  /** Travel unit vector, crest speed (px/s) and crest spacing (px) at birth. */
  px: number;
  py: number;
  v: number;
  s: number;
  /** Frame in which the cell was last visited. */
  seen: number;
}

/**
 * The dashes of every cell, kept between frames: a new forecast step (new fields) keeps every
 * live dash, which ends its life with the values it was born with, and the next dash of the
 * cell is born from the new fields. Without earlier frames a cell's dash is placed from the
 * clock alone: life number floor(t / life + phase) of the cell, so the result does not depend
 * on the frame rate or on when the map was opened.
 */
export class DashState {
  private stores = new Map<string, { map: Map<number, CellDash>; used: number }>();
  frame = 0;

  store(system: WaveSystem, zoom: number, cell: number): Map<number, CellDash> {
    const key = `${system}|${Math.round(zoom * 100)}|${Math.round(cell * 1000)}`;
    let s = this.stores.get(key);
    if (!s) this.stores.set(key, (s = { map: new Map(), used: this.frame }));
    s.used = this.frame;
    return s.map;
  }

  /** Forgets cells not visited for a while and stores of other zooms. */
  prune(): void {
    for (const [key, s] of this.stores) {
      if (this.frame - s.used > 120) {
        this.stores.delete(key);
        continue;
      }
      if (s.map.size > 20_000) s.map.forEach((d, k, m) => this.frame - d.seen > 300 && m.delete(k));
    }
  }
}

/** World px to (lat, lon) of one world copy. */
function placeOf(wx: number, wy: number, worldPx: number): [number, number] {
  const u = ((wx / worldPx) % 1 + 1) % 1;
  return [mercLat(wy / worldPx), mercLon(u)];
}

/**
 * Gives the cell's dash of life `d.k` (starting at `d.tb`) its place and values: the first of
 * BIRTH_TRIES hashed places in the cell where the system has data (a cell that is partly
 * land, sea ice or a partition swap still gets its dash), moved along the travel vector there
 * onto the nearest crest line of p . W = n s + v tb. Its life is 3 T / tau with the period
 * there. Without data at any of the places the cell stays empty for one reference life.
 */
function bear(d: CellDash, f: FamilyLattice, c: DashCell, cell: number, view: PixelView, zoom: number): void {
  d.ok = false;
  d.life = c.lifeRef;
  const seed = SYSTEM_SEED[f.system];
  const g = f.grids;
  for (let k = 0; k < BIRTH_TRIES; k++) {
    const bx = (c.ci + hash01(c.wrapped * 8 + seed, c.cj, d.k * 8 + k * 2 + 1)) * cell;
    const by = (c.cj + hash01(c.cj * 8 + seed, c.wrapped, d.k * 8 + k * 2 + 2)) * cell;
    const [lat, lon] = placeOf(bx, by, view.worldPx);
    if (!(Math.abs(lat) < 85)) continue;
    const hs = sampleGrid(g.hs, lat, lon, 0.5);
    if (!(hs >= MIN_DRAW_HS_M)) continue;
    const tp = sampleGrid(g.tp, lat, lon, 0.5);
    if (!(tp > 0)) continue;
    const from = sampleDirection(g.dir, lat, lon, 0.5, MAX_CORNER_SPREAD_DEG);
    if (!Number.isFinite(from)) continue;
    const s = crestSpacingPx(tp, metresPerPixel(zoom, lat));
    const v = crestSpeedPxPerS(tp, s);
    const px = -Math.sin(from * RAD);
    const py = Math.cos(from * RAD);
    // Onto the crest lattice: the phase p . B - v tb, taken to the nearest multiple of s.
    const phase = px * bx + py * by - v * d.tb;
    const off = phase - s * Math.round(phase / s);
    d.wx = bx - px * off;
    d.wy = by - py * off;
    d.px = px;
    d.py = py;
    d.v = v;
    d.s = s;
    d.life = dashLifeSeconds(tp);
    d.ok = true;
    return;
  }
}

/** Places a cell's dash from the clock alone (no earlier frames): its life number at time t. */
function freshDash(f: FamilyLattice, c: DashCell, cell: number, view: PixelView, zoom: number, t: number, frame: number): CellDash {
  const k = Math.floor(t / c.lifeRef + c.phase);
  const d: CellDash = { k, tb: (k - c.phase) * c.lifeRef, life: c.lifeRef, ok: false, wx: 0, wy: 0, px: 0, py: 0, v: 0, s: 0, seen: frame };
  bear(d, f, c, cell, view, zoom);
  return d;
}

/** The cell's dash at time t: the stored one, moved on through its lives as the clock passed them. */
function dashAt(store: Map<number, CellDash>, f: FamilyLattice, c: DashCell, cell: number, view: PixelView, zoom: number, t: number, frame: number): CellDash {
  let d = c.owner === store ? c.dash : store.get(c.key);
  if (!d || t < d.tb || t - d.tb > MAX_CATCH_UP_LIVES * d.life) {
    d = freshDash(f, c, cell, view, zoom, t, frame);
    store.set(c.key, d);
  }
  c.dash = d;
  c.owner = store;
  while (t >= d.tb + d.life) {
    d.tb += d.life;
    d.k++;
    bear(d, f, c, cell, view, zoom);
  }
  d.seen = frame;
  return d;
}

/**
 * Stroke batches: per system index, width level and opacity level. Each batch's path is built
 * once and stroked once per pass of DASH_PASSES (the halo, then the core).
 */
interface Batch {
  /** Segments (x0, y0, x1, y1), `n` numbers used (the array is reused and only grows). */
  segs: Float64Array;
  n: number;
  frame: number;
  style: FamilyStyle;
}
const NW = DASH_WIDTH_LEVELS.length;
const NA = DASH_ALPHA_LEVELS.length;
const NT = TRAIL_LEVELS.length;
const NS = TRAIL_STEPS.length;
const batchPool: Array<Batch | undefined> = [];
/** Trail batches: per system index, trail length level and opacity level; `segs` holds NS segments per piece. */
const trailPool: Array<Batch | undefined> = [];
let poolFrame = 0;

/** Appends numbers to a batch of this frame (reset on first use in a frame, grown as needed). */
function batchOf(pool: Array<Batch | undefined>, key: number, frame: number, style: FamilyStyle, need: number): Batch {
  let b = pool[key];
  if (!b) pool[key] = b = { segs: new Float64Array(256), n: 0, frame: 0, style };
  if (b.frame !== frame) {
    b.frame = frame;
    b.n = 0;
    b.style = style; // the system at this index can differ from the last frame's
  }
  if (b.n + need > b.segs.length) {
    const grown = new Float64Array(Math.max(b.segs.length * 2, b.n + need));
    grown.set(b.segs);
    b.segs = grown;
  }
  return b;
}

/** A path of the segments (x0, y0, x1, y1) of `b` at indices i, i + stride, ... (from `first`). */
function pathOf(b: Batch, first: number, stride: number): Path2D {
  const path = new Path2D();
  for (let i = first; i < b.n; i += stride) {
    path.moveTo(b.segs[i]!, b.segs[i + 1]!);
    path.lineTo(b.segs[i + 2]!, b.segs[i + 3]!);
  }
  return path;
}

/** The nearest of evenly spaced levels (index). */
const nearestLevel = (levels: readonly number[], x: number) => {
  const n = levels.length - 1;
  const i = Math.round(((x - levels[0]!) / (levels[n]! - levels[0]!)) * n);
  return i < 0 ? 0 : i > n ? n : i;
};

function hexRgb(hex: string): string {
  const v = parseInt(hex.slice(1), 16);
  return `${(v >> 16) & 255}, ${(v >> 8) & 255}, ${v & 255}`;
}

export interface FrameStats {
  /** Dashes drawn (data-crests). */
  crests: number;
  vertices: number;
  strokes: number;
  /** Dash cells visited. */
  cells: number;
  arrows: number;
  /** Dashes drawn with a motion trail (data-dash-trails). */
  trails: number;
  families: WaveSystem[];
}

export type WaveMode = "animated" | "static" | "off";

export interface DrawOptions {
  /** Animation time, s. */
  t: number;
  still: boolean;
  /** Most dashes this frame (maxDashes). */
  budget: number;
  /** Most vertices this frame (default MAX_FRAME_VERTICES). */
  maxVertices?: number;
  /** The dashes kept between frames (a fresh state places every dash from the clock alone). */
  state?: DashState;
}

/**
 * Draws one frame of crest dashes for the lattice into `ctx` (already cleared) and returns
 * what it drew. Pure apart from the canvas calls and `o.state`, so tests can record them.
 */
export function drawDashes(ctx: CanvasRenderingContext2D, view: PixelView, l: Lattice, o: DrawOptions): FrameStats {
  const frame = ++poolFrame;
  const state = o.still || !o.state ? new DashState() : o.state;
  state.frame++;
  const t = o.still ? 0 : o.t;
  const maxVertices = o.maxVertices ?? MAX_FRAME_VERTICES;
  const stats: FrameStats = { crests: 0, vertices: 0, strokes: 0, cells: 0, arrows: 0, trails: 0, families: [] };
  let maxKey = -1;
  let maxTrailKey = -1;
  const push = (sys: number, wl: number, al: number, style: FamilyStyle, x0: number, y0: number, x1: number, y1: number) => {
    const key = (sys * NW + wl) * NA + al;
    const b = batchOf(batchPool, key, frame, style, 4);
    b.segs[b.n++] = x0;
    b.segs[b.n++] = y0;
    b.segs[b.n++] = x1;
    b.segs[b.n++] = y1;
    if (key > maxKey) maxKey = key;
    stats.vertices += 2;
  };
  // The trail of one dash piece from (x0, y0) to (x1, y1) (along the crest): for each step a
  // segment parallel to the piece, set back against the travel (bx, by) by half the step's
  // length and stroked that long with butt caps, so the step covers the band from the crest
  // line back to `length` x the trail.
  const pushTrail = (sys: number, tl: number, al: number, style: FamilyStyle, x0: number, y0: number, x1: number, y1: number, bx: number, by: number, cap: number) => {
    const key = (sys * NT + tl) * NA + al;
    const b = batchOf(trailPool, key, frame, style, 4 * NS);
    const tr = TRAIL_LEVELS[tl]!;
    // As wide as the piece and its round caps: `cap` px beyond each end along the crest
    // tangent (-by, bx), the direction the pieces run in.
    const ex = -by * cap;
    const ey = bx * cap;
    for (const step of TRAIL_STEPS) {
      const back = (tr * step.length) / 2;
      b.segs[b.n++] = x0 - ex - bx * back;
      b.segs[b.n++] = y0 - ey - by * back;
      b.segs[b.n++] = x1 + ex - bx * back;
      b.segs[b.n++] = y1 + ey - by * back;
    }
    if (key > maxTrailKey) maxTrailKey = key;
    stats.vertices += 2 * NS;
  };
  const arrows: number[] = [];
  // The budget goes to the top system first (the primary swell is drawn last, on top, but is
  // never the one left out): each system may use BUDGET_SHARE of what is left, the last one
  // all of it.
  const order = l.families.map((f, sys) => ({ f, sys })).filter(({ f }) => f.validCount > 0);
  stats.families = order.map(({ f }) => f.system);
  const ox = view.originX;
  const oy = view.originY;
  order.reverse().forEach(({ f, sys }, rank) => {
    const style = f.style;
    const { cell, cells } = dashGrid(l, f, view);
    const store = state.store(f.system, l.zoom, cell);
    const last = rank === order.length - 1;
    const left = o.budget - stats.crests;
    const cap = stats.crests + (last ? left : Math.floor(left * BUDGET_SHARE));
    const before = stats.crests;
    // The vertex budget is shared the same way.
    const vLeft = maxVertices - stats.vertices;
    const vCap = stats.vertices + (last ? vLeft : Math.floor(vLeft * BUDGET_SHARE));
    const vBefore = stats.vertices;
    const arrowsOn = o.still && (f.system === "primary" || f.system === "combined");
    const reach = DASH_MAX_PX;
    let visited = 0;
    let hit = false;
    let drawnHere = 0;
    for (const c of cells) {
      if (visited >= f.limit) break;
      if (stats.crests >= cap || stats.vertices + MAX_DASH_VERTICES > vCap) {
        hit = true;
        break;
      }
      visited++;
      const d = dashAt(store, f, c, cell, view, l.zoom, t, state.frame);
      if (!d.ok) continue;
      const age = (t - d.tb) / d.life;
      const fade = o.still ? 1 : dashFade(age);
      const travel = d.v * (t - d.tb);
      const cx = d.wx + d.px * travel - ox;
      const cy = d.wy + d.py * travel - oy;
      if (cx < -reach || cy < -reach || cx > view.width + reach || cy > view.height + reach) continue;
      // Orientation and height where the dash is now; centre and both ends on valid nodes.
      if (!sampleLattice(l, f, cx, cy)) continue;
      const hs = sample.hs;
      const alpha = dashAlpha(hs) * fade;
      if (alpha < MIN_ALPHA) continue;
      const px = sample.px;
      const py = sample.py;
      const tx = -py; // crest tangent: a quarter turn clockwise of the travel vector on screen
      const ty = px;
      const half = dashLength(hs, f.system) / 2;
      if (!nodesValid(l, f, cx - tx * half, cy - ty * half) || !nodesValid(l, f, cx + tx * half, cy + ty * half)) continue;
      const width = dashWidth(hs, f.system);
      const wl = nearestLevel(DASH_WIDTH_LEVELS, width);
      const al = nearestLevel(DASH_ALPHA_LEVELS, alpha);
      // The trail: where the dash was over the last TRAIL_SECONDS (no further back than its
      // birth while animating), from MIN_TRAIL_PX, and only where all of it is on valid nodes
      // (trailValid; else the dash is drawn without one).
      const trail = !style.trail ? 0 : o.still ? trailLength(d.v, d.s) : Math.min(trailLength(d.v, d.s), d.v * (t - d.tb));
      const capPx = DASH_WIDTH_LEVELS[wl]! / 2;
      let tl = -1;
      if (trail >= MIN_TRAIL_PX) {
        const level = nearestLevel(TRAIL_LEVELS, trail);
        if (trailValid(l, f, cx, cy, px, py, half + capPx, TRAIL_LEVELS[level]!)) tl = level;
      }
      if (style.gap > 0) {
        // Split dash: the core shows a gap of `gap` x the length between its round caps.
        const g = (style.gap * half * 2) / 2 + DASH_WIDTH_LEVELS[wl]! / 2;
        push(sys, wl, al, style, cx - tx * half, cy - ty * half, cx - tx * g, cy - ty * g);
        push(sys, wl, al, style, cx + tx * g, cy + ty * g, cx + tx * half, cy + ty * half);
      } else {
        push(sys, wl, al, style, cx - tx * half, cy - ty * half, cx + tx * half, cy + ty * half);
      }
      // One trail behind the whole dash (a split dash shows its gap in the bright pieces in front).
      if (tl >= 0) {
        pushTrail(sys, tl, al, style, cx - tx * half, cy - ty * half, cx + tx * half, cy + ty * half, px, py, capPx);
        stats.trails++;
      }
      stats.crests++;
      drawnHere++;
      // Reduced motion: an arrowhead ahead of every ARROW_EVERY-th dash shows which way the waves travel.
      if (arrowsOn && drawnHere % ARROW_EVERY === 1) {
        const lead = DASH_WIDTH_LEVELS[wl]! * DASH_PASSES[0]!.width * 0.5 + 1;
        arrows.push(cx + px * (lead + ARROW_PX), cy + py * (lead + ARROW_PX), px, py);
        stats.arrows++;
      }
    }
    stats.cells += visited;
    // The cut only shrinks when a budget (dashes or vertices) is hit and grows back one cell a
    // frame while well under both, so a cell at the margin does not flicker.
    if (hit) f.limit = visited;
    else if (f.limit < cells.length && stats.crests - before < 0.75 * (cap - before) && stats.vertices - vBefore < 0.75 * (vCap - vBefore)) f.limit++;
  });

  ctx.lineJoin = "round";
  ctx.setLineDash([]);
  // Per system in drawing order (primary last, on top): its trails (each step of every trail
  // batch, butt caps), then every dash batch's halo and its core (round caps, DASH_PASSES).
  // Each batch's path is built once and stroked once per pass.
  const perSystem = NW * NA;
  const perSystemTrail = NT * NA;
  for (let sys = 0; sys * perSystem <= maxKey; sys++) {
    ctx.lineCap = "butt";
    for (let key = sys * perSystemTrail; key < (sys + 1) * perSystemTrail && key <= maxTrailKey; key++) {
      const b = trailPool[key];
      if (!b || b.frame !== frame || !b.n) continue;
      const tr = TRAIL_LEVELS[Math.floor(key / NA) % NT]!;
      const a = DASH_ALPHA_LEVELS[key % NA]!;
      const rgb = hexRgb(b.style.color);
      TRAIL_STEPS.forEach((step, k) => {
        ctx.lineWidth = tr * step.length;
        ctx.strokeStyle = `rgba(${rgb}, ${Math.round(a * step.alpha * 1000) / 1000})`;
        ctx.stroke(pathOf(b, 4 * k, 4 * NS));
        stats.strokes++;
      });
    }
    ctx.lineCap = "round";
    const paths: Array<{ path: Path2D; wl: number; al: number; rgb: string }> = [];
    for (let key = sys * perSystem; key < (sys + 1) * perSystem && key <= maxKey; key++) {
      const b = batchPool[key];
      if (!b || b.frame !== frame || !b.n) continue;
      paths.push({ path: pathOf(b, 0, 4), wl: Math.floor(key / NA) % NW, al: key % NA, rgb: hexRgb(b.style.color) });
    }
    for (const pass of DASH_PASSES) {
      for (const { path, wl, al, rgb } of paths) {
        ctx.lineWidth = DASH_WIDTH_LEVELS[wl]! * pass.width;
        ctx.strokeStyle = `rgba(${rgb}, ${Math.round(DASH_ALPHA_LEVELS[al]! * pass.alpha * 1000) / 1000})`;
        ctx.stroke(path);
        stats.strokes++;
      }
    }
  }
  if (arrows.length) {
    ctx.lineCap = "round";
    ctx.beginPath();
    for (let i = 0; i < arrows.length; i += 4) {
      const [tipX, tipY, px, py] = [arrows[i]!, arrows[i + 1]!, arrows[i + 2]!, arrows[i + 3]!];
      const bx = tipX - px * ARROW_PX;
      const by = tipY - py * ARROW_PX;
      const w = ARROW_PX * 0.7;
      ctx.moveTo(bx - py * w, by + px * w);
      ctx.lineTo(tipX, tipY);
      ctx.lineTo(bx + py * w, by - px * w);
    }
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
    ctx.stroke();
    stats.strokes++;
  }
  if (state === o.state) state.prune();
  return stats;
}

/** Most dashes per frame for a view (MAX_DASHES on desktops, PHONE_MAX_DASHES under 640 px wide). */
export function dashBudget(width: number): number {
  return maxDashes(width);
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
 * lattice resolution. Coastal nodes stay ocean, so dashes reach the coast, where the opaque
 * land (dark basemap) or the ocean mask (streets) clips them exactly. Null without a 2D
 * canvas (tests).
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
 * Runs the dash animation on one canvas: lattice and mask per view, a 30 fps time-based loop
 * (the clock only runs while animating, so dashes resume where they stopped after a pan), or
 * one still frame for reduced motion.
 */
export class WaveAnimator {
  private families: WaveFamilyGrids[] = [];
  private land: MercPath[] | null = null;
  private lattice: Lattice | null = null;
  private mask: HTMLCanvasElement | null = null;
  private maskOn = true;
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
  private readonly dashes = new DashState();
  stats: WaveStats | null = null;
  private listener: ((stats: WaveStats) => void) | null = null;

  /** Called after each drawn frame (the canvas layer writes its data attributes); null stops. */
  setListener(listener: ((stats: WaveStats) => void) | null): void {
    this.listener = listener;
  }

  /** New fields (another forecast step or other systems): the same dashes live on, the same clock. */
  setFamilies(families: WaveFamilyGrids[]): void {
    this.families = families;
    this.rebuild();
  }

  setLand(land: MercPath[] | null): void {
    this.land = land;
    this.rebuild();
  }

  /**
   * Whether every frame is composited with the ocean mask. Needed over street tiles; on the
   * dark basemap the opaque land drawn above the dashes already clips them at the coast (and
   * land lattice nodes keep them off land in the canvas), so the full-canvas composite, about
   * 4 ms a frame with software rendering, is skipped there.
   */
  setOceanMask(on: boolean): void {
    if (on === this.maskOn) return;
    this.maskOn = on;
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
    this.mask = this.maskOn ? oceanMask(view, this.lattice, this.land) : null;
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
    const frame = drawDashes(ctx, view, l, { t: still ? 0 : this.time, still, budget: dashBudget(view.width), state: this.dashes });
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

/** Longest clock step of one frame (after a stall the dashes do not jump). */
const MAX_STEP_MS = 100;
