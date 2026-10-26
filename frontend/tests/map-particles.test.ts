import { describe, expect, it } from "vitest";

import { WIND_PARTICLE_SPEED } from "@/components/map/map-canvas-layers";
import { ParticleAnimator, particleCount, type Flow } from "@/components/map/particles";
import { uvFromWind } from "@/lib/directions";
import type { Grid } from "@/lib/map-fields";
import { latLngToView, viewToLatLng, type PixelView } from "@/lib/map-geo";

/** A canvas context stand-in that records the line segments drawn. */
function recorder() {
  const segments: Array<[number, number, number, number]> = [];
  let from: [number, number] = [0, 0];
  const ctx = {
    beginPath: () => undefined,
    moveTo: (x: number, y: number) => {
      from = [x, y];
    },
    lineTo: (x: number, y: number) => {
      segments.push([from[0], from[1], x, y]);
      from = [x, y];
    },
    stroke: () => undefined,
    strokeStyle: "",
    lineWidth: 1,
    fillStyle: "",
    globalCompositeOperation: "source-over",
    save: () => undefined,
    restore: () => undefined,
    fillRect: () => undefined,
    clearRect: () => {
      clears++;
    },
  };
  let clears = 0;
  return { ctx: ctx as unknown as CanvasRenderingContext2D, segments, clears: () => clears };
}

/** A SYNTHETIC uniform global 1° grid. */
function uniform(value: number): Grid {
  return { nx: 360, ny: 181, lat0: 90, lon0: -180, dlat: 1, dlon: 1, values: new Float32Array(360 * 181).fill(value) };
}

/** A uniform SYNTHETIC wind of `speed` m/s FROM `fromDeg`, built as the API serves it (u east, v north). */
function wind(speed: number, fromDeg: number): Flow {
  const [u, v] = uvFromWind(speed, fromDeg);
  return { u: uniform(u), v: uniform(v), speed: WIND_PARTICLE_SPEED };
}

/** A view of `width` x `height` centred on (lat, lon) at `zoom`. */
function viewAt(zoom: number, lat: number, lon: number, width = 800, height = 600): PixelView {
  const worldPx = 256 * 2 ** zoom;
  const mx = ((lon + 180) / 360) * worldPx;
  const my = (0.5 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / (2 * Math.PI)) * worldPx;
  return { originX: Math.round(mx - width / 2), originY: Math.round(my - height / 2), width, height, worldPx };
}

/** Deterministic random numbers in [0, 1). */
function seeded(seed = 1) {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

// SYNTHETIC flow: 5 m/s eastward wind on a global 10° grid, no data west of 0°.
function halfGrid(value: number): Grid {
  const nx = 36;
  const ny = 19;
  const values = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) values[j * nx + i] = -180 + i * 10 < 0 ? NaN : value;
  return { nx, ny, lat0: 90, lon0: -180, dlat: 10, dlon: 10, values };
}

describe("particles", () => {
  it("scale with the viewport area and stay fewer on phones", () => {
    expect(particleCount(390, 700)).toBeLessThan(particleCount(1440, 900));
    expect(particleCount(390, 700)).toBeLessThanOrEqual(600);
    expect(particleCount(3840, 2160)).toBe(3500);
    expect(particleCount(1000, 600)).toBeGreaterThan(particleCount(800, 600));
  });

  it("reduced motion: static arrows only where the flow has data, pointing downwind", () => {
    const a = new ParticleAnimator();
    a.setFlow({ u: halfGrid(5), v: halfGrid(0), speed: 0.2 });
    const view: PixelView = { originX: 0, originY: 256, width: 1024, height: 512, worldPx: 1024 };
    const { ctx, segments } = recorder();
    const drawn = a.drawArrows(ctx, view);
    expect(drawn).toBeGreaterThan(20);
    expect(segments).toHaveLength(drawn * 3); // a shaft and two head strokes each
    for (let i = 0; i < segments.length; i += 3) {
      const [x0, y0, x1, y1] = segments[i]!;
      expect(viewToLatLng(view, x0, y0).lon).toBeGreaterThan(0); // none over the no-data half
      expect(x1).toBeGreaterThan(x0); // eastward flow → arrows point east
      expect(Math.abs(y1 - y0)).toBeLessThan(1e-9);
    }
    a.setFlow(null);
    expect(a.drawArrows(ctx, view)).toBe(0);
  });
});

