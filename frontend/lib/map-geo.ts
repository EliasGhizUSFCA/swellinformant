/**
 * Geometry for the dark vector basemap and the isobars: Web Mercator maths (the projection
 * Leaflet uses, EPSG:3857 with 256 px tiles), Natural Earth rings prepared for fast canvas
 * drawing, country label positions, greedy label collision and pressure contours.
 *
 * "Mercator units" below are x, y in 0..1 across the whole world (x east from -180°, y south
 * from the top at ~85.05°N); pixel = unit * 256 * 2^zoom.
 */
import { contours } from "d3-contour";
import type { Feature, FeatureCollection, Geometry, Position } from "geojson";

import type { FieldGrid } from "@/lib/map-fields";
import { wrapsLongitude } from "@/lib/map-fields";

export const MAX_LAT = 85.0511287798;
const RAD = Math.PI / 180;

export const mercX = (lon: number) => (lon + 180) / 360;
export function mercY(lat: number): number {
  const phi = Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) * RAD;
  return 0.5 - Math.log(Math.tan(Math.PI / 4 + phi / 2)) / (2 * Math.PI);
}
export const mercLon = (x: number) => x * 360 - 180;
export const mercLat = (y: number) => (2 * Math.atan(Math.exp(Math.PI * (1 - 2 * y))) - Math.PI / 2) / RAD;

/** What a canvas covers: global pixel of its top-left CSS pixel, size, world width in px. */
export interface PixelView {
  originX: number;
  originY: number;
  width: number;
  height: number;
  worldPx: number;
}

export function viewToLatLng(v: PixelView, x: number, y: number): { lat: number; lon: number } {
  return { lon: mercLon((v.originX + x) / v.worldPx), lat: mercLat((v.originY + y) / v.worldPx) };
}

export function latLngToView(v: PixelView, lat: number, lon: number): { x: number; y: number } {
  return { x: mercX(lon) * v.worldPx - v.originX, y: mercY(lat) * v.worldPx - v.originY };
}

/** World copies (offsets in whole worlds) that intersect a horizontal pixel span. */
export function worldCopies(v: PixelView): number[] {
  const first = Math.floor(v.originX / v.worldPx);
  const last = Math.floor((v.originX + v.width) / v.worldPx);
  const out: number[] = [];
  for (let k = first; k <= last; k++) out.push(k);
  return out;
}

/**
 * World copies to draw unwrapped paths for: as `worldCopies`, plus the copy to the west,
 * because a path that starts in the first world may reach past x = 1 (Chukotka's tip at
 * x ≈ 1.03 belongs at the far west of the next copy).
 */
export function pathCopies(v: PixelView): number[] {
  const copies = worldCopies(v);
  return [copies[0]! - 1, ...copies];
}

/** A ring or line in Mercator units with its bounding box. */
export interface MercPath {
  pts: Float64Array; // x0, y0, x1, y1, ...
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export function toMercPath(coords: Position[]): MercPath {
  const pts = new Float64Array(coords.length * 2);
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  coords.forEach(([lon, lat], i) => {
    const x = mercX(lon!);
    const y = mercY(lat!);
    pts[i * 2] = x;
    pts[i * 2 + 1] = y;
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  });
  return { pts, minX, minY, maxX, maxY };
}

/**
 * Longitudes made continuous along a ring or line. world-atlas rings are stitched across the
 * antimeridian (spherical topology for d3-geo), so a ring can step from 179.9° to -180°; on a
 * flat map that step is an edge across the whole world, and the even-odd fill then drops the
 * land in a band (65–71.5°N from Chukotka and Wrangel Island, a sliver at 16.5°S from Fiji).
 * Each step of more than 180° is undone with ±360°, so the ring continues past 180° (then
 * shifted whole so that its westernmost point lies in [-180°, 180°): x starts inside the
 * first world and may reach a little past 1, see `pathCopies`). A closed ring that winds
 * once around the globe (Antarctica's coast) is closed along the nearer pole instead of
 * straight back across the world.
 */
export function unwrapLongitudes(coords: Position[], closed: boolean): Position[] {
  if (coords.length < 2) return coords;
  const out: Position[] = [];
  let shift = 0;
  let prev = coords[0]![0]!;
  let latSum = 0;
  let west = Infinity;
  for (const [lon, lat] of coords) {
    const d = lon! - prev;
    if (d > 180) shift -= 360;
    else if (d < -180) shift += 360;
    prev = lon!;
    out.push([lon! + shift, lat!]);
    latSum += lat!;
    west = Math.min(west, lon! + shift);
  }
  const first = out[0]!;
  const last = out[out.length - 1]!;
  if (closed && Math.abs(last[0]! - first[0]!) > 180) {
    const pole = latSum < 0 ? -90 : 90;
    out.push([last[0]!, pole], [first[0]!, pole]);
  }
  const k = Math.floor((west + 180) / 360);
  if (k !== 0) for (const p of out) p[0] = p[0]! - k * 360;
  return out;
}

type AnyGeo = Geometry | Feature | FeatureCollection;

function forEachGeometry(g: AnyGeo, f: (geom: Geometry) => void): void {
  if (g.type === "FeatureCollection") g.features.forEach((ft) => forEachGeometry(ft, f));
  else if (g.type === "Feature") {
    if (g.geometry) forEachGeometry(g.geometry, f);
  } else if (g.type === "GeometryCollection") g.geometries.forEach((x) => forEachGeometry(x, f));
  else f(g);
}

/** Every polygon ring (outer rings and holes; draw with the even-odd rule), unwrapped. */
export function polygonRings(g: AnyGeo): MercPath[] {
  const out: MercPath[] = [];
  const add = (r: Position[]) => out.push(toMercPath(unwrapLongitudes(r, true)));
  forEachGeometry(g, (geom) => {
    if (geom.type === "Polygon") geom.coordinates.forEach(add);
    else if (geom.type === "MultiPolygon") geom.coordinates.forEach((p) => p.forEach(add));
  });
  return out;
}

export function lineStrings(g: AnyGeo): MercPath[] {
  const out: MercPath[] = [];
  const add = (l: Position[]) => out.push(toMercPath(unwrapLongitudes(l, false)));
  forEachGeometry(g, (geom) => {
    if (geom.type === "LineString") add(geom.coordinates);
    else if (geom.type === "MultiLineString") geom.coordinates.forEach(add);
  });
  return out;
}

/** Planar area of a lon/lat ring, scaled by cos(latitude): a rough km²-proportional size. */
function ringArea(ring: Position[]): number {
  let a = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [x1, y1] = ring[j]!;
    const [x2, y2] = ring[i]!;
    a += (x1! * y2! - x2! * y1!) * Math.cos((((y1! + y2!) / 2) * Math.PI) / 180);
  }
  return Math.abs(a / 2);
}

