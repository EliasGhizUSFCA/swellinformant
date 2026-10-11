import type { FeatureCollection, Geometry } from "geojson";
import type { GeometryCollection, Topology } from "topojson-specification";
import { describe, expect, it } from "vitest";

import type { FieldGrid } from "@/lib/map-fields";
import {
  countryLabels,
  decimate,
  interiorPoint,
  isobars,
  isobarsFor,
  labelMinZoom,
  latLngToView,
  lineStrings,
  mercLat,
  mercLon,
  mercX,
  mercY,
  pathCopies,
  placeBoxes,
  polygonRings,
  unwrapLongitudes,
  viewToLatLng,
  worldCopies,
} from "@/lib/map-geo";

describe("Web Mercator", () => {
  it("matches Leaflet's EPSG:3857 pixel space and round-trips", () => {
    expect(mercX(-180)).toBe(0);
    expect(mercX(180)).toBe(1);
    expect(mercY(0)).toBeCloseTo(0.5);
    expect(mercY(85.0511287798)).toBeCloseTo(0, 6);
    expect(mercY(90)).toBeCloseTo(0, 6); // clamped like Leaflet
    for (const lat of [-70, -33.9, 0, 21.66, 60]) expect(mercLat(mercY(lat))).toBeCloseTo(lat, 9);
    for (const lon of [-179.5, 0, 12.3, 180]) expect(mercLon(mercX(lon))).toBeCloseTo(lon, 9);
    const view = { originX: 300, originY: 200, width: 1440, height: 900, worldPx: 1024 };
    const p = latLngToView(view, -33.9, 25.6);
    const back = viewToLatLng(view, p.x, p.y);
    expect(back.lat).toBeCloseTo(-33.9, 9);
    expect(back.lon).toBeCloseTo(25.6, 9);
  });

  it("lists the world copies a wide or antimeridian view needs", () => {
    expect(worldCopies({ originX: 0, originY: 0, width: 1000, height: 10, worldPx: 1024 })).toEqual([0]);
    expect(worldCopies({ originX: -200, originY: 0, width: 1440, height: 10, worldPx: 1024 })).toEqual([-1, 0, 1]);
    expect(worldCopies({ originX: 3500, originY: 0, width: 1440, height: 10, worldPx: 4096 })).toEqual([0, 1]);
  });
});

describe("labels", () => {
  it("places a label inside a C-shaped polygon even when the centroid is outside", () => {
    // a "C" open to the east: the centroid falls in the empty middle
    const c = [
      [0, 0],
      [10, 0],
      [10, 2],
      [2, 2],
      [2, 8],
      [10, 8],
      [10, 10],
      [0, 10],
      [0, 0],
    ];
    const [x, y] = interiorPoint([c]);
    expect(x).toBeGreaterThanOrEqual(0);
    expect(x).toBeLessThanOrEqual(2);
    expect(y).toBeGreaterThan(0);
    expect(y).toBeLessThan(10);
    // a square: its centroid
    expect(interiorPoint([[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]])).toEqual([2, 2]);
  });

  it("labels each country at its largest polygon, biggest first, with zoom-dependent density", () => {
    const square = (x: number, y: number, s: number) => [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]];
    const labels = countryLabels({
      type: "FeatureCollection",
      features: [
        { type: "Feature", properties: { name: "Islandia" }, geometry: { type: "MultiPolygon", coordinates: [square(0, 0, 1), square(20, 0, 2)] } },
        { type: "Feature", properties: { name: "Bigland" }, geometry: { type: "Polygon", coordinates: square(40, 0, 20) } },
        { type: "Feature", properties: null, geometry: { type: "Polygon", coordinates: square(0, 0, 1) } },
      ],
    });
    expect(labels.map((l) => l.name)).toEqual(["Bigland", "Islandia"]);
    expect(labels[1]!.lon).toBeCloseTo(21); // the larger of Islandia's two islands
    expect(labels[0]!.minZoom).toBe(2);
    expect(labelMinZoom(10)).toBe(4);
    expect(labelMinZoom(0.01)).toBe(6);
  });

  it("drops colliding or off-screen boxes, keeping earlier ones", () => {
    const view = { width: 200, height: 100 };
    const kept = placeBoxes(
      [
        { x: 10, y: 10, w: 50, h: 12 },
        { x: 40, y: 15, w: 50, h: 12 }, // overlaps the first
        { x: 100, y: 10, w: 50, h: 12 },
        { x: 190, y: 10, w: 50, h: 12 }, // off the right edge
      ],
      view,
    );
    expect(kept).toEqual([0, 2]);
    expect(placeBoxes([{ x: 10, y: 10, w: 10, h: 10 }], view, 4, [{ x: 12, y: 12, w: 2, h: 2 }])).toEqual([]);
  });

  it("collects all polygon rings including holes", () => {
    const rings = polygonRings({
      type: "Polygon",
      coordinates: [
        [[0, 0], [10, 0], [10, 10], [0, 0]],
        [[2, 1], [3, 1], [3, 2], [2, 1]],
      ],
    });
    expect(rings).toHaveLength(2);
    expect(rings[0]!.minX).toBeCloseTo(0.5);
    expect(rings[0]!.maxY).toBeCloseTo(0.5);
  });
});

