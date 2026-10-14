import { deflateSync } from "node:zlib";

import type { Page } from "@playwright/test";

/**
 * Route mocks for the map's ocean layers with SYNTHETIC fields: smooth made-up patterns
 * (storm belts, trade winds, a few lows, and made-up swell / wind-sea partitions), NOT model
 * output and not any real forecast. They only exercise the browser's decoding, rendering,
 * timeline and readout code against the contract in backend/app/schemas/map_fields.py.
 * By default the index lists the six core variables only; pass `variables` with
 * PARTITION_VARIABLES (or a subset) to offer partitions.
 */

export const SYNTHETIC_SOURCE = {
  code: "synthetic",
  name: "Synthetic test fields",
  attribution: "Synthetic test data, not a forecast",
  is_demo: true,
};

const ENCODING: Record<string, { unit: string; offset: number; scale: number; label: string; min: number; max: number }> = {
  hs: { unit: "m", offset: 0, scale: 0.05, label: "Significant wave height", min: 0, max: 10 },
  tp: { unit: "s", offset: 0, scale: 0.1, label: "Peak period", min: 0, max: 20 },
  wave_dir: { unit: "deg", offset: 0, scale: 1.5, label: "Mean wave direction (from)", min: 0, max: 360 },
  wind_u: { unit: "m/s", offset: -40, scale: 0.32, label: "Wind u", min: -30, max: 30 },
  wind_v: { unit: "m/s", offset: -40, scale: 0.32, label: "Wind v", min: -30, max: 30 },
  pressure: { unit: "hPa", offset: 940, scale: 0.5, label: "Sea-level pressure", min: 960, max: 1050 },
  // Partitions, encoded like hs / tp / wave_dir.
  swell1_hs: { unit: "m", offset: 0, scale: 0.05, label: "Primary swell height", min: 0, max: 10 },
  swell1_tp: { unit: "s", offset: 0, scale: 0.1, label: "Primary swell period", min: 0, max: 20 },
  swell1_dir: { unit: "deg", offset: 0, scale: 1.5, label: "Primary swell direction (from)", min: 0, max: 360 },
  swell2_hs: { unit: "m", offset: 0, scale: 0.05, label: "Secondary swell height", min: 0, max: 10 },
  swell2_tp: { unit: "s", offset: 0, scale: 0.1, label: "Secondary swell period", min: 0, max: 20 },
  swell2_dir: { unit: "deg", offset: 0, scale: 1.5, label: "Secondary swell direction (from)", min: 0, max: 360 },
  windsea_hs: { unit: "m", offset: 0, scale: 0.05, label: "Wind sea height", min: 0, max: 10 },
  windsea_tp: { unit: "s", offset: 0, scale: 0.1, label: "Wind sea period", min: 0, max: 20 },
  windsea_dir: { unit: "deg", offset: 0, scale: 1.5, label: "Wind sea direction (from)", min: 0, max: 360 },
};
/** The six core variables (the default index). */
export const VARIABLES = ["hs", "tp", "wave_dir", "wind_u", "wind_v", "pressure"];
/** The nine partition variables and the wave system each belongs to. */
export const PARTITION_SYSTEM: Record<string, "primary" | "secondary" | "windsea"> = {
  swell1_hs: "primary",
  swell1_tp: "primary",
  swell1_dir: "primary",
  swell2_hs: "secondary",
  swell2_tp: "secondary",
  swell2_dir: "secondary",
  windsea_hs: "windsea",
  windsea_tp: "windsea",
  windsea_dir: "windsea",
};
export const PARTITION_VARIABLES = Object.keys(PARTITION_SYSTEM);

const RAD = Math.PI / 180;
const gauss = (d: number, w: number) => Math.exp(-((d / w) ** 2));
const lonGap = (a: number, b: number) => {
  const d = Math.abs(a - b) % 360;
  return Math.min(d, 360 - d);
};