function inRing(ring: Position[], x: number, y: number): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i]!;
    const [xj, yj] = ring[j]!;
    if (yi! > y !== yj! > y && x < ((xj! - xi!) * (y - yi!)) / (yj! - yi!) + xi!) inside = !inside;
  }
  return inside;
}

/**
 * A point inside a polygon (outer ring + holes) for its label: the centroid of the outer
 * ring when it falls inside, otherwise the middle of the widest inside span on the
 * centroid's latitude (C-shaped or crescent outlines).
 */
export function interiorPoint(polygon: Position[][]): [number, number] {
  const outer = polygon[0] ?? [];
  let cx = 0;
  let cy = 0;
  let a = 0;
  for (let i = 0, j = outer.length - 1; i < outer.length; j = i++) {
    const [x1, y1] = outer[j]!;
    const [x2, y2] = outer[i]!;
    const f = x1! * y2! - x2! * y1!;
    cx += (x1! + x2!) * f;
    cy += (y1! + y2!) * f;
    a += f;
  }
  if (Math.abs(a) < 1e-12) {
    const n = Math.max(1, outer.length);
    return [outer.reduce((s, p) => s + p[0]!, 0) / n, outer.reduce((s, p) => s + p[1]!, 0) / n];
  }
  cx /= 3 * a;
  cy /= 3 * a;
  const inside = (x: number, y: number) => inRing(outer, x, y) && !polygon.slice(1).some((h) => inRing(h, x, y));
  if (inside(cx, cy)) return [cx, cy];
  const xs: number[] = [];
  for (const ring of polygon) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i]!;
      const [xj, yj] = ring[j]!;
      if (yi! > cy !== yj! > cy) xs.push(((xj! - xi!) * (cy - yi!)) / (yj! - yi!) + xi!);
    }
  }
  xs.sort((p, q) => p - q);
  let best: [number, number] | null = null;
  let width = 0;
  for (let i = 0; i + 1 < xs.length; i += 2) {
    const w = xs[i + 1]! - xs[i]!;
    if (w > width) {
      width = w;
      best = [(xs[i]! + xs[i + 1]!) / 2, cy];
    }
  }
  return best ?? [cx, cy];
}

export interface PlaceLabel {
  name: string;
  lon: number;
  lat: number;
  /** Rough size of the largest polygon (cos-weighted square degrees), for label density. */
  area: number;
  minZoom: number;
}

/** Smallest zoom at which a country of this size gets a label (collisions still apply). */
export function labelMinZoom(area: number): number {
  if (area >= 150) return 2;
  if (area >= 25) return 3;
  if (area >= 3) return 4;
  if (area >= 0.3) return 5;
  return 6;
}

