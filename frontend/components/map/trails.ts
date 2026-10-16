/**
 * Explicit particle trails for canvases without half-float storage (Firefox, Safari and every
 * iOS browser today). The default trail effect dims the previous frame a little each frame;
 * on an 8-bit canvas the dimming stops at alpha 6/255 (round(6 x 0.92) = 6), so a faint
 * white veil builds up wherever particles have been. This renderer instead keeps each
 * particle's recent positions, clears the canvas every frame and strokes each trail with
 * the same age-based fade, so nothing is left behind once a trail is older than TRAIL_MS.
 * A respawned particle leaves its old trail behind to fade out (a "ghost" in a second slot),
 * as the dimming renderer does, instead of wiping it at once.
 *
 * Fade: a segment drawn `age` ms ago has alpha HEAD_ALPHA x FADE^(age / FRAME_MS), the same
 * curve as the dimming renderer, quantised into AGE_BUCKETS (one stroke per bucket).
 */

/** The fade per 1/60 s frame of the dimming renderer (particles.ts). */
export const FADE = 0.92;
/** The reference frame: animation constants are per 1/60 s, applied in proportion to time. */
export const FRAME_MS = 1000 / 60;
/** Alpha of the newest segment. */
export const HEAD_ALPHA = 0.82;
/** Trails end here: the fade has reached alpha 0.05 (0.82 x 0.92^33.5). */
export const TRAIL_MS = 560;
/** One stored point per particle at most this often (the head point always follows the particle). */
export const SAMPLE_MS = 32;
/** Upper ends of the age buckets, ms; each bucket is stroked at the fade of its mid age. */
export const AGE_BUCKETS = [60, 140, 250, 390, TRAIL_MS];

/** Alpha of the dimming renderer for a segment drawn `ageMs` ago. */
export const fadeAlpha = (ageMs: number) => HEAD_ALPHA * Math.pow(FADE, Math.max(0, ageMs) / FRAME_MS);

const BUCKET_ALPHA = AGE_BUCKETS.map((end, i) => fadeAlpha(((i ? AGE_BUCKETS[i - 1]! : 0) + end) / 2));

/** Points kept per particle: one per SAMPLE_MS over TRAIL_MS, plus the head. */
const CAPACITY = Math.ceil(TRAIL_MS / SAMPLE_MS) + 2;

export class TrailRenderer {
  private n = 0;
  private xs = new Float32Array(0);
  private ys = new Float32Array(0);
  private ts = new Float64Array(0);
  /** Ring start (newest point) and length per slot: two slots per particle, the live trail
   * and the fading trail it left when it last respawned. */
  private head = new Int32Array(0);
  private len = new Int32Array(0);
  /** The live slot of each particle (i or i + n). */
  private slot = new Int32Array(0);
  /** Per-bucket segment buffers (x0, y0, x1, y1 per segment), reused. */
  private buffers: Float32Array[] = AGE_BUCKETS.map(() => new Float32Array(0));
  private counts = new Int32Array(AGE_BUCKETS.length);

  /** Resets every trail for `count` particles. */
  reset(count: number): void {
    this.n = count;
    this.xs = new Float32Array(2 * count * CAPACITY);
    this.ys = new Float32Array(2 * count * CAPACITY);
    this.ts = new Float64Array(2 * count * CAPACITY);
    this.head = new Int32Array(2 * count);
    this.len = new Int32Array(2 * count);
    this.slot = Int32Array.from({ length: count }, (_, i) => i);
  }

  /**
   * Starts a new trail for particle `i` (respawned elsewhere: no segment across the jump).
   * The old trail stays behind and fades out with its age; it is dropped early only if the
   * particle respawns twice within TRAIL_MS.
   */
  clear(i: number): void {
    if (i >= this.n) return;
    const s = this.slot[i]!;
    if (!this.len[s]) return; // nothing drawn yet: keep the slot (and the ghost in the other)
    const next = s < this.n ? s + this.n : s - this.n;
    this.slot[i] = next;
    this.len[next] = 0;
  }