describe("wind particle velocity (u east, v north; directions FROM)", () => {
  const bearing = ([x, y]: [number, number]) => (((Math.atan2(x, -y) * 180) / Math.PI) % 360 + 360) % 360;
  const gap = (a: number, b: number) => {
    const d = Math.abs(a - b) % 360;
    return Math.min(d, 360 - d);
  };

  it("moves particles toward where the wind blows, at every latitude and zoom", () => {
    const winds = { N: 0, E: 90, S: 180, W: 270, SW: 225, WNW: 292.5 };
    for (const [name, from] of Object.entries(winds)) {
      for (const lat of [0, 45, 60, 75]) {
        for (const zoom of [2, 6]) {
          const a = new ParticleAnimator();
          a.setFlow(wind(10, from));
          const view = viewAt(zoom, lat, 20);
          a.drawArrows(recorder().ctx, view); // sets the view
          const c = latLngToView(view, lat, 20);
          const vel = a.velocity(c.x, c.y)!;
          // Mercator is conformal: the screen bearing is the geographic one (FROM + 180).
          expect(gap(bearing(vel), from + 180), `${name} wind at ${lat}°, zoom ${zoom}`).toBeLessThan(0.05);
          expect(Math.hypot(...vel)).toBeCloseTo(10 * WIND_PARTICLE_SPEED, 6);
        }
      }
    }
  });

  it("sends a north wind down the screen and a south wind up (the sign of v)", () => {
    const a = new ParticleAnimator();
    const view = viewAt(3, 30, 0);
    a.setFlow(wind(10, 0)); // from the north: v < 0
    a.drawArrows(recorder().ctx, view);
    const [x, y] = a.velocity(400, 300)!;
    expect(y).toBeGreaterThan(1.9); // y grows downwards
    expect(Math.abs(x)).toBeLessThan(1e-9);
    a.setFlow(wind(10, 180)); // from the south: v > 0
    expect(a.velocity(400, 300)![1]).toBeLessThan(-1.9);
  });

  it("keeps flowing across the antimeridian", () => {
    const a = new ParticleAnimator();
    const view = viewAt(3, 0, 180); // a Pacific view: 170°E .. 170°W
    a.setFlow(wind(10, 270));
    a.drawArrows(recorder().ctx, view);
    const east = latLngToView(view, 0, 179.9);
    const west = latLngToView(view, 0, 180.1);
    expect(west.x).toBeGreaterThan(east.x);
    expect(viewToLatLng(view, west.x, west.y).lon).toBeGreaterThan(180); // past the date line, unwrapped
    expect(a.velocity(east.x, east.y)![0]).toBeCloseTo(2, 9);
    expect(a.velocity(west.x, west.y)![0]).toBeCloseTo(2, 9);
  });

  it("draws still arrows pointing downwind for a southerly", () => {
    const a = new ParticleAnimator();
    a.setFlow(wind(8, 180));
    const { ctx, segments } = recorder();
    const drawn = a.drawArrows(ctx, viewAt(3, 20, 0));
    expect(drawn).toBeGreaterThan(50);
    for (let i = 0; i < segments.length; i += 3) {
      const [x0, y0, x1, y1] = segments[i]!;
      expect(y1).toBeLessThan(y0); // up the screen: northward
      expect(Math.abs(x1 - x0)).toBeLessThan(1e-9);
    }
  });
});

describe("wind particle animation", () => {
  const one = (random = seeded()) => new ParticleAnimator({ random, count: () => 1, maxAgeMs: 1e9 });

  it("covers the same distance per second at 60, 120 and 144 Hz (time-based)", () => {
    const distance = (hz: number) => {
      const a = one();
      a.setFlow(wind(10, 270)); // 10 m/s from the west: east at 0.2 x 10 px per 1/60 s
      a.start(recorder().ctx, viewAt(3, 0, 0, 1600, 600), "fade", false);
      const [x0, y0] = a.positions()[0]!;
      for (let k = 0; k <= hz; k++) a.advance((k * 1000) / hz);
      const [x1, y1] = a.positions()[0]!;
      return Math.hypot(x1 - x0, y1 - y0);
    };
    for (const hz of [60, 120, 144]) expect(Math.abs(distance(hz) - 120), `${hz} Hz`).toBeLessThan(1);
  });

  it("does not jump after a stall (a frame advances at most 50 ms)", () => {
    const a = one();
    a.setFlow(wind(10, 270));
    a.start(recorder().ctx, viewAt(3, 0, 0, 1600, 600), "fade", false);
    a.advance(0);
    const [x0] = a.positions()[0]!;
    a.advance(5000);
    expect(a.positions()[0]![0] - x0).toBeCloseTo(0.2 * 10 * (50 / (1000 / 60)), 6);
  });

  it("keeps positions and trails when the forecast step changes (no reseed, no clear)", () => {
    const a = new ParticleAnimator({ random: seeded(7), count: () => 40, maxAgeMs: 1e9 });
    const rec = recorder();
    a.setFlow(wind(10, 270));
    a.start(rec.ctx, viewAt(3, 0, 0), "fade", false);
    for (let k = 0; k < 10; k++) a.advance(k * 16);
    const before = a.positions();
    const clears = rec.clears();
    a.setFlow(wind(10, 0)); // the next step: wind from the north
    expect(a.positions()).toEqual(before);
    a.advance(160);
    const after = a.positions();
    expect(rec.clears()).toBe(clears);
    // Every particle moved on from where it was, now southward.
    after.forEach(([x, y], i) => {
      expect(x).toBeCloseTo(before[i]![0], 6);
      expect(y).toBeGreaterThan(before[i]![1]);
    });
  });

  it("uses the explicit trail renderer on an 8-bit canvas: cleared and redrawn every frame", () => {
    const a = new ParticleAnimator({ random: seeded(3), count: () => 30, maxAgeMs: 1e9 });
    const rec = recorder();
    a.setFlow(wind(10, 270));
    a.start(rec.ctx, viewAt(3, 0, 0), "explicit", false);
    expect(a.trailMode).toBe("explicit");
    for (let k = 0; k < 20; k++) a.advance(k * 16);
    expect(rec.clears()).toBe(20);
    expect(rec.segments.length).toBeGreaterThan(30);
  });
});