/** One label per country, at the interior point of its largest polygon, biggest first. */
export function countryLabels(fc: FeatureCollection<Geometry, { name?: string } | null>): PlaceLabel[] {
  const out: PlaceLabel[] = [];
  for (const f of fc.features) {
    const name = f.properties?.name;
    if (!name || !f.geometry) continue;
    const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.type === "MultiPolygon" ? f.geometry.coordinates : [];
    let best: Position[][] | null = null;
    let area = 0;
    for (const raw of polys) {
      const p = raw.map((r) => unwrapLongitudes(r, true));
      const pa = ringArea(p[0] ?? []);
      if (pa > area) {
        area = pa;
        best = p;
      }
    }
    if (!best) continue;
    const [lon, lat] = interiorPoint(best);
    out.push({ name, lon: ((((lon + 180) % 360) + 360) % 360) - 180, lat, area, minZoom: labelMinZoom(area) });
  }
  return out.sort((a, b) => b.area - a.area);
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

const overlaps = (a: Box, b: Box) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/**
 * Greedy collision: keeps candidates in order, dropping any whose box (grown by `gap`)
 * overlaps one already kept or lies outside the view. Returns the kept indices.
 */
export function placeBoxes(boxes: Box[], view: { width: number; height: number }, gap = 4, taken: Box[] = []): number[] {
  const kept: number[] = [];
  const placed = [...taken];
  boxes.forEach((b, i) => {
    const g = { x: b.x - gap, y: b.y - gap, w: b.w + 2 * gap, h: b.h + 2 * gap };
    if (b.x < 0 || b.y < 0 || b.x + b.w > view.width || b.y + b.h > view.height) return;
    if (placed.some((p) => overlaps(p, g))) return;
    placed.push(g);
    kept.push(i);
  });
  return kept;
}

export interface IsoLine {
  value: number;
  /** lon, lat pairs */
  pts: Float64Array;
}

const isobarCache = new WeakMap<FieldGrid, IsoLine[]>();

/** `isobars(g)`, computed once per decoded grid (contouring a global grid takes a while). */
export function isobarsFor(g: FieldGrid): IsoLine[] {
  let lines = isobarCache.get(g);
  if (!lines) {
    lines = isobars(g);
    isobarCache.set(g, lines);
  }
  return lines;
}

/**
 * Every `stride`-th row and column (keeps the first row and column). Sea-level pressure is
 * smooth at the scale of isobars, so contouring a 0.5° grid at 1° loses nothing visible and
 * takes about a third of the time (measured in Node: 175–300 ms → 55–70 ms for 720 × 361).
 */
export function decimate(g: FieldGrid, stride: number): FieldGrid {
  if (stride <= 1 || g.nx % stride !== 0) return g;
  const nx = g.nx / stride;
  const ny = Math.floor((g.ny - 1) / stride) + 1;
  const values = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) values[j * nx + i] = g.values[j * stride * g.nx + i * stride]!;
  return { nx, ny, lat0: g.lat0, lon0: g.lon0, dlat: g.dlat * stride, dlon: g.dlon * stride, values };
}

/**
 * Isobars every `interval` hPa from a pressure grid (d3-contour marching squares with linear
 * smoothing), contoured on at most ~`maxCells` cells. A global grid gets its first column
 * repeated at the end so lines continue across the antimeridian; segments d3-contour adds
 * along the grid border to close its polygons are dropped, leaving the open isolines. Cells
 * without data are filled with the grid mean first (pressure normally has none).
 */
export function isobars(full: FieldGrid, interval = 4, maxCells = 140_000): IsoLine[] {
  const g = decimate(full, Math.max(1, Math.ceil(Math.sqrt((full.nx * full.ny) / maxCells))));
  const wraps = wrapsLongitude(g);
  const W = wraps ? g.nx + 1 : g.nx;
  const H = g.ny;
  const values = new Float64Array(W * H);
  let sum = 0;
  let n = 0;
  let min = Infinity;
  let max = -Infinity;
  for (const v of g.values) {
    if (v === v) {
      sum += v;
      n++;
      if (v < min) min = v;
      if (v > max) max = v;
    }
  }
  if (!n) return [];
  const mean = sum / n;
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      const v = g.values[j * g.nx + (i % g.nx)]!;
      values[j * W + i] = v === v ? v : mean;
    }
  }
  const levels: number[] = [];
  for (let v = Math.ceil(min / interval) * interval; v <= max; v += interval) levels.push(v);
  if (!levels.length) return [];
  const onBorder = (x: number, y: number) => x <= 0 || x >= W || y <= 0 || y >= H;
  const out: IsoLine[] = [];
  for (const mp of contours().size([W, H]).thresholds(levels)(Array.from(values))) {
    for (const polygon of mp.coordinates) {
      for (const ring of polygon) {
        // Rings repeat their first point at the end; walk the loop once, starting on the
        // border when it touches it so that each open isoline comes out in one piece.
        const len = ring.length - 1;
        if (len < 2) continue;
        let start = ring.findIndex((p, k) => k < len && onBorder(p[0]!, p[1]!));
        const closed = start === -1;
        if (closed) start = 0;
        let run: number[] = [];
        const flush = () => {
          if (run.length >= 4) out.push({ value: mp.value, pts: Float64Array.from(run) });
          run = [];
        };
        for (let k = 0; k <= len; k++) {
          if (k === len && !closed) break;
          const [x, y] = ring[(start + k) % len]!;
          if (onBorder(x!, y!)) {
            flush();
            continue;
          }
          run.push(g.lon0 + (x! - 0.5) * g.dlon, g.lat0 - (y! - 0.5) * g.dlat);
        }
        flush();
      }
    }
  }
  return out;
}
