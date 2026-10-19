/**
 * Shared helpers of the crest-dash unit tests (map-dashes.test.ts, map-wavefronts.test.ts):
 * SYNTHETIC grids (made-up uniform or patterned seas, not model output), views, and a 2D
 * context stand-in that records every stroked polyline with its style.
 */
import { vi } from "vitest";

import { buildLattice, DASH_WIDTH_LEVELS, drawDashes, type DashState, type WaveFamilyGrids } from "@/components/map/wavefronts";
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

class RecordedPath {
  polys: Array<Array<[number, number]>> = [];
  moveTo(x: number, y: number) {
    this.polys.push([[x, y]]);
  }
  lineTo(x: number, y: number) {
    this.polys[this.polys.length - 1]!.push([x, y]);
  }
}

/** Path2D for jsdom (which has none): records its subpaths. */
export function installPath2D(): void {
  vi.stubGlobal("Path2D", RecordedPath);
}

/** A 2D context stand-in that records every stroked polyline with its style. */
export function recorder() {
  const lines: Polyline[] = [];
  let path = new RecordedPath();
  const ctx = {
    lineJoin: "miter",
    lineCap: "butt",
    lineWidth: 1,
    strokeStyle: "",
    globalCompositeOperation: "source-over",
    beginPath: () => {
      path = new RecordedPath();
    },
    moveTo: (x: number, y: number) => path.moveTo(x, y),
    lineTo: (x: number, y: number) => path.lineTo(x, y),
    setLineDash: () => undefined,
    stroke: (p?: RecordedPath) => {
      for (const pts of (p ?? path).polys) lines.push({ style: ctx.strokeStyle, width: ctx.lineWidth, cap: ctx.lineCap, pts });
    },
    clearRect: () => undefined,
    drawImage: () => undefined,
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, lines };
}

/** Opacity of an rgba() stroke style. */
export const alphaOf = (l: Polyline) => Number(l.style.slice(l.style.lastIndexOf(",") + 1, -1));
/** Colour (r, g, b) of an rgba() stroke style. */
export const rgbOf = (l: Polyline) => l.style.slice(5, l.style.lastIndexOf(","));

/** The core strokes (pass width 1: the core widths themselves), one polyline per dash or dash half. */
export const cores = (lines: Polyline[]) => lines.filter((l) => l.pts.length === 2 && DASH_WIDTH_LEVELS.includes(l.width));

export interface Dash {
  x: number;
  y: number;
  /** Unit vector along the dash and its length, px. */
  ux: number;
  uy: number;
  length: number;
  width: number;
  alpha: number;
  rgb: string;
}

/** Solid dashes from their core strokes (each a single segment). */
export function dashesOf(lines: Polyline[]): Dash[] {
  return cores(lines).map((l) => {
    const [[x0, y0], [x1, y1]] = l.pts as [[number, number], [number, number]];
    const length = Math.hypot(x1 - x0, y1 - y0);
    return { x: (x0 + x1) / 2, y: (y0 + y1) / 2, ux: (x1 - x0) / length, uy: (y1 - y0) / length, length, width: l.width, alpha: alphaOf(l), rgb: rgbOf(l) };
  });
}

/** Line orientation of a dash in screen degrees, 0..180 (x east, y down). */
export const orientation = (d: Pick<Dash, "ux" | "uy">) => (((Math.atan2(d.uy, d.ux) * 180) / Math.PI) % 180 + 180) % 180;

export function draw(view: PixelView, families: WaveFamilyGrids[], t = 0, still = false, budget = maxDashes(view.width), state?: DashState, land?: Uint8Array) {
  const { ctx, lines } = recorder();
  const lattice = buildLattice(view, families, land);
  const stats = drawDashes(ctx, view, lattice, { t, still, budget, state });
  return { lines, stats, lattice };
}
