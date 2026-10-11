/**
 * Map ocean layers: decoding, sampling and caching of the global model fields served by
 * `GET /api/map/fields/{variable}?at=ISO` (contract: backend/app/schemas/map_fields.py).
 *
 * Wire format: zlib (RFC 1950, "deflate" in DecompressionStream) compressed uint8, nx * ny
 * cells, row-major, row 0 at `lat0` (northernmost), row j at `lat0 - j * dlat`, column i at
 * `lon0 + i * dlon` wrapping east. value = offset + scale * byte; byte `nodata` (255) = no
 * data (land, ice, missing). The decoding parameters arrive in the `X-Field-Meta` header.
 *
 * Variables: hs (m, offshore significant wave height of the combined sea state; not surf
 * height), tp (s, peak period), wave_dir (deg, nautical FROM), wind_u / wind_v (m/s, the
 * eastward / northward components of the velocity the air moves with), pressure (hPa).
 */
import type { components } from "@/types/openapi";

export type FieldIndex = components["schemas"]["FieldIndexOut"];
export type FieldVariable = components["schemas"]["FieldVariableOut"];

export const FIELD_VARIABLES = ["hs", "tp", "wave_dir", "wind_u", "wind_v", "pressure"] as const;
export type FieldVariableName = (typeof FIELD_VARIABLES)[number];

/** Mirrors backend FieldMeta (sent as JSON in the X-Field-Meta response header). */
export interface FieldMeta {
  variable: string;
  unit: string;
  valid_time: string;
  run_key: string;
  nx: number;
  ny: number;
  lat0: number;
  lon0: number;
  dlat: number;
  dlon: number;
  scale: number;
  offset: number;
  nodata: number;
}

/** A decoded regular lat/lon grid; NaN marks cells without data. */
export interface FieldGrid {
  nx: number;
  ny: number;
  lat0: number;
  lon0: number;
  dlat: number;
  dlon: number;
  values: Float32Array;
}

const NUMERIC_KEYS = ["nx", "ny", "lat0", "lon0", "dlat", "dlon", "scale", "offset"] as const;

/** Parses and validates the X-Field-Meta header. Throws on anything unusable. */
export function parseFieldMeta(header: string | null): FieldMeta {
  if (!header) throw new Error("Missing X-Field-Meta header");
  const raw = JSON.parse(header) as Record<string, unknown>;
  for (const key of NUMERIC_KEYS) {
    if (typeof raw[key] !== "number" || !Number.isFinite(raw[key])) throw new Error(`Invalid field meta: ${key}`);
  }
  const meta = { nodata: 255, ...raw } as FieldMeta;
  if (!Number.isInteger(meta.nx) || !Number.isInteger(meta.ny) || meta.nx < 2 || meta.ny < 2) throw new Error("Invalid field meta: grid size");
  if (meta.dlat <= 0 || meta.dlon <= 0) throw new Error("Invalid field meta: spacing");
  return meta;
}

/** uint8 cells → physical values (NaN for the nodata byte). */
export function decodeField(bytes: Uint8Array, meta: FieldMeta): FieldGrid {
  const n = meta.nx * meta.ny;
  if (bytes.length !== n) throw new Error(`Field has ${bytes.length} cells, expected ${n}`);
  const values = new Float32Array(n);
  const { scale, offset } = meta;
  const nodata = meta.nodata ?? 255;
  for (let k = 0; k < n; k++) {
    const b = bytes[k]!;
    values[k] = b === nodata ? NaN : offset + scale * b;
  }
  return { nx: meta.nx, ny: meta.ny, lat0: meta.lat0, lon0: meta.lon0, dlat: meta.dlat, dlon: meta.dlon, values };
}