describe("antimeridian", () => {
  it("unwraps a ring stitched across ±180° instead of crossing the whole world", () => {
    // a Chukotka-like ring: 170°E → 170°W and back, as world-atlas stitches it
    const ring = [
      [170, 65],
      [179.9, 65],
      [-180, 65],
      [-170, 66],
      [-170, 70],
      [-180, 69],
      [179.9, 69],
      [170, 70],
      [170, 65],
    ];
    const lons = unwrapLongitudes(ring, true).map((p) => p[0]!);
    expect(lons).toEqual([170, 179.9, 180, 190, 190, 180, 179.9, 170, 170]);
    const [path] = polygonRings({ type: "Polygon", coordinates: [ring] });
    expect(path!.minX).toBeCloseTo(mercX(170));
    expect(path!.maxX).toBeCloseTo(mercX(190)); // > 1: drawn on the next world copy
    expect(path!.maxX - path!.minX).toBeLessThan(0.1);
    // a ring that starts west of the antimeridian is shifted whole into the first world
    const west = unwrapLongitudes([[-180, 65], [179.8, 65], [100, 60], [-175, 66], [-180, 65]], true);
    expect(west.map((p) => p[0])).toEqual([180, 179.8, 100, 185, 180]);
  });

  it("draws one extra world copy to the west for paths that reach past x = 1", () => {
    expect(pathCopies({ originX: 0, originY: 0, width: 390, height: 10, worldPx: 1024 })).toEqual([-1, 0]);
    expect(pathCopies({ originX: -200, originY: 0, width: 1440, height: 10, worldPx: 1024 })).toEqual([-2, -1, 0, 1]);
  });

  it("closes a ring that winds around the globe along the nearer pole", () => {
    const coast = [[-180, -70], [-90, -66], [0, -69], [90, -66], [179.6, -70], [-180, -70]];
    const out = unwrapLongitudes(coast, true);
    expect(out.slice(-3)).toEqual([[180, -70], [180, -90], [-180, -90]]);
    const [path] = polygonRings({ type: "Polygon", coordinates: [coast] });
    expect(path!.minX).toBeCloseTo(0);
    expect(path!.maxX).toBeCloseTo(1);
    expect(path!.maxY).toBeCloseTo(1); // reaches the bottom of the map (clamped pole)
    // lines are never closed
    expect(unwrapLongitudes([[179, 1], [-179, 2]], false)).toEqual([[179, 1], [181, 2]]);
  });

  it("leaves no world-wide edges in the Natural Earth land and borders (regression: 65–72°N band)", async () => {
    const topo = await import("topojson-client");
    const land = (await import("world-atlas/land-50m.json")).default as unknown as Topology<{ land: GeometryCollection }>;
    const countries = (await import("world-atlas/countries-50m.json")).default as unknown as Topology<{ countries: GeometryCollection }>;
    const paths = [
      ...polygonRings(topo.feature(land, land.objects.land)),
      ...lineStrings(topo.mesh(countries, countries.objects.countries, (a, b) => a !== b)),
    ];
    let worst = 0;
    for (const p of paths) {
      expect(p.minX).toBeGreaterThanOrEqual(0);
      expect(p.minX).toBeLessThan(1);
      // every edge except the pole closure (along y = 1 or 0) is short
      for (let i = 2; i < p.pts.length; i += 2) {
        const alongPole = (p.pts[i + 1]! >= 1 - 1e-9 && p.pts[i - 1]! >= 1 - 1e-9) || (p.pts[i + 1]! <= 1e-9 && p.pts[i - 1]! <= 1e-9);
        if (!alongPole) worst = Math.max(worst, Math.abs(p.pts[i]! - p.pts[i - 2]!));
      }
    }
    expect(worst).toBeLessThan(0.1);
    const labels = countryLabels(topo.feature(countries, countries.objects.countries) as FeatureCollection<Geometry, { name?: string }>);
    const russia = labels.find((l) => l.name === "Russia")!;
    expect(russia.lon).toBeGreaterThan(40);
    expect(russia.lon).toBeLessThan(140);
    expect(labels.every((l) => l.lon >= -180 && l.lon < 180)).toBe(true);
  });
});

