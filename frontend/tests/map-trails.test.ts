import { describe, expect, it } from "vitest";

import { ParticleAnimator, type Flow } from "@/components/map/particles";
import { AGE_BUCKETS, FADE, fadeAlpha, HEAD_ALPHA, TRAIL_MS, TrailRenderer } from "@/components/map/trails";
import type { Grid } from "@/lib/map-fields";
import type { PixelView } from "@/lib/map-geo";

/**
 * A 2D context stand-in that keeps what is on the canvas: clearRect wipes it, stroke adds the
 * path's segments with the current strokeStyle. "Ink" = segments drawn since the last clear.
 */
function canvas() {
  let ink: Array<{ alpha: number; seg: [number, number, number, number] }> = [];
  let path: Array<[number, number, number, number]> = [];
  let last: [number, number] = [0, 0];
  const ctx = {
    strokeStyle: "",
    lineWidth: 1,
    beginPath: () => {
      path = [];
    },
    moveTo: (x: number, y: number) => {
      last = [x, y];
    },
    lineTo: (x: number, y: number) => {
      path.push([last[0], last[1], x, y]);
      last = [x, y];
    },
    stroke: () => {
      const alpha = Number(/,\s*([\d.]+)\)$/.exec(ctx.strokeStyle)![1]);
      for (const seg of path) ink.push({ alpha, seg });
    },
    clearRect: () => {
      ink = [];
    },
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, ink: () => ink };
}

describe("8-bit trail renderer (Firefox, Safari, iOS)", () => {
  it("is needed: dimming an 8-bit alpha by 0.92 per frame stops at 6/255 (the veil)", () => {
    let a = 209;
    for (let i = 0; i < 200; i++) a = Math.round(a * FADE);
    expect(a).toBe(6);
  });

  it("fades like the dimming renderer, and ends the trail at TRAIL_MS", () => {
    expect(fadeAlpha(0)).toBe(HEAD_ALPHA);
    expect(fadeAlpha(1000 / 60)).toBeCloseTo(HEAD_ALPHA * FADE, 12);
    expect(fadeAlpha(TRAIL_MS)).toBeLessThan(0.06);
    expect(AGE_BUCKETS[AGE_BUCKETS.length - 1]).toBe(TRAIL_MS);
  });

  it("strokes recent segments only, newest brightest", () => {
    const r = new TrailRenderer();
    r.reset(1);
    for (let t = 0; t <= 600; t += 16) r.push(0, t / 4, 0, t);
    const { ctx, ink } = canvas();
    r.draw(ctx, 400, 100, 600);
    const segs = ink();
    expect(segs.length).toBeGreaterThan(3);
    // nothing older than TRAIL_MS: the oldest point drawn is at most (600 - 560 - 32) / 4 px in
    for (const s of segs) expect(Math.min(s.seg[0], s.seg[2])).toBeGreaterThanOrEqual((600 - TRAIL_MS - 32) / 4);
    const front = Math.max(...segs.map((s) => Math.max(s.seg[0], s.seg[2])));
    expect(front).toBe(148); // the newest point (t = 592 ms)
    const head = segs.find((s) => Math.max(s.seg[0], s.seg[2]) === front)!;
    expect(head.alpha).toBe(Math.max(...segs.map((s) => s.alpha)));
  });

  it("leaves no ink once the particles stop: 0 residual alpha after TRAIL_MS", () => {
    // SYNTHETIC uniform wind of 10 m/s from the west.
    const g = (v: number): Grid => ({ nx: 360, ny: 181, lat0: 90, lon0: -180, dlat: 1, dlon: 1, values: new Float32Array(360 * 181).fill(v) });
    const moving: Flow = { u: g(10), v: g(0), speed: 0.2 };
    const still: Flow = { u: g(0), v: g(0), speed: 0.2 };
    const a = new ParticleAnimator({ count: () => 200, maxAgeMs: 1e9 });
    const { ctx, ink } = canvas();
    const view: PixelView = { originX: 0, originY: 300, width: 1024, height: 400, worldPx: 2048 };
    a.setFlow(moving);
    a.start(ctx, view, "explicit", false);
    let t = 0;
    for (; t < 1000; t += 16) a.advance(t);
    expect(ink().length).toBeGreaterThan(500);
    a.setFlow(still); // the particles stop where they are
    const frames = Math.ceil(TRAIL_MS / 16) + 2;
    for (let k = 0; k < frames; k++, t += 16) a.advance(t);
    expect(ink()).toEqual([]);
  });

  it("starts a new trail for a respawned particle (no segment across the jump)", () => {
    const r = new TrailRenderer();
    r.reset(2);
    r.push(0, 0, 0, 0);
    r.push(0, 10, 0, 40);
    expect(r.length(0)).toBe(2);
    r.clear(0);
    expect(r.length(0)).toBe(0);
    r.push(0, 500, 500, 80);
    const { ctx, ink } = canvas();
    r.draw(ctx, 1000, 1000, 80);
    // Only the old trail, left behind; nothing joins it to the new position.
    expect(ink().map((s) => s.seg)).toEqual([[0, 0, 10, 0]]);
  });

  it("lets the trail a respawned particle left behind fade out like the dimming renderer", () => {
    const r = new TrailRenderer();
    r.reset(1);
    for (let t = 0; t <= 320; t += 16) r.push(0, t / 4, 0, t);
    r.clear(0); // respawned at t = 320 ms
    let brightest = Infinity;
    for (let t = 336; t <= 1000; t += 16) {
      r.push(0, 600, 50 + t / 4, t);
      const { ctx, ink } = canvas();
      r.draw(ctx, 1000, 1000, t);
      const left = ink().filter((s) => s.seg[1] === 0 && s.seg[3] === 0); // the old trail (y = 0)
      if (t - 320 < AGE_BUCKETS[0]!) expect(left.length).toBeGreaterThan(0); // still there just after the respawn
      const top = Math.max(0, ...left.map((s) => s.alpha));
      expect(top).toBeLessThanOrEqual(brightest); // fading with its age, never brightening
      brightest = top;
      if (t - 320 >= AGE_BUCKETS[2]!) expect(top).toBeLessThan(fadeAlpha(AGE_BUCKETS[1]!));
      if (t - 320 >= TRAIL_MS) expect(left).toEqual([]); // gone after TRAIL_MS
    }
    // Respawning again before anything was drawn keeps the trail left behind.
    const q = new TrailRenderer();
    q.reset(1);
    q.push(0, 0, 0, 0);
    q.push(0, 10, 0, 40);
    q.clear(0);
    q.clear(0);
    const { ctx, ink } = canvas();
    q.draw(ctx, 100, 100, 50);
    expect(ink().length).toBe(1);
  });
});
