/**
 * Crest dashes for the map's wave layers: short, soft blobs drawn from the wave model's
 * height, period and direction of each wave system (the combined sea by default, or the
 * primary swell, secondary swell and wind sea partitions). Each dash is a short piece of a
 * wave crest: it lies across the direction of travel, sits on a crest line of the stated
 * spacing and moves with the crests at the phase speed (time scale TIME_SCALE). The physics,
 * the scales and every dash constant are in lib/map-waves.ts and docs/map-rendering.md.
 *
 * Per view (after a pan or zoom, or new fields) a screen LATTICE is built every LATTICE_PX
 * on a grid fixed in world pixels: for each system the propagation vector (TOWARD), height
 * and a validity flag (and the largest crest spacing), plus the tint level of the colour field
 * under each node. A node is invalid without data, below MIN_DRAW_HS_M, where the four grid
 * corners of the direction disagree by more than MAX_CORNER_SPREAD_DEG (a partition swap
 * between cells: no averaged direction is invented), or over land.
 *
 * DASHES: one per cell of a grid fixed in WORLD pixels (cell DASH_CELL_PX on screen at every
 * zoom: the same density at every zoom), so dashes stay put geographically while the map pans
 * and the timeline steps. A cell's dash lives DASH_LIFE_PASSAGES crest passages (3 T / tau s)
 * and is then born again at a hashed place in its cell. At birth its centre is moved along
 * the travel vector p onto the nearest crest line of the lattice p . W = n s + v t (W in world
 * px), and it then moves along p at the crest speed v = s tau / T, so every dash of a uniform
 * sea stays on those crest lines. It fades in over the first 20 % of its life and out over the
 * last 30 % (a display choice, not a wave group). A dash is drawn only where its centre and
 * its four corners sample valid lattice nodes. A blob's bright leading edge lies on the crest
 * line and its body is the band the crest swept over in the last SMEAR_SECONDS (also a
 * display choice), fading towards the back (BLOB_PROFILE).
 *
 * Every blob is ONE filled rectangle under its own transform (fillRect, no path): the canvas
 * transform maps the unit rectangle onto the blob, and a linear gradient in the rectangle's
 * own coordinates gives the falloff, so one gradient per tint serves every blob of that tint.
 * Blobs are batched per system and tint level within a dash and a quad budget per frame; the
 * canvas is cleared every frame, then masked to the ocean on the street basemap. Reduced
 * motion draws one still frame at full opacity with arrowheads.
 */
import { sampleDirection, sampleGrid, type Grid } from "@/lib/map-fields";
import { mercLat, mercLon, type MercPath, type PixelView } from "@/lib/map-geo";
import {
  ARROW_EVERY,
  ARROW_PX,
  BLOB_HEAD_PX,
  BLOB_PROFILE,
  blobDepth,
  crestSpacingPx,
  crestSpeedPxPerS,
  DASH_ALPHA_MAX,
  DASH_LIFE_PASSAGES,
  DASH_MAX_PX,
  dashAlpha,
  dashFade,
  dashLength,
  dashLifeSeconds,
  dashTint,
  dashWidth,
  DRAW_TARGET_MS,
  FAMILY_STYLES,
  maxDashes,
  metresPerPixel,
  MAX_FRAME_QUADS,
  MIN_DRAW_HS_M,
  MIN_FRAME_QUADS,
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
/** 1 / LATTICE_PX (exact: a power of two), for view px to lattice units. */
const INV_LATTICE_PX = 1 / LATTICE_PX;
/** Largest disagreement of the four grid corners of a direction before the node is invalid. */
export const MAX_CORNER_SPREAD_DEG = 60;
/** Animation frame interval (30 fps cap: dashes move a few px per second). */
export const FRAME_INTERVAL_MS = 1000 / 30;
/** Share of the remaining dash budget each system may use, top system first. */
export const BUDGET_SHARE = 0.6;
/** Tint levels: the colour field's scale (0..1) in this many steps, one gradient per level and system. */
export const TINT_LEVELS = 32;
/** Lattice tint value of a node without a colour under it. */
const NO_TINT = 255;
/** Quads one dash takes (a blob, or a whole split bar: its gap is in its fill). */
const MAX_DASH_QUADS = 1;
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
  hs: Float32Array;
  valid: Uint8Array;
  /** 1 where all four nodes of the lattice cell (i, j)..(i + 1, j + 1) are valid (index of node (i, j)). */
  cellValid: Uint8Array;
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
  /** Tint level (0..TINT_LEVELS - 1) of the colour field at each node, NO_TINT without one; null without a colour field. */
  tint: Uint8Array | null;
  /** RGB of the colour field at the middle of each tint level (null without a colour field). */
  tintRgb: Array<[number, number, number]> | null;
  buildMs: number;
}