/** zlib-inflates a response body with the browser's DecompressionStream. */
export async function inflate(data: ArrayBuffer | Uint8Array): Promise<Uint8Array> {
  const blob = new Blob([data as BlobPart]);
  const stream = blob.stream().pipeThrough(new DecompressionStream("deflate"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** True when the grid's columns cover the full circle, so column nx-1 neighbours column 0. */
export function wrapsLongitude(g: Pick<FieldGrid, "nx" | "dlon">): boolean {
  return Math.abs(g.nx * g.dlon - 360) < g.dlon * 0.5;
}

/** The four bilinear corners of a point: cell indices and weights (reused, not reentrant). */
interface Corners {
  k: [number, number, number, number];
  w: [number, number, number, number];
}
const scratch: Corners = { k: [0, 0, 0, 0], w: [0, 0, 0, 0] };

function corners(g: FieldGrid, lat: number, lon: number): Corners | null {
  const fy = (g.lat0 - lat) / g.dlat;
  if (!(fy >= 0 && fy <= g.ny - 1)) return null;
  let fx = (lon - g.lon0) / g.dlon;
  const wraps = wrapsLongitude(g);
  if (wraps) fx = ((fx % g.nx) + g.nx) % g.nx;
  else if (!(fx >= 0 && fx <= g.nx - 1)) return null;
  const i0 = Math.floor(fx);
  const j0 = Math.floor(fy);
  const tx = fx - i0;
  const ty = fy - j0;
  const i1 = i0 + 1 >= g.nx ? (wraps ? 0 : i0) : i0 + 1;
  const j1 = j0 + 1 >= g.ny ? j0 : j0 + 1;
  scratch.k[0] = j0 * g.nx + i0;
  scratch.k[1] = j0 * g.nx + i1;
  scratch.k[2] = j1 * g.nx + i0;
  scratch.k[3] = j1 * g.nx + i1;
  scratch.w[0] = (1 - tx) * (1 - ty);
  scratch.w[1] = tx * (1 - ty);
  scratch.w[2] = (1 - tx) * ty;
  scratch.w[3] = tx * ty;
  return scratch;
}

/**
 * Bilinear sample at (lat, lon) with longitude wrap. Corners without data are dropped and
 * the remaining weights renormalised; when the valid weight is below `minWeight` the result
 * is NaN. minWeight 0.5 (default) reports values only where at least half the interpolation
 * weight is real data; a small value lets the colour field reach under the land mask at
 * coastlines instead of leaving a gap.
 */
export function sampleGrid(g: FieldGrid, lat: number, lon: number, minWeight = 0.5): number {
  const c = corners(g, lat, lon);
  if (!c) return NaN;
  let sum = 0;
  let w = 0;
  for (let n = 0; n < 4; n++) {
    const v = g.values[c.k[n]!]!;
    const wn = c.w[n]!;
    if (v === v) {
      sum += v * wn;
      w += wn;
    }
  }
  return w > 0 && w >= minWeight ? sum / w : NaN;
}

/**
 * Direction (nautical degrees FROM) at (lat, lon), interpolated as unit vectors so that 350°
 * and 10° average to 0°, never 180°. NaN without data or when the corners cancel out.
 */
export function sampleDirection(g: FieldGrid, lat: number, lon: number, minWeight = 0.5): number {
  const c = corners(g, lat, lon);
  if (!c) return NaN;
  const rad = Math.PI / 180;
  let s = 0;
  let co = 0;
  let w = 0;
  for (let n = 0; n < 4; n++) {
    const d = g.values[c.k[n]!]!;
    const wn = c.w[n]!;
    if (d === d) {
      s += Math.sin(d * rad) * wn;
      co += Math.cos(d * rad) * wn;
      w += wn;
    }
  }
  if (!(w > 0 && w >= minWeight) || !(Math.hypot(s, co) > 1e-6 * w)) return NaN;
  return (((Math.atan2(s, co) / rad) % 360) + 360) % 360;
}

/** A grid with the same geometry and values mapped cell by cell (NaN stays NaN). */
export function withValues(g: FieldGrid, f: (v: number, k: number) => number): FieldGrid {
  const out = new Float32Array(g.values.length);
  for (let k = 0; k < out.length; k++) {
    const v = g.values[k]!;
    out[k] = v === v ? f(v, k) : NaN;
  }
  return { ...g, values: out };
}

function sameShape(a: FieldGrid, b: FieldGrid): void {
  if (a.nx !== b.nx || a.ny !== b.ny || a.lat0 !== b.lat0 || a.lon0 !== b.lon0 || a.dlat !== b.dlat || a.dlon !== b.dlon) {
    throw new Error("Fields are on different grids");
  }
}

/** Wind speed |(u, v)| in m/s, cell by cell. */
export function windSpeedGrid(u: FieldGrid, v: FieldGrid): FieldGrid {
  sameShape(u, v);
  return withValues(u, (uu, k) => Math.hypot(uu, v.values[k]!));
}

/**
 * Deep-water wave power per metre of wave crest, kW/m: P = rho g^2 Hs^2 Te / (64 pi)
 * ~= 0.49 Hs^2 Te, with the energy period approximated as Te ~= 0.9 Tp (a standard
 * approximation for a single-peaked spectrum). Offshore model energy, not surf.
 */
export function waveEnergyKwPerM(hs: number, tp: number): number {
  return 0.49 * hs * hs * 0.9 * tp;
}

export function waveEnergyGrid(hs: FieldGrid, tp: FieldGrid): FieldGrid {
  sameShape(hs, tp);
  return withValues(hs, (h, k) => waveEnergyKwPerM(h, tp.values[k]!));
}

/** Nautical direction the wind blows FROM, degrees, for velocity components (u east, v north). */
export function directionFromUV(u: number, v: number): number {
  if (!(Math.hypot(u, v) > 1e-9)) return NaN;
  return (((270 - (Math.atan2(v, u) * 180) / Math.PI) % 360) + 360) % 360;
}

/** Particle flow for the wave layers: unit vectors pointing where the waves travel TOWARDS
 * (direction FROM + 180°), scaled by period / 10 s so long-period swell moves faster. */
export function waveFlowGrids(dir: FieldGrid, tp: FieldGrid): { u: FieldGrid; v: FieldGrid } {
  sameShape(dir, tp);
  const rad = Math.PI / 180;
  const u = withValues(dir, (d, k) => -Math.sin(d * rad) * (tp.values[k]! / 10));
  const v = withValues(dir, (d, k) => -Math.cos(d * rad) * (tp.values[k]! / 10));
  return { u, v };
}

/** Small least-recently-used cache (Map keeps insertion order; re-set on read). */
export class LruCache<K, V> {
  private readonly items = new Map<K, V>();

  constructor(readonly capacity: number) {}

  get(key: K): V | undefined {
    const value = this.items.get(key);
    if (value !== undefined) {
      this.items.delete(key);
      this.items.set(key, value);
    }
    return value;
  }

  set(key: K, value: V): void {
    this.items.delete(key);
    this.items.set(key, value);
    while (this.items.size > this.capacity) {
      const oldest = this.items.keys().next().value as K;
      this.items.delete(oldest);
    }
  }

  delete(key: K): void {
    this.items.delete(key);
  }

  has(key: K): boolean {
    return this.items.has(key);
  }

  get size(): number {
    return this.items.size;
  }
}

export const fieldUrl = (variable: string, at: string) => `/api/map/fields/${variable}?at=${encodeURIComponent(at)}`;

/** Fetches, inflates and decodes one field. */
export async function fetchField(variable: string, at: string, signal?: AbortSignal): Promise<FieldGrid> {
  const res = await fetch(fieldUrl(variable, at), { credentials: "same-origin", signal });
  if (!res.ok) throw new Error(`Field ${variable} at ${at}: HTTP ${res.status}`);
  const meta = parseFieldMeta(res.headers.get("X-Field-Meta"));
  return decodeField(await inflate(await res.arrayBuffer()), meta);
}

/**
 * Decoded fields keyed by (variable, valid time): each is downloaded and decoded once and
 * kept in an LRU of ~24 fields (about 1 MB each at 0.5°). In-flight requests are shared.
 */
export class FieldStore {
  private readonly cache: LruCache<string, Promise<FieldGrid>>;

  constructor(
    capacity = 24,
    private readonly loader: (variable: string, at: string) => Promise<FieldGrid> = fetchField,
  ) {
    this.cache = new LruCache(capacity);
  }

  load(variable: string, at: string): Promise<FieldGrid> {
    const key = `${variable}@${at}`;
    const hit = this.cache.get(key);
    if (hit) return hit;
    const pending = this.loader(variable, at).catch((e: unknown) => {
      this.cache.delete(key);
      throw e;
    });
    this.cache.set(key, pending);
    return pending;
  }

  has(variable: string, at: string): boolean {
    return this.cache.has(`${variable}@${at}`);
  }
}