/** One synthetic value, or null for "no data" (polar ice in the wave fields). */
export function syntheticValue(variable: string, lat: number, lon: number, t: number): number | null {
  const wave = variable === "hs" || variable === "tp" || variable === "wave_dir";
  if (wave && Math.abs(lat) > 72) return null;
  const drift = t * 6; // patterns move east 6° per step
  switch (variable) {
    case "hs":
      return (
        0.8 +
        4.2 * gauss(lat + 50, 13) * (0.75 + 0.35 * Math.sin((lon - drift) * RAD * 3)) +
        2.6 * gauss(lat - 47, 9) * (0.6 + 0.4 * Math.cos((lon - drift) * RAD * 2)) +
        0.6 * gauss(Math.abs(lat) - 15, 8)
      );
    case "tp":
      return 7 + 9 * gauss(lat + 42, 26) + 2.5 * Math.sin((lon + drift) * RAD * 1.5) * gauss(lat, 40);
    case "wave_dir": {
      // swell from the south-west in the south turning to from the west in the north, blended
      // smoothly so that the particles do not pile up along a seam at the equator
      const base = 242 + 18 * Math.tanh(lat / 12);
      return (base + 25 * Math.sin((lon - drift) * RAD * 2) + 360) % 360;
    }
    case "wind_u":
      return 9 * Math.sin((Math.abs(lat) - 22.5) * RAD * 4) * gauss(Math.abs(lat) - 40, 35) + 2 * Math.cos((lon + drift) * RAD * 3);
    case "wind_v":
      return 5 * Math.sin((lon - drift) * RAD * 2.5) * Math.cos(lat * RAD * 2);
    case "swell1_hs":
    case "swell1_tp":
    case "swell1_dir": {
      // made-up groundswell: from the SW in the south, from the WNW in the north
      if (Math.abs(lat) > 72) return null;
      if (variable === "swell1_hs") return 0.7 + 3.2 * gauss(lat + 48, 16) * (0.8 + 0.25 * Math.sin((lon - drift) * RAD * 2)) + 1.2 * gauss(lat - 45, 10);
      if (variable === "swell1_tp") return 11.5 + 4 * gauss(lat + 45, 25) + 1.5 * gauss(lat - 45, 12);
      return (262 + 40 * Math.tanh(lat / 15) + 12 * Math.sin((lon - drift) * RAD * 2) + 360) % 360;
    }
    case "swell2_hs":
    case "swell2_tp":
    case "swell2_dir": {
      // made-up older swell crossing the tropics from the south; absent at high latitudes
      if (lat > 48 || lat < -38) return null;
      if (variable === "swell2_hs") return 0.5 + 1.4 * gauss(lat - 5, 22);
      if (variable === "swell2_tp") return 9.5 + 2 * gauss(lat + 10, 20);
      return (190 + 15 * Math.sin((lon + drift) * RAD * 1.5) + 360) % 360;
    }
    case "windsea_hs":
    case "windsea_tp":
    case "windsea_dir": {
      // made-up trade-wind and westerly wind sea; absent where it would be under 0.35 m
      if (Math.abs(lat) > 72) return null;
      const trades = gauss(Math.abs(lat) - 15, 9);
      const westerlies = gauss(Math.abs(lat) - 50, 8);
      const hs = 0.25 + 1.4 * trades + 1.1 * westerlies;
      if (hs < 0.35) return null;
      if (variable === "windsea_hs") return hs;
      if (variable === "windsea_tp") return 4.5 + 2 * trades + 2.5 * westerlies;
      const from = westerlies > trades ? 275 : lat > 0 ? 65 : 115;
      return (from + 10 * Math.sin((lon - drift) * RAD * 3) + 360) % 360;
    }
    case "pressure": {
      const lows = [
        [-52, 20 + drift],
        [-48, 140 + drift],
        [52, -35 + drift],
        [48, 170 + drift],
      ].reduce((s, [la, lo]) => s - 26 * Math.exp(-((Math.hypot(lat - la!, lonGap(lon, lo!)) / 14) ** 2)), 0);
      return 1013 + 8 * gauss(Math.abs(lat) - 30, 14) + lows;
    }
    default:
      return null;
  }
}

export interface GridSpec {
  nx: number;
  ny: number;
}

/** A named SYNTHETIC field generator: value at (lat, lon) for step t, or null for no data. */
export interface SyntheticFields {
  name: string;
  value: (variable: string, lat: number, lon: number, t: number) => number | null;
}

export const DEFAULT_FIELDS: SyntheticFields = { name: "default", value: syntheticValue };

const encoded = new Map<string, Buffer>();