/**
 * The colour field under the dashes (the colour layer), for their tint: its scale position
 * (0..1, NaN without a value) at a place and its RGBA lookup table (lib/map-colors buildLut).
 */
export interface DashTintSource {
  position: (lat: number, lon: number) => number;
  lut: Uint8ClampedArray;
}

/**
 * Builds the lattice for a view. `land` (optional) flags nodes over land: an array of nx * ny
 * from `landAtNodes`, 1 = land. `tint` (optional) is the colour field the dashes are tinted
 * from. The view's zoom comes from its world width.
 */
export function buildLattice(view: PixelView, families: WaveFamilyGrids[], land?: Uint8Array | null, tint?: DashTintSource | null): Lattice {
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
      hs: new Float32Array(n),
      valid: new Uint8Array(n),
      cellValid: new Uint8Array(n),
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
        fl.hs[k] = hs;
        fl.valid[k] = 1;
        fl.validCount++;
        if (s > fl.maxSpacing) fl.maxSpacing = s;
        anyValid[k] = 1;
      }
    }
    const v = fl.valid;
    for (let j = 0; j < ny - 1; j++) {
      for (let i = 0; i < nx - 1; i++) {
        const k = j * nx + i;
        if (v[k] && v[k + 1] && v[k + nx] && v[k + nx + 1]) fl.cellValid[k] = 1;
      }
    }
    out.push(fl);
  }
  let tintLevels: Uint8Array | null = null;
  let tintRgb: Array<[number, number, number]> | null = null;
  if (tint && out.length) {
    tintLevels = new Uint8Array(n).fill(NO_TINT);
    for (let j = 0; j < ny; j++) {
      const lat = lats[j]!;
      if (!(lat > -85 && lat < 85)) continue;
      for (let i = 0; i < nx; i++) {
        const k = j * nx + i;
        if (!anyValid[k]) continue;
        const pos = tint.position(lat, lons[i]!);
        if (pos === pos) tintLevels[k] = Math.min(TINT_LEVELS - 1, Math.max(0, Math.floor(pos * TINT_LEVELS)));
      }
    }
    const size = tint.lut.length / 4;
    tintRgb = Array.from({ length: TINT_LEVELS }, (_, level) => {
      const e = Math.round(((level + 0.5) / TINT_LEVELS) * (size - 1)) * 4;
      return [tint.lut[e]!, tint.lut[e + 1]!, tint.lut[e + 2]!] as [number, number, number];
    });
  }
  return { nx, ny, x0, y0, zoom, families: out, anyValid, tint: tintLevels, tintRgb, buildMs: performance.now() - t0 };
}

/** Lattice sample at a view point (travel vector and height); false outside the lattice or next to an invalid node. */
const sample = { px: 0, py: 0, hs: 0 };

