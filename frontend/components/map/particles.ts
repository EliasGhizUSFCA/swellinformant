/**
 * Wind particles drawn over the ocean colour field: thin white streaks with fading trails.
 *
 * Motion is time-based: a particle moves vel x dt / FRAME_MS, so it covers the same distance
 * per second at 60, 120 or 144 Hz (Flow.speed is CSS px per 1/60 s per flow unit: 0.2 for
 * wind, i.e. 12 px/s per m/s, a visual scale, not ground speed). Trails fade the same way in
 * two renderers: on a half-float canvas each frame dims the previous one by FADE^(dt /
 * FRAME_MS) and draws the newest segments; on an 8-bit canvas (no float16 colorType), where
 * that dimming stops at alpha 6/255 and leaves a veil, trails.ts keeps the recent positions
 * and redraws them from a cleared canvas every frame.
 *
 * Velocities: the screen vector of a flow (u east, v north) is (u, -v) x speed. Web Mercator
 * is conformal, so this keeps the bearing at every latitude (no cos(lat) factor on one
 * component). Particles live in canvas pixels of a fixed view; the map pauses them while it
 * moves and reseeds them when it settles. A new flow (the next forecast step) keeps their
 * positions and trails. A particle that reaches a point without data (outside the grid; the
 * wind field has no land mask) is respawned elsewhere.
 */
import type { Grid } from "@/lib/map-fields";
import { sampleGrid } from "@/lib/map-fields";
import { viewToLatLng, type PixelView } from "@/lib/map-geo";

import { FADE, FRAME_MS, HEAD_ALPHA, TrailRenderer } from "./trails";

export interface Flow {
  /** East / north components (m/s for wind). */
  u: Grid;
  v: Grid;
  /** CSS pixels per 1/60 s per flow unit. */
  speed: number;
}

/** "fade": dim the previous frame (needs a float16 canvas); "explicit": trails.ts. */
export type TrailMode = "fade" | "explicit";

interface Particle {
  x: number;
  y: number;
  /** Age and lifetime, ms of animation time. */
  age: number;
  maxAge: number;
}

/** Longest step one frame may take (a frame after a stall, a background tab). */
export const MAX_DT_MS = 50;
/** Particle lifetime, ms (between 50 and 110 frames at 60 Hz, as before). */
const MIN_AGE_MS = 50 * FRAME_MS;
const AGE_SPREAD_MS = 60 * FRAME_MS;
const LINE_WIDTH = 1.1;

/** Particle count for a view: denser on large screens, fewer on phones. */
export function particleCount(width: number, height: number): number {
  const area = width * height;
  return width < 640 ? Math.min(600, Math.round(area / 1100)) : Math.min(3500, Math.round(area / 650));
}

export interface ParticleOptions {
  /** Random source in [0, 1) (tests pass a seeded one). */
  random?: () => number;
  /** Particle count for a view size. */
  count?: (width: number, height: number) => number;
  /** Fixed lifetime in ms instead of the random 50-110 frames. */
  maxAgeMs?: number;
}

export class ParticleAnimator {
  private particles: Particle[] = [];
  private frame = 0;
  private running = false;
  private view: PixelView | null = null;
  private ctx: CanvasRenderingContext2D | null = null;
  private flow: Flow | null = null;
  private last: number | null = null;
  /** Animation clock, ms (advances only while running). */
  private clock = 0;
  private mode: TrailMode = "fade";
  private readonly trails = new TrailRenderer();
  private readonly random: () => number;
  private readonly count: (width: number, height: number) => number;
  private readonly fixedAge: number | null;

  constructor(opts: ParticleOptions = {}) {
    this.random = opts.random ?? Math.random;
    this.count = opts.count ?? particleCount;
    this.fixedAge = opts.maxAgeMs ?? null;
  }

  /** Sets the flow. Running particles keep their positions and trails (no reseed). */
  setFlow(flow: Flow | null): void {
    this.flow = flow;
  }

  get trailMode(): TrailMode {
    return this.mode;
  }

  /** Screen velocity at a view pixel, CSS px per 1/60 s: (u, -v) x speed. */
  velocity(x: number, y: number): [number, number] | null {
    const f = this.flow;
    const v = this.view;
    if (!f || !v) return null;
    const { lat, lon } = viewToLatLng(v, x, y);
    const u = sampleGrid(f.u, lat, lon, 0.5);
    const n = sampleGrid(f.v, lat, lon, 0.5);
    if (u !== u || n !== n) return null;
    return [u * f.speed, -n * f.speed];
  }

  private spawn(p: Particle, i: number): void {
    const v = this.view!;
    this.trails.clear(i);
    for (let tries = 0; tries < 12; tries++) {
      const x = this.random() * v.width;
      const y = this.random() * v.height;
      if (this.velocity(x, y)) {
        p.x = x;
        p.y = y;
        p.age = 0;
        p.maxAge = this.fixedAge ?? MIN_AGE_MS + Math.floor(this.random() * AGE_SPREAD_MS);
        if (this.mode === "explicit") this.trails.push(i, x, y, this.clock);
        return;
      }
    }
    p.age = p.maxAge; // nothing found this frame; retry on the next one
  }