  /** Records particle `i` at (x, y) at animation time `t` (ms). */
  push(i: number, x: number, y: number, t: number): void {
    if (i >= this.n) return;
    const s = this.slot[i]!;
    const base = s * CAPACITY;
    let h = this.head[s]!;
    const l = this.len[s]!;
    // Keep one stored point per SAMPLE_MS: within that, the newest point follows the particle.
    // The previous stored point (h + 1) is at least SAMPLE_MS older than a fresh one.
    if (l >= 2 && t - this.ts[base + ((h + 1) % CAPACITY)]! < SAMPLE_MS) {
      this.xs[base + h] = x;
      this.ys[base + h] = y;
      this.ts[base + h] = t;
      return;
    }
    h = (h - 1 + CAPACITY) % CAPACITY;
    this.head[s] = h;
    this.xs[base + h] = x;
    this.ys[base + h] = y;
    this.ts[base + h] = t;
    this.len[s] = Math.min(CAPACITY, l + 1);
  }

  /** Points stored for particle `i`'s live trail (tests and diagnostics). */
  length(i: number): number {
    return i < this.n ? this.len[this.slot[i]!]! : 0;
  }

  /**
   * Clears the canvas and strokes every trail (live or left behind) younger than TRAIL_MS at
   * time `now`. Returns the number of segments drawn.
   */
  draw(ctx: CanvasRenderingContext2D, width: number, height: number, now: number, lineWidth = 1.1): number {
    ctx.clearRect(0, 0, width, height);
    this.counts.fill(0);
    for (let i = 0; i < 2 * this.n; i++) {
      const l = this.len[i]!;
      if (l < 2) continue;
      const base = i * CAPACITY;
      const h = this.head[i]!;
      for (let k = 0; k < l - 1; k++) {
        const a = base + ((h + k) % CAPACITY);
        const b = base + ((h + k + 1) % CAPACITY);
        const age = now - this.ts[a]!;
        if (age >= TRAIL_MS) break;
        const x0 = this.xs[b]!;
        const y0 = this.ys[b]!;
        const x1 = this.xs[a]!;
        const y1 = this.ys[a]!;
        if (x0 === x1 && y0 === y1) continue; // a still particle leaves no ink
        let bucket = 0;
        while (age >= AGE_BUCKETS[bucket]!) bucket++;
        this.add(bucket, x0, y0, x1, y1);
      }
    }
    let drawn = 0;
    ctx.lineWidth = lineWidth;
    for (let bucket = 0; bucket < AGE_BUCKETS.length; bucket++) {
      const c = this.counts[bucket]!;
      if (!c) continue;
      const buf = this.buffers[bucket]!;
      ctx.beginPath();
      for (let s = 0; s < c; s++) {
        ctx.moveTo(buf[s * 4]!, buf[s * 4 + 1]!);
        ctx.lineTo(buf[s * 4 + 2]!, buf[s * 4 + 3]!);
      }
      ctx.strokeStyle = `rgba(255, 255, 255, ${BUCKET_ALPHA[bucket]!.toFixed(3)})`;
      ctx.stroke();
      drawn += c;
    }
    return drawn;
  }

  private add(bucket: number, x0: number, y0: number, x1: number, y1: number): void {
    const c = this.counts[bucket]!;
    let buf = this.buffers[bucket]!;
    if ((c + 1) * 4 > buf.length) {
      const grown = new Float32Array(Math.max(1024, buf.length * 2));
      grown.set(buf);
      this.buffers[bucket] = buf = grown;
    }
    buf[c * 4] = x0;
    buf[c * 4 + 1] = y0;
    buf[c * 4 + 2] = x1;
    buf[c * 4 + 3] = y1;
    this.counts[bucket] = c + 1;
  }
}