function sampleLattice(l: Lattice, f: FamilyLattice, x: number, y: number): boolean {
  const fx = (x - l.x0) * INV_LATTICE_PX;
  const fy = (y - l.y0) * INV_LATTICE_PX;
  if (!(fx >= 0 && fy >= 0 && fx < l.nx - 1 && fy < l.ny - 1)) return false;
  const i = fx | 0;
  const j = fy | 0;
  const a = j * l.nx + i;
  if (!f.cellValid[a]) return false;
  const b = a + 1;
  const c = a + l.nx;
  const d = c + 1;
  const tx = fx - i;
  const ty = fy - j;
  const wa = (1 - tx) * (1 - ty);
  const wb = tx * (1 - ty);
  const wc = (1 - tx) * ty;
  const wd = tx * ty;
  const px = f.px[a]! * wa + f.px[b]! * wb + f.px[c]! * wc + f.px[d]! * wd;
  const py = f.py[a]! * wa + f.py[b]! * wb + f.py[c]! * wc + f.py[d]! * wd;
  // (|px|, |py| <= 1, a blend of unit vectors: no overflow, so no need for Math.hypot, which is slower.)
  const m = Math.sqrt(px * px + py * py);
  if (!(m > 0.5)) return false; // the nodes disagree too much to give a direction
  sample.px = px / m;
  sample.py = py / m;
  sample.hs = f.hs[a]! * wa + f.hs[b]! * wb + f.hs[c]! * wc + f.hs[d]! * wd;
  return true;
}