  /**
   * Starts (or restarts) the animation on a freshly reset canvas for `view`, with the trail
   * renderer the canvas supports. `schedule` = false leaves the frames to the caller (tests).
   */
  start(ctx: CanvasRenderingContext2D, view: PixelView, mode: TrailMode = "fade", schedule = true): void {
    this.stop();
    this.ctx = ctx;
    this.view = view;
    this.mode = mode;
    if (!this.flow) return;
    const n = this.count(view.width, view.height);
    this.particles = Array.from({ length: n }, () => ({ x: 0, y: 0, age: 0, maxAge: 1 }));
    this.trails.reset(mode === "explicit" ? n : 0);
    this.particles.forEach((p, i) => {
      this.spawn(p, i);
      p.age = Math.floor(this.random() * p.maxAge); // stagger respawns
    });
    this.running = true;
    this.last = null;
    if (!schedule) return;
    const tick = (now: number) => {
      if (!this.running) return;
      this.advance(now);
      this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }

  stop(): void {
    this.running = false;
    this.last = null;
    cancelAnimationFrame(this.frame);
  }

  get isRunning(): boolean {
    return this.running;
  }

  /** One frame at timestamp `now` (ms, as requestAnimationFrame passes it). */
  advance(now: number): void {
    const dt = this.last == null ? FRAME_MS : Math.min(MAX_DT_MS, Math.max(0, now - this.last));
    this.last = now;
    this.step(dt);
  }

  /** Positions of the live particles (tests and diagnostics). */
  positions(): Array<[number, number]> {
    return this.particles.map((p) => [p.x, p.y]);
  }

  /** Moves every particle by dt ms and draws the frame. */
  step(dtMs: number): void {
    const ctx = this.ctx;
    const v = this.view;
    if (!ctx || !v) return;
    const f = dtMs / FRAME_MS;
    this.clock += dtMs;
    const explicit = this.mode === "explicit";
    if (!explicit) {
      ctx.save();
      ctx.globalCompositeOperation = "destination-in";
      ctx.fillStyle = `rgba(0, 0, 0, ${Math.pow(FADE, f)})`;
      ctx.fillRect(0, 0, v.width, v.height);
      ctx.restore();
      ctx.beginPath();
    }
    this.particles.forEach((p, i) => {
      if (p.age >= p.maxAge) {
        this.spawn(p, i);
        return;
      }
      const vel = this.velocity(p.x, p.y);
      if (!vel) {
        this.spawn(p, i);
        return;
      }
      const nx = p.x + vel[0] * f;
      const ny = p.y + vel[1] * f;
      if (explicit) this.trails.push(i, nx, ny, this.clock);
      else {
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(nx, ny);
      }
      p.x = nx;
      p.y = ny;
      p.age += dtMs;
      if (nx < 0 || ny < 0 || nx > v.width || ny > v.height) p.age = p.maxAge;
    });
    if (explicit) {
      this.trails.draw(ctx, v.width, v.height, this.clock, LINE_WIDTH);
    } else {
      ctx.strokeStyle = `rgba(255, 255, 255, ${HEAD_ALPHA})`;
      ctx.lineWidth = LINE_WIDTH;
      ctx.stroke();
    }
  }

  /** Reduced motion: short static arrows on a regular grid instead of moving particles. */
  drawArrows(ctx: CanvasRenderingContext2D, view: PixelView, spacing = 46): number {
    this.stop();
    this.view = view;
    let drawn = 0;
    ctx.beginPath();
    for (let y = spacing / 2; y < view.height; y += spacing) {
      for (let x = spacing / 2; x < view.width; x += spacing) {
        const vel = this.velocity(x, y);
        if (!vel) continue;
        const mag = Math.hypot(vel[0], vel[1]);
        if (mag < 1e-6) continue;
        const len = Math.min(18, Math.max(7, mag * 8));
        const ux = vel[0] / mag;
        const uy = vel[1] / mag;
        const x0 = x - (ux * len) / 2;
        const y0 = y - (uy * len) / 2;
        const x1 = x + (ux * len) / 2;
        const y1 = y + (uy * len) / 2;
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
        // Arrowhead pointing where the flow goes.
        ctx.moveTo(x1, y1);
        ctx.lineTo(x1 - ux * 4 - uy * 3, y1 - uy * 4 + ux * 3);
        ctx.moveTo(x1, y1);
        ctx.lineTo(x1 - ux * 4 + uy * 3, y1 - uy * 4 - ux * 3);
        drawn++;
      }
    }
    ctx.strokeStyle = "rgba(255, 255, 255, 0.6)";
    ctx.lineWidth = 1.2;
    ctx.stroke();
    return drawn;
  }
}
