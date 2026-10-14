import { describe, expect, it } from "vitest";

import { ParticleAnimator, particleCount } from "@/components/map/particles";
import type { Grid } from "@/lib/map-fields";
import { viewToLatLng, type PixelView } from "@/lib/map-geo";

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
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, segments };
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
