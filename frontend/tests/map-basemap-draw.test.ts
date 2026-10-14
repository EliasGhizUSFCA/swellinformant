import { describe, expect, it } from "vitest";

import { loadBasemap } from "@/components/map/basemap-data";
import { drawBasemap, LAND_ALPHA, LAND_COLOR } from "@/components/map/basemap-draw";
import type { PixelView } from "@/lib/map-geo";

/** A 2D context stand-in that counts every call and records the fill styles used. */
function counting() {
  const calls: Record<string, number> = {};
  const fills: string[] = [];
  const state: Record<string, unknown> = { fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "left", textBaseline: "alphabetic", lineJoin: "miter" };
  const ctx = new Proxy(state, {
    get(target, key: string) {
      if (key in target) return target[key];
      return (...args: unknown[]) => {
        calls[key] = (calls[key] ?? 0) + 1;
        if (key === "fill") fills.push(String(target.fillStyle));
        if (key === "measureText") return { width: 8 * String(args[0]).length };
        return undefined;
      };
    },
    set(target, key: string, value) {
      target[key] = value;
      return true;
    },
  });
  return { ctx: ctx as unknown as CanvasRenderingContext2D, calls, fills };
}

describe("basemap redraw", () => {
  it("draws the Natural Earth land without one closePath per ring (MAPNAV-5)", async () => {
    const geometry = await loadBasemap();
    const view: PixelView = { originX: -208, originY: 64, width: 1440, height: 807, worldPx: 1024 };
    const { ctx, calls } = counting();
    drawBasemap(ctx, view, { geometry, vector: true, isobars: null, cities: null, zoom: 2 });
    expect(calls.closePath ?? 0).toBe(0);
    expect(calls.moveTo).toBeGreaterThan(1000); // every ring and border of the world view
    expect(calls.fill).toBeGreaterThanOrEqual(1);
  });

  it("paints opaque land, or translucent land on the Wind layer", async () => {
    const geometry = await loadBasemap();
    const view: PixelView = { originX: 0, originY: 0, width: 1024, height: 1024, worldPx: 1024 };
    const opaque = counting();
    drawBasemap(opaque.ctx, view, { geometry, vector: true, isobars: null, cities: null, zoom: 2 });
    expect(opaque.fills[0]).toBe(LAND_COLOR);
    const wind = counting();
    drawBasemap(wind.ctx, view, { geometry, vector: true, isobars: null, cities: null, zoom: 2, translucentLand: true });
    const m = /^rgba\(43, 43, 43, ([\d.]+)\)$/.exec(wind.fills[0]!);
    expect(m).not.toBeNull();
    expect(Number(m![1])).toBe(LAND_ALPHA);
    expect(Math.round(Number(m![1]) * 255)).toBeLessThan(255);
  });
});