/** SYNTHETIC pressure field (hPa): a 985 hPa low centred at (latC, lonC) on a global 2° grid. */
function syntheticLow(latC: number, lonC: number): FieldGrid {
  const nx = 180;
  const ny = 91;
  const values = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const lat = 90 - j * 2;
      let dLon = Math.abs(-180 + i * 2 - lonC);
      dLon = Math.min(dLon, 360 - dLon);
      const d = Math.hypot(lat - latC, dLon);
      values[j * nx + i] = 1013 - 28 * Math.exp(-((d / 15) ** 2));
    }
  }
  return { nx, ny, lat0: 90, lon0: -180, dlat: 2, dlon: 2, values };
}

describe("isobars", () => {
  it("draws closed isobars every 4 hPa around a low, at the right radius", () => {
    const lines = isobars(syntheticLow(-40, 30));
    const values = [...new Set(lines.map((l) => l.value))].sort((a, b) => a - b);
    expect(values).toEqual([988, 992, 996, 1000, 1004, 1008, 1012]);
    expect(values.every((v) => v % 4 === 0)).toBe(true);
    // 1000 hPa: 1013 - 28 exp(-(d/15)^2) = 1000 → d = 15 sqrt(ln(28/13)) ≈ 13.1°
    const ring = lines.find((l) => l.value === 1000)!;
    const r = Math.sqrt(Math.log(28 / 13)) * 15;
    for (let i = 0; i < ring.pts.length; i += 2) {
      const d = Math.hypot(ring.pts[i + 1]! + 40, ring.pts[i]! - 30);
      expect(Math.abs(d - r)).toBeLessThan(0.6);
    }
    // closed: first and last points coincide
    expect(ring.pts[0]).toBeCloseTo(ring.pts[ring.pts.length - 2]!);
    expect(ring.pts[1]).toBeCloseTo(ring.pts[ring.pts.length - 1]!);
  });

  it("continues isobars across the antimeridian without border artefacts", () => {
    const lines = isobars(syntheticLow(0, 180));
    expect(lines.length).toBeGreaterThan(0);
    for (const l of lines) {
      for (let i = 0; i < l.pts.length; i += 2) {
        // inside the grid's sample span (lon -180..180 incl. the repeated column, lat ±90)
        expect(l.pts[i]!).toBeGreaterThanOrEqual(-180);
        expect(l.pts[i]!).toBeLessThanOrEqual(180);
        expect(Math.abs(l.pts[i + 1]!)).toBeLessThanOrEqual(90);
      }
    }
    // the 1000 hPa isobar is split at the seam into pieces that meet at ±180°
    const pieces = lines.filter((l) => l.value === 1000);
    expect(pieces.length).toBe(2);
    const ends = pieces.flatMap((p) => [p.pts[0]!, p.pts[p.pts.length - 2]!]);
    expect(ends.every((x) => Math.abs(Math.abs(x) - 180) < 1e-9)).toBe(true);
  });

  it("contours large grids on a decimated copy (every 2nd row and column)", () => {
    const g: FieldGrid = { nx: 8, ny: 5, lat0: 90, lon0: -180, dlat: 45, dlon: 45, values: Float32Array.from({ length: 40 }, (_, k) => k) };
    const d = decimate(g, 2);
    expect([d.nx, d.ny, d.dlat, d.dlon]).toEqual([4, 3, 90, 90]);
    expect(Array.from(d.values)).toEqual([0, 2, 4, 6, 16, 18, 20, 22, 32, 34, 36, 38]);
    expect(decimate(g, 3)).toBe(g); // 8 columns cannot be split in 3 without breaking the wrap
    const low = syntheticLow(-40, 30);
    const coarse = isobars(low, 4, 4000);
    expect(new Set(coarse.map((l) => l.value))).toEqual(new Set(isobars(low).map((l) => l.value)));
  });

  it("returns nothing for a flat or empty field and caches per grid", () => {
    const flat: FieldGrid = { nx: 4, ny: 3, lat0: 10, lon0: -180, dlat: 10, dlon: 90, values: new Float32Array(12).fill(1013) };
    expect(isobars(flat)).toEqual([]);
    expect(isobars({ ...flat, values: new Float32Array(12).fill(NaN) })).toEqual([]);
    const g = syntheticLow(10, 10);
    expect(isobarsFor(g)).toBe(isobarsFor(g));
  });
});
