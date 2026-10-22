import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildLattice, DASH_ALPHA_LEVELS, DASH_WIDTH_LEVELS, DashState, drawDashes, LATTICE_PX, TRAIL_LEVELS, WaveAnimator } from "@/components/map/wavefronts";
import { mercLat, mercLon, type PixelView } from "@/lib/map-geo";
import {
  ARROW_EVERY,
  crestSpacingPx,
  crestSpeedPxPerS,
  DASH_PASSES,
  dashAlpha,
  dashLength,
  dashWidth,
  MAX_DASHES,
  MAX_FRAME_VERTICES,
  PHONE_MAX_DASHES,
  propagation,
  TRAIL_STEPS,
  trailLength,
} from "@/lib/map-waves";

import { alphaOf, constant, dashesOf, draw, family, globalGrid, installPath2D, orientation, recorder, trailsOf, UNBOUNDED, viewAt, type Dash } from "./map-dash-fixtures";

// Every grid below is SYNTHETIC test data (made-up uniform or patterned seas), not model output.

beforeEach(() => {
  // jsdom has no 2D canvas (the land and ocean masks are skipped) and no Path2D.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  installPath2D();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const VIEW = viewAt(3, -40, -30);
const nearestLevel = (levels: number[], x: number) => levels.reduce((best, v) => (Math.abs(v - x) < Math.abs(best - x) ? v : best), levels[0]!);

describe("orientation: dashes lie along the crests, across the direction of travel", () => {
  it("draws vertical dashes for a uniform sea from the west (270°), each within 1°", () => {
    const { lines, stats } = draw(VIEW, [family("combined", 3, 12, 270)], 2);
    const dashes = dashesOf(lines);
    expect(stats.crests).toBeGreaterThan(500);
    expect(dashes.length).toBe(stats.crests);
    for (const d of dashes) expect(Math.abs(orientation(d) - 90)).toBeLessThan(1);
  });

  it("draws dashes at 45° (NW-SE, a quarter turn from the NE travel) for a sea from 225°, each within 1°", () => {
    const dashes = dashesOf(draw(VIEW, [family("primary", 3, 12, 225)], 2).lines);
    expect(dashes.length).toBeGreaterThan(500);
    for (const d of dashes) expect(Math.abs(orientation(d) - 45)).toBeLessThan(1);
  });
});

describe("motion and spacing: the crest lattice p . W = n s + v t", () => {
  it("moves every dash along the travel direction p at the crest speed v (within 2 %)", () => {
    const tp = 12;
    const s = crestSpacingPx(tp);
    const v = crestSpeedPxPerS(tp, s); // 9 px/s
    const [px, py] = propagation(225)!; // toward the NE
    const state = new DashState();
    const fam = [family("primary", 3, tp, 225)];
    const lattice = buildLattice(VIEW, fam);
    const at = (t: number) => {
      const r = recorder();
      drawDashes(r.ctx, VIEW, lattice, { t, still: false, budget: UNBOUNDED, maxVertices: Infinity, state });
      return dashesOf(r.lines);
    };
    at(3.0);
    const a = at(3.1);
    const dt = 0.25;
    const b = at(3.1 + dt);
    // How many dashes of frame a have a dash of frame b at their place moved by k x v dt
    // along a direction (within 0.01 px). Dashes are on average 18 px apart; the few born or
    // dying in between have no partner.
    const partners = (k: number, dx: number, dy: number) => {
      const index = new Set(b.map((e) => `${Math.round(e.x * 100)},${Math.round(e.y * 100)}`));
      let n = 0;
      for (const d of a) {
        const x = d.x + dx * k * v * dt;
        const y = d.y + dy * k * v * dt;
        let found = false;
        for (const ox of [-1, 0, 1]) for (const oy of [-1, 0, 1]) found ||= index.has(`${Math.round(x * 100) + ox},${Math.round(y * 100) + oy}`);
        if (found) n++;
      }
      return n;
    };
    expect(a.length).toBeGreaterThan(500);
    expect(partners(1, px, py)).toBeGreaterThan(0.9 * a.length);
    // 2 % slower or faster, or the other way (FROM instead of TOWARD), and the dashes are not there.
    for (const [k, dx, dy] of [
      [0.98, px, py],
      [1.02, px, py],
      [1, -px, -py],
      [1, -py, px],
    ] as const) {
      expect(partners(k, dx, dy), `k ${k}, (${dx.toFixed(2)}, ${dy.toFixed(2)})`).toBeLessThan(0.02 * a.length);
    }
  });

  it.each([8, 10, 16])("puts dash centres on crest lines one spacing apart (T = %i s, spacing within 3 %)", (tp) => {
    const s = crestSpacingPx(tp);
    const v = crestSpeedPxPerS(tp, s);
    const t = 4.2;
    const [px, py] = propagation(270)!;
    const dashes = dashesOf(draw(VIEW, [family("combined", 2.5, tp, 270)], t, false, UNBOUNDED).lines);
    expect(dashes.length).toBeGreaterThan(300);
    // Every centre's phase p . W - v t is a multiple of s (W in world px) ...
    const u = dashes.map((d) => px * (d.x + VIEW.originX) + py * (d.y + VIEW.originY) - v * t);
    for (const x of u) {
      const m = ((x % s) + s) % s;
      expect(Math.min(m, s - m)).toBeLessThan(0.01 * s);
    }
    // ... and the rows of dashes are one crest spacing apart: the smallest gap between rows.
    const rows = [...new Set(u.map((x) => Math.round(x * 100) / 100))].sort((p, q) => p - q);
    const gaps = rows.slice(1).map((x, i) => x - rows[i]!).filter((g) => g > 0.5);
    expect(Math.abs(Math.min(...gaps) - s) / s).toBeLessThan(0.03);
  });

  it("gives the 16 s rows four times the spacing of the 8 s rows", () => {
    const rowGap = (tp: number) => {
      const dashes = dashesOf(draw(VIEW, [family("combined", 2.5, tp, 270)], 1, false, UNBOUNDED).lines);
      const xs = [...new Set(dashes.map((d) => Math.round((d.x + VIEW.originX) * 100) / 100))].sort((p, q) => p - q);
      return Math.min(...xs.slice(1).map((x, i) => x - xs[i]!).filter((g) => g > 0.5));
    };
    expect(rowGap(16) / rowGap(8)).toBeCloseTo(4, 1);
  });
});

describe("size and opacity follow the height", () => {
  it("draws each dash with the stated length, width and opacity, all growing with Hs", () => {
    const stats = [1, 3, 5].map((hs) => {
      const { lines } = draw(VIEW, [family("combined", hs, 12, 270)], 3);
      const dashes = dashesOf(lines);
      expect(dashes.length).toBeGreaterThan(300);
      for (const d of dashes) {
        expect(d.length).toBeCloseTo(dashLength(hs, "combined"), 9);
        expect(d.width).toBe(nearestLevel(DASH_WIDTH_LEVELS, dashWidth(hs)));
        expect(d.alpha).toBeLessThanOrEqual(nearestLevel(DASH_ALPHA_LEVELS, dashAlpha(hs)) + 1e-9);
      }
      // Dashes in mid-life are at the full opacity for their height.
      expect(Math.max(...dashes.map((d) => d.alpha))).toBe(nearestLevel(DASH_ALPHA_LEVELS, dashAlpha(hs)));
      // Under every core: its soft halo stroke(s), wider and fainter, with round caps; the
      // trail strokes under them have butt caps (bands that end at the crest line).
      for (const pass of DASH_PASSES.slice(0, -1)) {
        expect(lines.some((l) => l.cap === "round" && Math.abs(l.width - dashes[0]!.width * pass.width) < 1e-9)).toBe(true);
      }
      const halo = new Set(DASH_WIDTH_LEVELS.flatMap((w) => DASH_PASSES.map((p) => w * p.width)));
      const band = new Set(TRAIL_LEVELS.flatMap((w) => TRAIL_STEPS.map((p) => w * p.length)));
      for (const l of lines) expect(l.cap === "round" ? halo.has(l.width) : l.cap === "butt" && band.has(l.width)).toBe(true);
      expect(trailsOf(lines).length).toBeGreaterThan(0);
      return { length: dashes[0]!.length, width: dashes[0]!.width, alpha: Math.max(...dashes.map((d) => d.alpha)) };
    });
    for (let i = 1; i < stats.length; i++) {
      expect(stats[i]!.length).toBeGreaterThan(stats[i - 1]!.length);
      expect(stats[i]!.width).toBeGreaterThan(stats[i - 1]!.width);
      expect(stats[i]!.alpha).toBeGreaterThan(stats[i - 1]!.alpha);
    }
  });

  it("keeps every dash shape at least twice as long as wide, so its orientation reads", () => {
    // Visible length of a round-capped stroke: its segment plus the core width.
    for (const hs of [0.5, 1, 2, 4, 8]) {
      for (const system of ["primary", "secondary", "windsea", "combined"] as const) {
        const { lines } = draw(VIEW, [family(system, hs, 12, 270)], 3);
        const segs = lines.filter((l) => l.pts.length === 2 && DASH_WIDTH_LEVELS.includes(l.width));
        expect(segs.length, `${system} at ${hs} m`).toBeGreaterThan(50);
        for (const l of segs) {
          const len = Math.hypot(l.pts[1]![0] - l.pts[0]![0], l.pts[1]![1] - l.pts[0]![1]);
          expect((len + l.width) / l.width, `${system} at ${hs} m`).toBeGreaterThanOrEqual(2);
        }
      }
    }
  });

  it("fades each dash in and out over its life (no dash pops in at full opacity)", () => {
    // One cell followed over its whole life: the opacity of its dash rises, holds and falls.
    const state = new DashState();
    const fam = [family("combined", 3, 10, 270)];
    const lattice = buildLattice(VIEW, fam);
    const alphas: number[] = [];
    for (let k = 0; k <= 75; k++) {
      const r = recorder();
      drawDashes(r.ctx, VIEW, lattice, { t: 20 + k / 10, still: false, budget: UNBOUNDED, maxVertices: Infinity, state });
      alphas.push(dashesOf(r.lines).filter((d) => d.alpha <= DASH_ALPHA_LEVELS[1]!).length);
    }
    // At any moment some dashes are faint (being born or fading out), not all at once.
    for (const n of alphas) expect(n).toBeGreaterThan(0);
  });
});

describe("motion trails: behind each dash, the band it swept over the last TRAIL_SECONDS", () => {
  const nearestTrail = (x: number) => TRAIL_LEVELS.reduce((best, v) => (Math.abs(v - x) < Math.abs(best - x) ? v : best), TRAIL_LEVELS[0]!);

  it.each([
    [8, 6],
    [10, 8],
    [16, 12],
  ])("draws each trail behind its dash, as wide as the dash, v x 1 s long (T = %i s: %i px)", (tp, expected) => {
    const s = crestSpacingPx(tp);
    const v = crestSpeedPxPerS(tp, s);
    const full = nearestTrail(trailLength(v, s));
    expect(full).toBe(expected); // 8 s: min(6.0, 6.0, 16); 10 s: 7.5 -> 8; 16 s: 12.0
    const hs = 3;
    const { lines, stats } = draw(VIEW, [family("combined", hs, tp, 270)], 9, false, UNBOUNDED);
    const dashes = dashesOf(lines);
    const trails = trailsOf(lines);
    expect(stats.trails).toBeGreaterThan(0.8 * stats.crests);
    expect(trails.length).toBe(TRAIL_STEPS.length * stats.trails);
    // (keys at 1e-6 px: two dashes of neighbouring cells can sit within 0.01 px of each other)
    const at = new Map(dashes.map((d) => [`${Math.round(d.x * 1e6)},${Math.round(d.y * 1e6)}`, d]));
    let atFull = 0;
    for (const l of trails) {
      const [[x0, y0], [x1, y1]] = l.pts as [[number, number], [number, number]];
      // Waves from the W travel east (+x): each trail step is a segment along the crest
      // (vertical), set back to the WEST of its dash by half its width, so the band runs from
      // the crest line back.
      expect(Math.abs(x1 - x0)).toBeLessThan(1e-9);
      const d = at.get(`${Math.round(((x0 + x1) / 2 + l.width / 2) * 1e6)},${Math.round(((y0 + y1) / 2) * 1e6)}`);
      expect(d, "a dash just east of the trail").toBeDefined();
      // As wide (along the crest) as the dash and its round caps, at a share of its opacity.
      expect(Math.abs(y1 - y0)).toBeCloseTo(dashLength(hs, "combined") + d!.width, 9);
      expect(TRAIL_STEPS.some((st) => Math.abs(alphaOf(l) - Math.round(d!.alpha * st.alpha * 1000) / 1000) < 1e-9)).toBe(true);
      // Never longer than v x TRAIL_SECONDS (a young dash's trail reaches back to its birth only).
      expect(l.width).toBeLessThanOrEqual(full + 1e-9);
      if (l.width === full) atFull++;
    }
    expect(atFull).toBeGreaterThan(0.5 * stats.trails);
    // Some dashes were born less than a second ago: their trails are shorter.
    expect(trails.some((l) => l.width < full * TRAIL_STEPS[TRAIL_STEPS.length - 1]!.length)).toBe(true);
  });

  it("fades backwards in its steps: the front half of the band is brighter than the back half", () => {
    // Opacity of the band at a distance behind the crest line, from the stacked steps.
    const at = (back: number) => 1 - TRAIL_STEPS.filter((st) => back < st.length).reduce((a, st) => a * (1 - st.alpha), 1);
    expect(at(0.25)).toBeCloseTo(0.4375, 9);
    expect(at(0.75)).toBeCloseTo(0.25, 9);
    expect(TRAIL_STEPS.map((st) => st.length)).toEqual([1, 0.5]);
  });

  it("draws full trails in the still frame (reduced motion) too", () => {
    const tp = 12;
    const s = crestSpacingPx(tp);
    const full = nearestTrail(trailLength(crestSpeedPxPerS(tp, s), s));
    const { lines, stats } = draw(VIEW, [family("combined", 3, tp, 180)], 0, true);
    // A swell from the S travels N (up the screen): its trails hang below (south of) its dashes.
    const dashes = dashesOf(lines);
    const trails = trailsOf(lines);
    const ys = new Set(trails.map((l) => Math.round((l.pts[0]![1] - l.width / 2) * 100)));
    for (const l of trails) expect([full, full * TRAIL_STEPS[1]!.length]).toContain(l.width);
    // Every dash has its trail, but where the trail would leave the lattice (at the bottom of the view).
    expect(stats.trails).toBeGreaterThan(0.95 * stats.crests);
    for (const d of dashes) if (!ys.has(Math.round(d.y * 100))) expect(d.y, `dash at ${d.x.toFixed(1)}, ${d.y.toFixed(1)} without a trail`).toBeGreaterThan(VIEW.height - full - 2 * LATTICE_PX);
  });
});

describe("masking: no dash on land or where the model has no data", () => {
  it("keeps every dash centre and both ends on valid lattice nodes", () => {
    // No data in 20°S..20°N, 40°W..40°E (a SYNTHETIC "continent"), plus a block of lattice
    // nodes flagged as land (as landAtNodes flags Natural Earth land).
    const hole = (lat: number, lon: number) => Math.abs(lat) <= 20 && Math.abs(lon) <= 40;
    const hs = globalGrid((lat, lon) => (hole(lat, lon) ? NaN : 2.5));
    const view = viewAt(3, 0, 0);
    const probe = buildLattice(view, []);
    const land = new Uint8Array(probe.nx * probe.ny);
    for (let j = 10; j < 40; j++) for (let i = 10; i < 50; i++) land[j * probe.nx + i] = 1;
    const { lines, stats, lattice } = draw(view, [family("combined", hs, 11, 250)], 5, false, UNBOUNDED, undefined, land);
    expect(stats.crests).toBeGreaterThan(100);
    const f = lattice.families[0]!;
    const nodeOk = (x: number, y: number) => {
      const i = Math.floor((x - lattice.x0) / LATTICE_PX);
      const j = Math.floor((y - lattice.y0) / LATTICE_PX);
      const k = j * lattice.nx + i;
      return !!(f.valid[k] && f.valid[k + 1] && f.valid[k + lattice.nx] && f.valid[k + lattice.nx + 1]);
    };
    for (const l of lines.filter((x) => x.pts.length === 2)) {
      const [[x0, y0], [x1, y1]] = l.pts as [[number, number], [number, number]];
      for (const [x, y] of [
        [x0, y0],
        [x1, y1],
        [(x0 + x1) / 2, (y0 + y1) / 2],
      ] as const) {
        expect(nodeOk(x, y), `dash point at ${x.toFixed(1)}, ${y.toFixed(1)}`).toBe(true);
        const lat = mercLat((view.originY + y) / view.worldPx);
        const lon = mercLon((view.originX + x) / view.worldPx);
        // within half a grid cell of the edge the bilinear sample still has data
        expect(Math.abs(lat) < 19 && Math.abs(lon) < 39, `dash at ${lat.toFixed(2)}, ${lon.toFixed(2)}`).toBe(false);
        expect(x > 10 * LATTICE_PX + lattice.x0 + 8 && x < 50 * LATTICE_PX + lattice.x0 - 8 && y > 10 * LATTICE_PX + lattice.y0 + 8 && y < 40 * LATTICE_PX + lattice.y0 - 8).toBe(false);
      }
    }
  });
});

describe("world-anchored, time-based and frame-rate independent", () => {
  /** Dash centres in world px inside a central window of the view. */
  const worldCentres = (v: PixelView, lines: ReturnType<typeof draw>["lines"], margin = 200) =>
    new Set(
      dashesOf(lines)
        .filter((d) => d.x > margin && d.x < v.width - margin && d.y > margin && d.y < v.height - margin)
        .map((d) => `${(d.x + v.originX).toFixed(3)},${(d.y + v.originY).toFixed(3)}`),
    );

  it("draws identical dashes at the same geographic places after a 100 px pan", () => {
    const fam = [family("secondary", 1.2, 9, 200), family("primary", 3, 12, 250)];
    const a = VIEW;
    const b = { ...a, originX: a.originX + 100, originY: a.originY - 40 };
    // Fresh state for each view, and one state carried across the pan: the same dashes.
    for (const shared of [false, true]) {
      const state = shared ? new DashState() : undefined;
      const pa = worldCentres(a, draw(a, fam, 5, false, UNBOUNDED, state).lines);
      const pb = worldCentres(b, draw(b, fam, 5, false, UNBOUNDED, state).lines);
      const both = [...pa].filter((k) => {
        const [x, y] = k.split(",").map(Number) as [number, number];
        return x - b.originX > 200 && x - b.originX < b.width - 200 && y - b.originY > 200 && y - b.originY < b.height - 200;
      });
      expect(both.length).toBeGreaterThan(400);
      expect(both.filter((k) => !pb.has(k))).toEqual([]);
    }
  });

  it("gives the same dashes at the same time whether drawn at 60 Hz or 120 Hz", { timeout: 30_000 }, () => {
    // A 6 s sea (dash life 4.5 s) over 6 s: every cell's dash is born again at least once.
    const view = viewAt(3, -40, -30, 480, 320);
    const fam = [family("combined", 2.5, 6, 240)];
    const lattice = buildLattice(view, fam);
    const key = (lines: ReturnType<typeof recorder>["lines"]) => JSON.stringify(dashesOf(lines).map((d) => [d.x.toFixed(6), d.y.toFixed(6), d.alpha]));
    const run = (hz: number) => {
      const state = new DashState();
      let last: ReturnType<typeof recorder>["lines"] = [];
      for (let k = 0; k <= 6 * hz; k++) {
        const r = recorder();
        drawDashes(r.ctx, view, lattice, { t: k / hz, still: false, budget: UNBOUNDED, maxVertices: Infinity, state });
        last = r.lines;
      }
      return key(last);
    };
    const fresh = recorder();
    drawDashes(fresh.ctx, view, lattice, { t: 6, still: false, budget: UNBOUNDED, maxVertices: Infinity });
    const at60 = run(60);
    expect(JSON.parse(at60).length).toBeGreaterThan(100);
    expect(run(120)).toBe(at60);
    expect(key(fresh.lines)).toBe(at60); // and the same as placing them from the clock alone
  });

  describe("cells that cannot draw in the view are skipped for the rest of their dash's life", () => {
    // SYNTHETIC seas whose period varies across the view (lives of different lengths), on a
    // view with land-like gaps, at a zoom where dashes born in the margin drift into view.
    const view = viewAt(3, -40, -30, 480, 320);
    const tp = globalGrid((_lat, lon) => 9 + 4 * Math.sin(lon / 4));
    const hs = globalGrid((lat, lon) => (Math.abs(lon + 30) < 3 && lat > -42 ? NaN : 2.5));
    const fam = [family("combined", hs, tp, 240)];
    const key = (lines: ReturnType<typeof recorder>["lines"]) => JSON.stringify(dashesOf(lines).map((d) => [d.x.toFixed(6), d.y.toFixed(6), d.alpha]));
    const animate = (skipIdle: boolean, still: (k: number) => boolean) => {
      const lattice = buildLattice(view, fam);
      const frame = (t: number, isStill: boolean, state?: DashState) => {
        const r = recorder();
        drawDashes(r.ctx, view, lattice, { t, still: isStill, budget: UNBOUNDED, maxVertices: Infinity, state, skipIdle });
        return r.lines;
      };
      const state = new DashState();
      const out: string[] = [];
      for (let k = 0; k <= 360; k++) {
        if (still(k)) expect(dashesOf(frame(0, true)).length).toBeGreaterThan(50);
        const lines = frame(k / 30, false, state);
        if (k % 15 === 0) out.push(key(lines));
      }
      return out;
    };

    it("draws exactly what visiting every cell every frame draws", { timeout: 60_000 }, () => {
      const every = animate(false, () => false);
      expect(JSON.parse(every[every.length - 1]!).length).toBeGreaterThan(50);
      expect(animate(true, () => false)).toEqual(every);
    });

    it("draws the same animation whether or not still frames (their own state) came in between", { timeout: 60_000 }, () => {
      expect(animate(true, (k) => k % 45 === 0)).toEqual(animate(true, () => false));
    });
  });

  it("advances the animation clock by time, not frames (60, 120 and 144 Hz)", () => {
    const rafs: Array<() => void> = [];
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => rafs.push(cb));
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    const clock = (hz: number) => {
      const anim = new WaveAnimator();
      anim.setFamilies([family("combined", 3, 12, 250)]);
      anim.start(recorder().ctx, VIEW, "animated");
      for (let k = 0; k <= hz; k++) anim.advance((k * 1000) / hz);
      anim.stop();
      return anim.time;
    };
    for (const hz of [60, 120, 144]) expect(clock(hz)).toBeCloseTo(1, 9);
  });

  it("keeps the live dashes when the fields change (the next forecast step)", () => {
    const state = new DashState();
    const before = dashesOf(draw(VIEW, [family("combined", 3, 12, 250)], 7, false, UNBOUNDED, state).lines);
    // Another step: higher and from another direction. Live dashes stay where they were (and
    // turn with the new field); only the cells whose dash ends meanwhile get a new one.
    const after = dashesOf(draw(VIEW, [family("combined", 4, 12, 240)], 7, false, UNBOUNDED, state).lines);
    const keys = (ds: Dash[]) => new Set(ds.map((d) => `${d.x.toFixed(3)},${d.y.toFixed(3)}`));
    const kb = keys(before);
    const kept = [...keys(after)].filter((k) => kb.has(k)).length;
    expect(kept).toBeGreaterThan(0.95 * before.length);
  });
});

describe("budget", () => {
  it("never draws more dashes than the budget, cuts steadily, and gives the top system its share", () => {
    const spec = [
      ["windsea", 1.5, 5, 300],
      ["secondary", 2, 9, 200],
      ["primary", 4, 12, 250],
    ] as const;
    const fam = spec.map(([system, hs, tp, dir]) => family(system, hs, tp, dir));
    const view = viewAt(4, -30, -30);
    const l = buildLattice(view, fam);
    const state = new DashState();
    // Where each dash of a frame should be one frame (1/30 s) later: moved along its system's
    // travel vector at its crest speed (uniform SYNTHETIC seas). Systems by dash colour.
    const motion = new Map<string, [number, number]>();
    const rgb = { windsea: "184, 240, 255", secondary: "255, 213, 154", primary: "255, 255, 255" } as const;
    for (const [system, , tp, dir] of spec) {
      const v = crestSpeedPxPerS(tp, crestSpacingPx(tp)) / 30;
      const [px, py] = propagation(dir)!;
      motion.set(rgb[system], [px * v, py * v]);
    }
    const key = (x: number, y: number) => `${Math.round(x * 100)},${Math.round(y * 100)}`;
    let prev: Dash[] = [];
    const counts: number[] = [];
    for (let k = 0; k < 90; k++) {
      const r = recorder();
      const st = drawDashes(r.ctx, view, l, { t: k / 30, still: false, budget: 1000, state });
      expect(st.crests).toBeLessThanOrEqual(1000);
      const now = dashesOf(r.lines);
      if (k >= 45) {
        counts.push(st.crests);
        // Steady: nearly every dash of this frame is a dash of the last frame moved on (the
        // rest were born or reached the end of their life meanwhile), so no cell flickers in
        // and out at the margin of the cut.
        const moved = new Set(prev.map((d) => key(d.x + motion.get(d.rgb)![0], d.y + motion.get(d.rgb)![1])));
        const near = (d: Dash) => [-1, 0, 1].some((ox) => [-1, 0, 1].some((oy) => moved.has(`${Math.round(d.x * 100) + ox},${Math.round(d.y * 100) + oy}`)));
        expect(now.filter(near).length / now.length).toBeGreaterThan(0.95);
      }
      prev = now;
      if (k === 89) {
        const white = now.filter((d) => d.rgb === rgb.primary).length; // primary
        expect(white).toBeGreaterThanOrEqual(0.55 * 1000);
        // The cut leaves out an even, hashed share of the cells, never a region: every tile of
        // a 4 x 4 split of the view keeps at least half of its fair share of the dashes.
        const tiles = new Array<number>(16).fill(0);
        const inView = now.filter((d) => d.x >= 0 && d.x < view.width && d.y >= 0 && d.y < view.height);
        for (const d of inView) tiles[Math.floor((d.y / view.height) * 4) * 4 + Math.floor((d.x / view.width) * 4)]! += 1;
        expect(Math.min(...tiles), tiles.join(",")).toBeGreaterThan((0.5 * inView.length) / 16);
      }
    }
    expect(Math.min(...counts)).toBeGreaterThan(900);
  });

  it("stays within the desktop and phone budgets on busy views", () => {
    const fam = [
      family("windsea", 1.8, 5, globalGrid((_lat, lon) => 60 + 20 * Math.sin((lon * Math.PI) / 30))),
      family("secondary", 2, 9, globalGrid((lat) => 190 + lat)),
      family("primary", 4, 12, 250),
    ];
    for (const z of [2, 4, 6]) {
      const desk = draw(viewAt(z, -30, -30), fam, 4);
      expect(desk.stats.crests).toBeLessThanOrEqual(MAX_DASHES);
      expect(desk.stats.families).toEqual(["windsea", "secondary", "primary"]);
      const phone = draw(viewAt(z, -30, -30, 412, 915), fam, 4);
      expect(phone.stats.crests).toBeLessThanOrEqual(PHONE_MAX_DASHES);
    }
    // Two vertices a dash (a split dash four) and two for each step of its trail: the strokes
    // of a batch share one path.
    const one = draw(VIEW, [family("combined", 3, 12, 250)], 4, false, UNBOUNDED);
    expect(one.stats.trails).toBeGreaterThan(0.8 * one.stats.crests);
    expect(one.stats.vertices).toBe(2 * one.stats.crests + 2 * TRAIL_STEPS.length * one.stats.trails);
  });

  it("never passes the vertex budget (MAX_FRAME_VERTICES a frame), shared between the systems like the dash budget", () => {
    const fam = [family("windsea", 1.8, 5, 300), family("secondary", 2, 9, 200), family("primary", 4, 12, 250)];
    for (const z of [2, 3, 4]) {
      for (const width of [1440, 2560]) {
        const { stats } = draw(viewAt(z, -30, -30, width, 1300), fam, 4);
        expect(stats.vertices, `zoom ${z}, ${width} px`).toBeLessThanOrEqual(MAX_FRAME_VERTICES);
        // Every system still gets its share (the top one is not the only one drawn).
        expect(stats.families).toEqual(["windsea", "secondary", "primary"]);
      }
    }
    // The cut is real: without it this view draws more.
    const busy = viewAt(2, -30, -30, 2560, 1300);
    expect(draw(busy, fam, 4, false, UNBOUNDED).stats.vertices).toBeGreaterThan(MAX_FRAME_VERTICES);
    // Each system in view leaves a share of the vertices to the next.
    const { lines } = draw(busy, fam, 4);
    for (const rgb of ["184, 240, 255", "255, 213, 154", "255, 255, 255"]) expect(lines.filter((l) => l.style.includes(rgb)).length, rgb).toBeGreaterThan(100);
  });

  it("keeps the same dash density at every zoom (one dash per cell fixed on screen)", () => {
    const density = (z: number) => draw(viewAt(z, -40, -30), [family("combined", 3, 12, 250)], 3, false, UNBOUNDED).stats.crests;
    const d3 = density(3);
    for (const z of [4, 5, 7]) expect(Math.abs(density(z) - d3) / d3).toBeLessThan(0.12);
  });
});

describe("reduced motion", () => {
  it("draws one still frame at full opacity, the same whatever the time, with arrowheads", () => {
    const fam = [family("combined", 3, 12, 180)];
    const a = draw(VIEW, fam, 0, true);
    const b = draw(VIEW, fam, 37, true);
    expect(a.stats.arrows).toBeGreaterThan(3);
    expect(a.stats.arrows).toBe(Math.ceil(a.stats.crests / ARROW_EVERY));
    expect(JSON.stringify(b.lines)).toBe(JSON.stringify(a.lines));
    // Full opacity for the height (no fade).
    for (const d of dashesOf(a.lines)) expect(d.alpha).toBe(nearestLevel(DASH_ALPHA_LEVELS, dashAlpha(3)));
    // Arrowheads point where the waves travel: a swell from the S goes N (up the screen).
    const heads = a.lines.filter((l) => l.style === "rgba(255, 255, 255, 0.85)" && l.pts.length === 3);
    expect(heads.length).toBe(a.stats.arrows);
    for (const { pts } of heads) {
      const [p, tip, q] = pts as [[number, number], [number, number], [number, number]];
      expect(tip[1] - (p[1] + q[1]) / 2).toBeLessThan(-4);
      expect(Math.abs(tip[0] - (p[0] + q[0]) / 2)).toBeLessThan(1e-6);
    }
  });

  it("puts no arrowheads on the dashes of the other partitions", () => {
    expect(draw(VIEW, [family("secondary", 2, 10, 200)], 0, true).stats.arrows).toBe(0);
    expect(draw(VIEW, [family("windsea", 1.5, 6, 200)], 0, true).stats.arrows).toBe(0);
    expect(draw(VIEW, [family("primary", 2, 12, 200)], 0, true).stats.arrows).toBeGreaterThan(3);
  });
});

describe("no data, no dashes", () => {
  it("draws nothing below 0.2 m or without a period or direction", () => {
    expect(draw(VIEW, [family("combined", 0.1, 12, 250)]).stats.crests).toBe(0);
    expect(draw(VIEW, [family("combined", 3, NaN, 250)]).stats.crests).toBe(0);
    expect(draw(VIEW, [family("combined", 3, 12, NaN)]).stats.crests).toBe(0);
    expect(draw(VIEW, [family("combined", constant(NaN), 12, 250)]).stats.crests).toBe(0);
  });
});
