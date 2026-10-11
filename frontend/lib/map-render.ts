/**
 * Paints a model field into an RGBA pixel buffer for the map's colour layer.
 *
 * The buffer covers a Web Mercator view at `k` CSS pixels per buffer pixel (the canvas is
 * then scaled up and smoothed by the browser). Longitude depends only on the column and
 * latitude only on the row, so the grid lookups are computed once per column and once per
 * row; the inner loop is a bilinear blend with no-data handling and a colour-table lookup.
 */
import { LUT_SIZE, type LayerScale } from "@/lib/map-colors";
import { wrapsLongitude, type FieldGrid } from "@/lib/map-fields";
import { mercLat, mercLon, type PixelView } from "@/lib/map-geo";

export interface FieldPaint {
  grid: FieldGrid;
  scale: LayerScale;
  /** RGBA table from buildLut (LUT_SIZE entries). */
  lut: Uint8ClampedArray;
  /** Minimum valid bilinear weight (see sampleGrid). */
  minWeight: number;
  /** Opacity 0..255. */
  alpha: number;
}

export interface PixelBuffer {
  data: Uint8ClampedArray;
  width: number;
  height: number;
}

/** Fills `out` and returns the number of painted (non-transparent) pixels. */
export function renderField(out: PixelBuffer, view: PixelView, k: number, p: FieldPaint): number {
  const { grid: g, lut, minWeight, alpha } = p;
  const { width: W, height: H, data } = out;
  data.fill(0);
  const wraps = wrapsLongitude(g);
  const col0 = new Int32Array(W);
  const col1 = new Int32Array(W);
  const colT = new Float32Array(W);
  for (let x = 0; x < W; x++) {
    const lon = mercLon((view.originX + (x + 0.5) * k) / view.worldPx);
    let fx = (lon - g.lon0) / g.dlon;
    if (wraps) fx = ((fx % g.nx) + g.nx) % g.nx;
    else if (!(fx >= 0 && fx <= g.nx - 1)) {
      col0[x] = -1;
      continue;
    }
    const i0 = Math.floor(fx);
    col0[x] = i0;
    col1[x] = i0 + 1 >= g.nx ? (wraps ? 0 : i0) : i0 + 1;
    colT[x] = fx - i0;
  }
  const { min, max, transform } = p.scale;
  const inv = max > min ? 1 / (max - min) : 0;
  const sqrt = transform === "sqrt";
  const top = LUT_SIZE - 1;
  const v = g.values;
  let painted = 0;
  for (let y = 0; y < H; y++) {
    const lat = mercLat((view.originY + (y + 0.5) * k) / view.worldPx);
    const fy = (g.lat0 - lat) / g.dlat;
    if (!(fy >= 0 && fy <= g.ny - 1)) continue;
    const j0 = Math.floor(fy);
    const j1 = j0 + 1 >= g.ny ? j0 : j0 + 1;
    const ty = fy - j0;
    const r0 = j0 * g.nx;
    const r1 = j1 * g.nx;
    let o = y * W * 4;
    for (let x = 0; x < W; x++, o += 4) {
      const i0 = col0[x]!;
      if (i0 < 0) continue;
      const i1 = col1[x]!;
      const tx = colT[x]!;
      const a = v[r0 + i0]!;
      const b = v[r0 + i1]!;
      const c = v[r1 + i0]!;
      const d = v[r1 + i1]!;
      const wa = (1 - tx) * (1 - ty);
      const wb = tx * (1 - ty);
      const wc = (1 - tx) * ty;
      const wd = tx * ty;
      let val: number;
      if (a === a && b === b && c === c && d === d) {
        val = a * wa + b * wb + c * wc + d * wd;
      } else {
        let s = 0;
        let w = 0;
        if (a === a) {
          s += a * wa;
          w += wa;
        }
        if (b === b) {
          s += b * wb;
          w += wb;
        }
        if (c === c) {
          s += c * wc;
          w += wc;
        }
        if (d === d) {
          s += d * wd;
          w += wd;
        }
        if (!(w > 0 && w >= minWeight)) continue;
        val = s / w;
      }
      let t = (val - min) * inv;
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      if (sqrt) t = Math.sqrt(t);
      const li = ((t * top + 0.5) | 0) * 4;
      data[o] = lut[li]!;
      data[o + 1] = lut[li + 1]!;
      data[o + 2] = lut[li + 2]!;
      data[o + 3] = alpha;
      painted++;
    }
  }
  return painted;
}