/** Whether the four lattice nodes around a view point are all valid (false outside the lattice). */
function nodesValid(l: Lattice, f: FamilyLattice, x: number, y: number): boolean {
  const fx = (x - l.x0) * INV_LATTICE_PX;
  const fy = (y - l.y0) * INV_LATTICE_PX;
  if (!(fx >= 0 && fy >= 0 && fx < l.nx - 1 && fy < l.ny - 1)) return false;
  return f.cellValid[(fy | 0) * l.nx + (fx | 0)] === 1;
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
  /**
   * State key, unique per (column, row): column x 2^24 + row. Rows are 0..nWorld - 1, under 2^24
   * up to the map's highest zoom, 19 (9.6 million rows of 14 px cells; zoom 20 would pass it),
   * and the key stays an exact integer.
   */
  key: number;
  /** Phase offset of the cell's life cycle, 0..1. */
  phase: number;
  /** Life (s) used to place the cell's first dash: 3 T / tau with T at the cell centre (else 10 s). */
  lifeRef: number;
  /** The cell's dash in `owner` (a DashState store), held here so a frame needs no lookups. */
  dash?: CellDash;
  owner?: Map<number, CellDash>;
  /**
   * The store (`testedBy`) and life number (`testedLife`) of the dash last tested against the
   * view, and the animation time [idleFrom, idleUntil) (s) of that life in which the cell draws
   * nothing in this view: its birth place had no data, or its whole path from birth to the end
   * of its life stays outside the view (else an empty interval). A frame of that store skips
   * the cell then; the cell's next life is placed exactly as if it had been visited (dashAt
   * replays the lives in order).
   */
  testedBy?: Map<number, CellDash>;
  testedLife: number;
  idleFrom: number;
  idleUntil: number;
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
  const cells: Array<Pick<DashCell, "ci" | "cj" | "wrapped" | "key" | "phase" | "lifeRef"> & { keep: number }> = [];
  for (let cj = cj0; cj <= cj1; cj++) {
    const lat = mercLat(((cj + 0.5) * cell) / view.worldPx);
    for (let ci = ci0; ci <= ci1; ci++) {
      const wrapped = ((ci % nWorld) + nWorld) % nWorld;
      const tp = Math.abs(lat) < 85 ? sampleGrid(f.grids.tp, lat, mercLon(((wrapped + 0.5) * cell) / view.worldPx), 0.5) : NaN;
      cells.push({
        ci,
        cj,
        wrapped,
        key: ci * 16777216 + cj,
        phase: hash01(wrapped * 8 + seed, cj, 7),
        lifeRef: dashLifeSeconds(tp > 0 ? tp : REFERENCE_PERIOD_S),
        keep: hash01(wrapped * 8 + seed, cj, 11),
      });
    }
  }
  // In a fixed hashed order: a frame over budget leaves out an even, random share of the
  // cells, never a region, and the same one from frame to frame. The cells are made again in
  // that order (with every field set), so a frame walks them in memory order with one shape.
  cells.sort((a, b) => a.keep - b.keep);
  const ordered: DashCell[] = cells.map(({ ci, cj, wrapped, key, phase, lifeRef }) => ({ ci, cj, wrapped, key, phase, lifeRef, dash: undefined, owner: undefined, testedBy: undefined, testedLife: NaN, idleFrom: 0, idleUntil: 0 }));
  const grid = { cell, cells: ordered };
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
 * Quad batches: per system index, tint level and opacity level, the blobs (or bar pieces) of
 * this frame. Each quad is the unit rectangle under its own transform: QUAD_STRIDE numbers per
 * quad, the matrix (a, b, c, d, e, f) in CSS px. A batch sets its fill and opacity once.
 */
interface Batch {
  /** `n` numbers used (the array is reused and only grows). */
  q: Float64Array;
  n: number;
  frame: number;
  system: WaveSystem;
}
const QUAD_STRIDE = 6;
/** Tint slots per system: one per tint level, and one for dashes without a colour under them. */
const PER_SYSTEM = TINT_LEVELS + 1;
/**
 * Opacity levels a dash's opacity is rounded to (0..DASH_ALPHA_MAX in steps of ALPHA_STEP,
 * 0.039), so a batch sets it once: one canvas call fewer per dash. A step is about 6 grey
 * levels of the tint, finer than the life fade moves in two frames.
 */
export const ALPHA_LEVELS = 24;
export const ALPHA_STEP = DASH_ALPHA_MAX / (ALPHA_LEVELS - 1);
/** The opacity a dash of opacity `alpha` is drawn at (rounded to its level). */
export const drawnAlpha = (alpha: number): number => Math.round(alpha / ALPHA_STEP) * ALPHA_STEP;
const batchPool: Array<Batch | undefined> = [];
let poolFrame = 0;

/** The batch of this frame for a key (reset on first use in a frame, grown as needed). */
function batchOf(key: number, frame: number, system: WaveSystem): Batch {
  let b = batchPool[key];
  if (!b) batchPool[key] = b = { q: new Float64Array(64 * QUAD_STRIDE), n: 0, frame: 0, system };
  if (b.frame !== frame) {
    b.frame = frame;
    b.n = 0;
    b.system = system; // the system at this index can differ from the last frame's
  }
  if (b.n + QUAD_STRIDE > b.q.length) {
    const grown = new Float64Array(b.q.length * 2);
    grown.set(b.q);
    b.q = grown;
  }
  return b;
}

/**
 * Fill styles per context: a blob's gradient runs along the unit rectangle's y (0 = the
 * leading edge, 1 = the back) in the tint with BLOB_PROFILE's opacities, so the canvas
 * transform of each blob stretches it over that blob. A bar is a plain colour, or, split, a
 * gradient along its x (the crest) with hard stops: opaque, a transparent gap of `gap` of its
 * length in the middle, opaque again.
 */
const fillCache = new WeakMap<CanvasRenderingContext2D, Map<string, CanvasGradient | string>>();

function fillOf(ctx: CanvasRenderingContext2D, style: FamilyStyle, [r, g, b]: readonly [number, number, number]): CanvasGradient | string {
  let cache = fillCache.get(ctx);
  // (At most TINT_LEVELS x 4 systems per colour scale; cleared if many scales came and went.)
  if (!cache || cache.size > 1024) fillCache.set(ctx, (cache = new Map()));
  const key = `${style.shape}|${style.gap}|${r},${g},${b}`;
  let fill = cache.get(key);
  if (fill == null) {
    if (style.shape === "bar" && style.gap > 0) {
      const gradient = ctx.createLinearGradient(-0.5, 0, 0.5, 0);
      const solid = `rgb(${r}, ${g}, ${b})`;
      const clear = `rgba(${r}, ${g}, ${b}, 0)`;
      const a = (1 - style.gap) / 2;
      for (const [at, color] of [
        [0, solid],
        [a, solid],
        [a, clear],
        [1 - a, clear],
        [1 - a, solid],
        [1, solid],
      ] as const)
        gradient.addColorStop(at, color);
      fill = gradient;
    } else if (style.shape === "bar") {
      fill = `rgb(${r}, ${g}, ${b})`;
    } else {
      const gradient = ctx.createLinearGradient(0, 0, 0, 1);
      for (const stop of BLOB_PROFILE) gradient.addColorStop(stop.at, `rgba(${r}, ${g}, ${b}, ${stop.alpha})`);
      fill = gradient;
    }
    cache.set(key, fill);
  }
  return fill;
}

export interface FrameStats {
  /** Dashes drawn (data-crests). */
  crests: number;
  /** Filled quads drawn (one per dash: a blob, or a bar with its gap in its fill). */
  quads: number;
  /** Path vertices stroked (the reduced-motion arrowheads, three each). */
  vertices: number;
  /** Canvas draw calls (fills and strokes). */
  strokes: number;
  /** Dash cells visited. */
  cells: number;
  arrows: number;
  /** Blobs drawn with their motion smear (deeper than the bright head; data-dash-trails). */
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
  /** Most filled quads this frame (default MAX_FRAME_QUADS). */
  maxQuads?: number;
  /** The dashes kept between frames (a fresh state places every dash from the clock alone). */
  state?: DashState;
  /**
   * Skip, for the rest of its dash's life, a cell that cannot draw in this view (default true;
   * false only so tests can check that skipping changes nothing).
   */
  skipIdle?: boolean;
}

/** The tint level at a view point: the nearest lattice node's (NO_TINT outside the lattice or without a colour field). */
function tintLevelAt(l: Lattice, x: number, y: number): number {
  if (!l.tint) return NO_TINT;
  const i = Math.round((x - l.x0) * INV_LATTICE_PX);
  const j = Math.round((y - l.y0) * INV_LATTICE_PX);
  if (i < 0 || j < 0 || i >= l.nx || j >= l.ny) return NO_TINT;
  return l.tint[j * l.nx + i]!;
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
  const maxQuads = o.maxQuads ?? MAX_FRAME_QUADS;
  const skipIdle = o.skipIdle ?? true;
  const stats: FrameStats = { crests: 0, quads: 0, vertices: 0, strokes: 0, cells: 0, arrows: 0, trails: 0, families: [] };
  let maxKey = -1;
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
    const blob = style.shape === "blob";
    const { cell, cells } = dashGrid(l, f, view);
    const store = state.store(f.system, l.zoom, cell);
    const last = rank === order.length - 1;
    const left = o.budget - stats.crests;
    const cap = stats.crests + (last ? left : Math.floor(left * BUDGET_SHARE));
    const before = stats.crests;
    // The quad budget is shared the same way.
    const qLeft = maxQuads - stats.quads;
    const qCap = stats.quads + (last ? qLeft : Math.floor(qLeft * BUDGET_SHARE));
    const qBefore = stats.quads;
    const arrowsOn = o.still && (f.system === "primary" || f.system === "combined");
    const head = BLOB_HEAD_PX * style.depthFactor;
    const reach = DASH_MAX_PX;
    const right = view.width + reach;
    const bottom = view.height + reach;
    let visited = 0;
    let hit = false;
    let drawnHere = 0;
    for (const c of cells) {
      if (visited >= f.limit) break;
      if (stats.crests >= cap || stats.quads + MAX_DASH_QUADS > qCap) {
        hit = true;
        break;
      }
      visited++;
      if (skipIdle && c.testedBy === store && t >= c.idleFrom && t < c.idleUntil) continue;
      // The hot helpers below are called with #__NOINLINE__: the production minifier would
      // otherwise inline each single-use function as a function expression created and called
      // anew for every cell (a closure allocated per call and more young-generation GC pauses).
      const d = /*#__NOINLINE__*/ dashAt(store, f, c, cell, view, l.zoom, t, state.frame);
      if (skipIdle && (c.testedBy !== store || c.testedLife !== d.k)) {
        // Once a life: a dash without data, or whose path never comes near the view, leaves
        // its cell idle for the rest of that life.
        c.testedBy = store;
        c.testedLife = d.k;
        const x0 = d.wx - ox;
        const y0 = d.wy - oy;
        const x1 = x0 + d.px * d.v * d.life;
        const y1 = y0 + d.py * d.v * d.life;
        const idle = !d.ok || Math.max(x0, x1) < -reach || Math.max(y0, y1) < -reach || Math.min(x0, x1) > right || Math.min(y0, y1) > bottom;
        c.idleFrom = idle ? d.tb : 0;
        c.idleUntil = idle ? d.tb + d.life : 0;
        if (idle) continue;
      }
      if (!d.ok) continue;
      const travel = d.v * (t - d.tb);
      const cx = d.wx + d.px * travel - ox;
      const cy = d.wy + d.py * travel - oy;
      if (cx < -reach || cy < -reach || cx > right || cy > bottom) continue;
      const fade = o.still ? 1 : dashFade((t - d.tb) / d.life);
      // dashAlpha is at most DASH_ALPHA_MAX: a dash this faint is left out before any sampling.
      if (fade * DASH_ALPHA_MAX < MIN_ALPHA) continue;
      // Orientation and height where the dash is now; centre and corners on valid nodes.
      if (!(/*#__NOINLINE__*/ sampleLattice(l, f, cx, cy))) continue;
      const hs = sample.hs;
      const alpha = dashAlpha(hs) * fade;
      if (alpha < MIN_ALPHA) continue;
      const px = sample.px;
      const py = sample.py;
      const tx = -py; // crest tangent: a quarter turn clockwise of the travel vector on screen
      const ty = px;
      const length = dashLength(hs, f.system);
      const half = length / 2;
      const ex = tx * half;
      const ey = ty * half;
      if (!nodesValid(l, f, cx - ex, cy - ey) || !nodesValid(l, f, cx + ex, cy + ey)) continue;
      const level = /*#__NOINLINE__*/ tintLevelAt(l, cx, cy);
      const key = (sys * PER_SYSTEM + (level === NO_TINT ? TINT_LEVELS : level)) * ALPHA_LEVELS + Math.round(alpha / ALPHA_STEP);
      const b = /*#__NOINLINE__*/ batchOf(key, frame, f.system);
      const q = b.q;
      let n = b.n;
      if (blob) {
        // The blob: its leading edge on the crest line through (cx, cy), its body the band the
        // crest swept over (no further back than its birth while animating), on valid nodes
        // at its back corners too (else only its head is drawn, if that is).
        const full = blobDepth(hs, f.system, d.v, d.s);
        let depth = o.still ? full : Math.min(full, head + d.v * (t - d.tb));
        let back = nodesValid(l, f, cx - px * depth - ex, cy - py * depth - ey) && nodesValid(l, f, cx - px * depth + ex, cy - py * depth + ey);
        if (!back && depth > head) {
          depth = head;
          back = nodesValid(l, f, cx - px * depth - ex, cy - py * depth - ey) && nodesValid(l, f, cx - px * depth + ex, cy - py * depth + ey);
        }
        if (!back) continue;
        // Unit rectangle x in [-0.5, 0.5] (along the crest), y in [0, 1] (back against the travel).
        q[n] = tx * length;
        q[n + 1] = ty * length;
        q[n + 2] = -px * depth;
        q[n + 3] = -py * depth;
        q[n + 4] = cx;
        q[n + 5] = cy;
        n += QUAD_STRIDE;
        stats.quads++;
        if (depth > head + 1e-9) stats.trails++;
      } else {
        // A thin bar centred on the crest line (unit rectangle centred on its origin); its fill
        // leaves a gap of `gap` x the length in the middle (fillOf).
        const width = dashWidth(hs, f.system);
        q[n] = tx * length;
        q[n + 1] = ty * length;
        q[n + 2] = px * width;
        q[n + 3] = py * width;
        q[n + 4] = cx;
        q[n + 5] = cy;
        n += QUAD_STRIDE;
        stats.quads++;
      }
      b.n = n;
      if (key > maxKey) maxKey = key;
      stats.crests++;
      drawnHere++;
      // Reduced motion: an arrowhead ahead of every ARROW_EVERY-th dash shows which way the waves travel.
      if (arrowsOn && drawnHere % ARROW_EVERY === 1) {
        const lead = 1.5;
        arrows.push(cx + px * (lead + ARROW_PX), cy + py * (lead + ARROW_PX), px, py);
        stats.arrows++;
      }
    }
    stats.cells += visited;
    // The cut only shrinks when a budget (dashes or quads) is hit and grows back one cell a
    // frame while well under both, so a cell at the margin does not flicker.
    if (hit) f.limit = visited;
    else if (f.limit < cells.length && stats.crests - before < 0.75 * (cap - before) && stats.quads - qBefore < 0.75 * (qCap - qBefore)) f.limit++;
  });

  // Per system in drawing order (primary last, on top), per tint and opacity level: one fill
  // style and opacity, then every quad of the batch as the unit rectangle under its own
  // transform (composed with the canvas's own, which maps CSS px to canvas px).
  // (Without getTransform, the canvas layer's own transform is its pixel scale, CSS px to canvas px.)
  const base = typeof ctx.getTransform === "function" ? ctx.getTransform() : null;
  const k = ctx.canvas && view.width > 0 ? ctx.canvas.width / view.width : 1;
  const [A, B, C, D, E, F] = base ? [base.a, base.b, base.c, base.d, base.e, base.f] : [k, 0, 0, k, 0, 0];
  // One fill per system and tint level this frame: its batches differ only in opacity.
  const fills: Array<CanvasGradient | string | undefined> = [];
  for (let key = 0; key <= maxKey; key++) {
    const b = batchPool[key];
    if (!b || b.frame !== frame || !b.n) continue;
    const slot = Math.floor(key / ALPHA_LEVELS);
    const style = FAMILY_STYLES[b.system];
    let fill = fills[slot];
    if (fill === undefined) {
      const level = slot % PER_SYSTEM;
      const field = level < TINT_LEVELS && l.tintRgb ? l.tintRgb[level]! : null;
      fills[slot] = fill = fillOf(ctx, style, dashTint(field, b.system));
    }
    ctx.fillStyle = fill;
    ctx.globalAlpha = (key % ALPHA_LEVELS) * ALPHA_STEP;
    const y0 = style.shape === "blob" ? 0 : -0.5;
    const q = b.q;
    for (let i = 0; i < b.n; i += QUAD_STRIDE) {
      const a = q[i]!;
      const bb = q[i + 1]!;
      const c = q[i + 2]!;
      const d = q[i + 3]!;
      const e = q[i + 4]!;
      const f = q[i + 5]!;
      ctx.setTransform(A * a + C * bb, B * a + D * bb, A * c + C * d, B * c + D * d, A * e + C * f + E, B * e + D * f + F);
      ctx.fillRect(-0.5, y0, 1, 1);
      stats.strokes++;
    }
  }
  ctx.globalAlpha = 1;
  ctx.setTransform(A, B, C, D, E, F);
  if (arrows.length) {
    ctx.lineJoin = "round";
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
      stats.vertices += 3;
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

/** Frames per adaptation step of QuadBudget, and frames after a start it ignores (each dash placed afresh). */
const BUDGET_WINDOW = 20;
const BUDGET_WARMUP_FRAMES = 15;

/**
 * The device-adaptive quad budget (lib/map-waves DRAW_TARGET_MS), fed with the draw time of
 * each animated frame and the quads it drew. Every BUDGET_WINDOW frames: when their median
 * draw time is over the target, the cap drops to the quads those frames drew, scaled by
 * target / time (and 5 % under), never below MIN_FRAME_QUADS; when it is under 80 % of the
 * target and the cap limited those frames (they drew at least 90 % of it), the cap grows by
 * 15 % (less than 1 / 0.8, so it settles instead of swinging), up to MAX_FRAME_QUADS. A quiet
 * view far under the cap never raises it.
 */
export class QuadBudget {
  cap = MAX_FRAME_QUADS;
  private times: number[] = [];
  private quads: number[] = [];

  /** One frame's draw time (ms) and quads drawn; returns true when the cap grew. */
  record(ms: number, quads: number): boolean {
    this.times.push(ms);
    this.quads.push(quads);
    if (this.times.length < BUDGET_WINDOW) return false;
    const ms50 = this.times.sort((a, b) => a - b)[Math.floor(BUDGET_WINDOW / 2)]!;
    const drawn = this.quads.sort((a, b) => a - b)[Math.floor(BUDGET_WINDOW / 2)]!;
    this.times = [];
    this.quads = [];
    if (ms50 > DRAW_TARGET_MS) {
      this.cap = Math.max(MIN_FRAME_QUADS, Math.min(this.cap, Math.floor((drawn * 0.95 * DRAW_TARGET_MS) / ms50)));
    } else if (ms50 < 0.8 * DRAW_TARGET_MS && drawn >= 0.9 * this.cap && this.cap < MAX_FRAME_QUADS) {
      this.cap = Math.min(MAX_FRAME_QUADS, Math.ceil(this.cap * 1.15));
      return true;
    }
    return false;
  }
}

export interface WaveStats extends FrameStats {
  /** The adaptive quad budget of the frame (QuadBudget.cap). */
  quadCap: number;
  drawMs: number;
  /** Median and 95th percentile of the frame draw time over the last 120 frames. */
  drawMsP50: number;
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
  private tint: DashTintSource | null = null;
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
  private readonly budget = new QuadBudget();
  /** Animated frames drawn since the last start (the first ones place every dash afresh). */
  private sinceStart = 0;
  stats: WaveStats | null = null;
  private listener: ((stats: WaveStats) => void) | null = null;

  /** Called after each drawn frame (the canvas layer writes its data attributes); null stops. */
  setListener(listener: ((stats: WaveStats) => void) | null): void {
    this.listener = listener;
  }

  /**
   * New fields (another forecast step or other systems): the same dashes live on, the same
   * clock. `tint` (when given, also null) is the colour field the dashes are tinted from.
   */
  setFamilies(families: WaveFamilyGrids[], tint?: DashTintSource | null): void {
    this.families = families;
    if (tint !== undefined) this.tint = tint;
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
    this.lattice = buildLattice(view, this.families, land, this.tint);
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
    this.sinceStart = 0;
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
    // (The cap this frame drew under: record() below may change it for the next frame.)
    const quadCap = this.budget.cap;
    const frame = drawDashes(ctx, view, l, { t: still ? 0 : this.time, still, budget: dashBudget(view.width), maxQuads: quadCap, state: this.dashes });
    if (this.mask) {
      ctx.globalCompositeOperation = "destination-in";
      ctx.drawImage(this.mask, 0, 0, view.width, view.height);
      ctx.globalCompositeOperation = "source-over";
    }
    const ms = performance.now() - t0;
    // The budget learns from settled animated frames only; when it grows, every system may
    // visit all its cells again (the next frame finds the new cut).
    if (!still && ++this.sinceStart > BUDGET_WARMUP_FRAMES && this.budget.record(ms, frame.quads)) for (const f of l.families) f.limit = Infinity;
    this.times.push(ms);
    if (this.times.length > 120) this.times.shift();
    const sorted = [...this.times].sort((a, b) => a - b);
    this.framesDrawn++;
    this.stats = { ...frame, quadCap, drawMs: ms, drawMsP50: sorted[Math.floor(sorted.length / 2)]!, drawMsP95: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))]!, latticeMs: l.buildMs, frames: this.framesDrawn };
    this.listener?.(this.stats);
  }
}

/** Longest clock step of one frame (after a stall the dashes do not jump). */
const MAX_STEP_MS = 100;
