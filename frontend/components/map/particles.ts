/**
 * Flow particles drawn over the ocean colour field: thin white streaks with fading trails
 * (each frame dims the previous one, then draws every particle's newest segment).
 *
 * Particles live in canvas pixels of a fixed view; the map pauses them while it moves and
 * reseeds them when it settles. A particle that reaches a point without data (land, ice,
 * outside the grid) is respawned elsewhere, so none ever drifts over no-data areas.
 */
import { sampleGrid, type FieldGrid } from "@/lib/map-fields";
import { viewToLatLng, type PixelView } from "@/lib/map-geo";

export interface Flow {
  /** East / north components (m/s for wind; period-scaled unit vectors for waves). */
  u: FieldGrid;
  v: FieldGrid;
  /** Screen pixels per frame per flow unit. */
  speed: number;
}

interface Particle {
  x: number;
  y: number;
  age: number;
  maxAge: number;
}

const FADE = 0.92;

/** Particle count for a view: denser on large screens, fewer on phones. */
export function particleCount(width: number, height: number): number {
  const area = width * height;
  return width < 640 ? Math.min(600, Math.round(area / 1100)) : Math.min(3500, Math.round(area / 650));
}

export class ParticleAnimator {
  private particles: Particle[] = [];
  private frame = 0;
  private running = false;
  private view: PixelView | null = null;
  private ctx: CanvasRenderingContext2D | null = null;
  private flow: Flow | null = null;

  setFlow(flow: Flow | null): void {
    this.flow = flow;
  }

  private velocity(x: number, y: number): [number, number] | null {
    const f = this.flow;
    const v = this.view;
    if (!f || !v) return null;
    const { lat, lon } = viewToLatLng(v, x, y);
    const u = sampleGrid(f.u, lat, lon, 0.5);
    const n = sampleGrid(f.v, lat, lon, 0.5);
    if (u !== u || n !== n) return null;
    return [u * f.speed, -n * f.speed];
  }

  private spawn(p: Particle): void {
    const v = this.view!;
    for (let tries = 0; tries < 12; tries++) {
      const x = Math.random() * v.width;
      const y = Math.random() * v.height;
      if (this.velocity(x, y)) {
        p.x = x;
        p.y = y;
        p.age = 0;
        p.maxAge = 50 + Math.floor(Math.random() * 60);
        return;
      }
    }
    p.age = p.maxAge; // nothing found this frame; retry on the next one
  }

  /** Starts (or restarts) the animation on a freshly reset canvas. */
  start(ctx: CanvasRenderingContext2D, view: PixelView): void {
    this.stop();
    this.ctx = ctx;
    this.view = view;
    if (!this.flow) return;
    const n = particleCount(view.width, view.height);
    this.particles = Array.from({ length: n }, () => ({ x: 0, y: 0, age: 0, maxAge: 1 }));
    for (const p of this.particles) {
      this.spawn(p);
      p.age = Math.floor(Math.random() * p.maxAge); // stagger respawns
    }
    this.running = true;
    const tick = () => {
      if (!this.running) return;
      this.step();
      this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }

  stop(): void {
    this.running = false;
    cancelAnimationFrame(this.frame);
  }

  get isRunning(): boolean {
    return this.running;
  }

  private step(): void {
    const ctx = this.ctx;
    const v = this.view;
    if (!ctx || !v) return;
    ctx.save();
    ctx.globalCompositeOperation = "destination-in";
    ctx.fillStyle = `rgba(0, 0, 0, ${FADE})`;
    ctx.fillRect(0, 0, v.width, v.height);
    ctx.restore();
    ctx.beginPath();
    for (const p of this.particles) {
      if (p.age >= p.maxAge) {
        this.spawn(p);
        continue;
      }
      const vel = this.velocity(p.x, p.y);
      if (!vel) {
        this.spawn(p);
        continue;
      }
      const nx = p.x + vel[0];
      const ny = p.y + vel[1];
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(nx, ny);
      p.x = nx;
      p.y = ny;
      p.age++;
      if (nx < 0 || ny < 0 || nx > v.width || ny > v.height) p.age = p.maxAge;
    }
    ctx.strokeStyle = "rgba(255, 255, 255, 0.82)";
    ctx.lineWidth = 1.1;
    ctx.stroke();
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