/** zlib-compressed uint8 bytes for one synthetic field, per the wire format. */
export function syntheticFieldBytes(variable: string, t: number, grid: GridSpec, fields: SyntheticFields = DEFAULT_FIELDS): Buffer {
  const key = `${fields.name}|${variable}|${t}|${grid.nx}x${grid.ny}`;
  const hit = encoded.get(key);
  if (hit) return hit;
  const e = ENCODING[variable]!;
  const dlat = 180 / (grid.ny - 1);
  const dlon = 360 / grid.nx;
  const bytes = new Uint8Array(grid.nx * grid.ny);
  for (let j = 0; j < grid.ny; j++) {
    for (let i = 0; i < grid.nx; i++) {
      const v = fields.value(variable, 90 - j * dlat, -180 + i * dlon, t);
      bytes[j * grid.nx + i] = v == null ? 255 : Math.max(0, Math.min(254, Math.round((v - e.offset) / e.scale)));
    }
  }
  const out = deflateSync(bytes);
  encoded.set(key, out);
  return out;
}

/** 3-hourly valid times on the UTC 3-hour grid, starting 6 h before `now`. */
export function syntheticValidTimes(now = Date.now(), steps = 41): string[] {
  const start = Math.floor(now / 10_800_000) * 10_800_000 - 2 * 10_800_000;
  return Array.from({ length: steps }, (_, i) => new Date(start + i * 10_800_000).toISOString().replace(".000Z", "Z"));
}

export interface FieldMock {
  validTimes: string[];
  /** Every field request, as "variable@at". */
  requests: string[];
  /** Every field request URL. */
  urls: string[];
}

/** Serves a synthetic field index and field bytes for /api/map/fields*. */
export async function mockFields(
  page: Page,
  {
    grid = { nx: 720, ny: 361 },
    validTimes = syntheticValidTimes(),
    variables = VARIABLES,
    fields = DEFAULT_FIELDS,
    origin = SYNTHETIC_SOURCE,
  }: { grid?: GridSpec; validTimes?: string[]; variables?: string[]; fields?: SyntheticFields; origin?: typeof SYNTHETIC_SOURCE } = {},
): Promise<FieldMock> {
  const mock: FieldMock = { validTimes, requests: [], urls: [] };
  await page.route(/\/api\/map\/fields(\?.*)?$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        available: true,
        message: null,
        source: origin,
        run_key: "synthetic-run",
        cycle_time: validTimes[0],
        valid_times: validTimes,
        variables: variables.map((name) => ({
          name,
          label: ENCODING[name]!.label,
          unit: ENCODING[name]!.unit,
          display_min: ENCODING[name]!.min,
          display_max: ENCODING[name]!.max,
          group: PARTITION_SYSTEM[name] ? "partition" : "core",
          system: PARTITION_SYSTEM[name] ?? null,
        })),
        updated_at: validTimes[0],
        age_hours: 0,
        stale_after_hours: 12,
        stale: false,
      }),
    }),
  );
  await page.route(/\/api\/map\/fields\/[a-z0-9_]+\?/, (route) => {
    const url = new URL(route.request().url());
    const variable = url.pathname.split("/").pop()!;
    const at = url.searchParams.get("at") ?? "";
    mock.requests.push(`${variable}@${at}`);
    mock.urls.push(url.toString());
    const t = validTimes.findIndex((v) => Date.parse(v) === Date.parse(at));
    if (!ENCODING[variable] || !variables.includes(variable) || t < 0) {
      return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: { code: "not_found", message: "No map field for that variable and time." } }) });
    }
    const e = ENCODING[variable]!;
    const meta = {
      variable,
      group: PARTITION_SYSTEM[variable] ? "partition" : "core",
      unit: e.unit,
      valid_time: at,
      run_key: "synthetic-run",
      nx: grid.nx,
      ny: grid.ny,
      lat0: 90,
      lon0: -180,
      dlat: 180 / (grid.ny - 1),
      dlon: 360 / grid.nx,
      scale: e.scale,
      offset: e.offset,
      nodata: 255,
    };
    return route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      headers: { "X-Field-Meta": JSON.stringify(meta), "Access-Control-Expose-Headers": "X-Field-Meta" },
      body: syntheticFieldBytes(variable, t, grid, fields),
    });
  });
  return mock;
}

/** The index as the backend reports it when no ocean layers exist. */
export async function mockFieldsUnavailable(page: Page, message = "Ocean layers need live NOAA data"): Promise<void> {
  await page.route(/\/api\/map\/fields(\?.*)?$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ available: false, message, valid_times: [], variables: [] }) }),
  );
}
