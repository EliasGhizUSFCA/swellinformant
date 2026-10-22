import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ALPHA_STEP, buildLattice, DashState, drawDashes, drawnAlpha, LATTICE_PX, QuadBudget, TINT_LEVELS, WaveAnimator } from "@/components/map/wavefronts";
import { buildLut, colorAtValue, LAYER_SCALES, WAVE_HEIGHT_RAMP } from "@/lib/map-colors";
import { mercLat, mercLon, type PixelView } from "@/lib/map-geo";
import { fieldPositionAt } from "@/lib/map-render";
import {
  ARROW_EVERY,
  BLOB_HEAD_PX,
  BLOB_MAX_DEPTH_SHARE,
  BLOB_PROFILE,
  blobDepth,
  crestSpacingPx,
  crestSpeedPxPerS,
  dashAlpha,
  dashLength,
  dashTint,
  dashWidth,
  DRAW_TARGET_MS,
  MAX_DASHES,
  MAX_FRAME_QUADS,
  MIN_FRAME_QUADS,
  PHONE_MAX_DASHES,
  propagation,
  SMEAR_SECONDS,
} from "@/lib/map-waves";

import { blobsOf, constant, dashesOf, draw, family, globalGrid, installPath2D, orientation, recorder, UNBOUNDED, viewAt, type Dash } from "./map-dash-fixtures";

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

describe("orientation: dashes lie along the crests, across the direction of travel", () => {
  it("draws vertical dashes for a uniform sea from the west (270°), each within 1°", () => {
    const { quads, stats } = draw(VIEW, [family("combined", 3, 12, 270)], 2);
    const dashes = dashesOf(quads);
    expect(stats.crests).toBeGreaterThan(500);
    expect(dashes.length).toBe(stats.crests); // one blob per dash
    for (const d of dashes) expect(Math.abs(orientation(d) - 90)).toBeLessThan(1);
  });

  it("draws dashes at 45° (NW-SE, a quarter turn from the NE travel) for a sea from 225°, each within 1°", () => {
    const dashes = dashesOf(draw(VIEW, [family("primary", 3, 12, 225)], 2).quads);
    expect(dashes.length).toBeGreaterThan(500);
    for (const d of dashes) expect(Math.abs(orientation(d) - 45)).toBeLessThan(1);
  });

  it("smears each blob behind its crest line, against the travel (TOWARD = FROM + 180°)", () => {
    // From the west: travel east (+x), so the body hangs west of the leading edge; from 225°
    // (travel NE, (0.707, -0.707)) it hangs to the SW, (-0.707, 0.707).
    for (const [from, bx, by] of [
      [270, -1, 0],
      [225, -Math.SQRT1_2, Math.SQRT1_2],
      [180, 0, 1],
    ] as const) {
      const blobs = blobsOf(draw(VIEW, [family("combined", 3, 12, from)], 2).quads);
      expect(blobs.length).toBeGreaterThan(500);
      for (const d of blobs) {
        expect(d.bx).toBeCloseTo(bx, 6);
        expect(d.by).toBeCloseTo(by, 6);
        // ... and the crest direction is perpendicular to it.
        expect(Math.abs(d.ux * d.bx + d.uy * d.by)).toBeLessThan(1e-9);
      }
    }
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
      drawDashes(r.ctx, VIEW, lattice, { t, still: false, budget: UNBOUNDED, maxQuads: Infinity, state });
      return dashesOf(r.quads);
    };
    at(3.0);
    const a = at(3.1);
    const dt = 0.25;
    const b = at(3.1 + dt);
    // How many dashes of frame a have a dash of frame b at their place moved by k x v dt
    // along a direction (within 0.01 px). Dashes are on average 14 px apart; the few born or
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

  it.each([8, 10, 16])("puts dash leading edges on crest lines one spacing apart (T = %i s, spacing within 3 %)", (tp) => {
    const s = crestSpacingPx(tp);
    const v = crestSpeedPxPerS(tp, s);
    const t = 4.2;
    const [px, py] = propagation(270)!;
    const dashes = dashesOf(draw(VIEW, [family("combined", 2.5, tp, 270)], t, false, UNBOUNDED).quads);
    expect(dashes.length).toBeGreaterThan(300);
    // Every leading edge's phase p . W - v t is a multiple of s (W in world px) ...
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
      const dashes = dashesOf(draw(VIEW, [family("combined", 2.5, tp, 270)], 1, false, UNBOUNDED).quads);
      const xs = [...new Set(dashes.map((d) => Math.round((d.x + VIEW.originX) * 100) / 100))].sort((p, q) => p - q);
      return Math.min(...xs.slice(1).map((x, i) => x - xs[i]!).filter((g) => g > 0.5));
    };
    expect(rowGap(16) / rowGap(8)).toBeCloseTo(4, 1);
  });
});

describe("size and opacity follow the height", () => {
  it("draws each dash with the stated length, depth and opacity, all growing with Hs", () => {
    const tp = 12;
    const s = crestSpacingPx(tp);
    const v = crestSpeedPxPerS(tp, s);
    const stats = [1, 3, 5].map((hs) => {
      const { quads } = draw(VIEW, [family("combined", hs, tp, 270)], 3);
      const dashes = blobsOf(quads);
      expect(dashes.length).toBeGreaterThan(300);
      const full = blobDepth(hs, "combined", v, s);
      for (const d of dashes) {
        expect(d.length).toBeCloseTo(dashLength(hs, "combined"), 9);
        // Never deeper than the stated depth; at least the bright head.
        expect(d.depth).toBeLessThanOrEqual(full + 1e-9);
        expect(d.depth).toBeGreaterThanOrEqual(BLOB_HEAD_PX - 1e-9);
        // (opacities are drawn rounded to ALPHA_STEP, so a batch sets its opacity once)
        expect(d.alpha).toBeLessThanOrEqual(drawnAlpha(dashAlpha(hs)) + 1e-9);
        expect(Math.abs(d.alpha / ALPHA_STEP - Math.round(d.alpha / ALPHA_STEP))).toBeLessThan(1e-9);
        // One gradient along the blob's own depth (0 = the leading edge, 1 = the back) with the stated profile.
        expect(d.gradient!.line).toEqual([0, 0, 0, 1]);
        expect(d.gradient!.stops.map((st) => st.at)).toEqual(BLOB_PROFILE.map((p) => p.at));
      }
      // Dashes in mid-life are at the full opacity for their height, and most are at full depth.
      expect(Math.max(...dashes.map((d) => d.alpha))).toBeCloseTo(drawnAlpha(dashAlpha(hs)), 9);
      expect(Math.abs(drawnAlpha(dashAlpha(hs)) - dashAlpha(hs))).toBeLessThanOrEqual(ALPHA_STEP / 2 + 1e-12);
      expect(dashes.filter((d) => Math.abs(d.depth - full) < 1e-9).length).toBeGreaterThan(0.5 * dashes.length);
      return { length: dashes[0]!.length, depth: full, alpha: Math.max(...dashes.map((d) => d.alpha)) };
    });
    for (let i = 1; i < stats.length; i++) {
      expect(stats[i]!.length).toBeGreaterThan(stats[i - 1]!.length);
      expect(stats[i]!.depth).toBeGreaterThanOrEqual(stats[i - 1]!.depth);
      expect(stats[i]!.alpha).toBeGreaterThan(stats[i - 1]!.alpha);
    }
    expect(stats[2]!.depth).toBeGreaterThan(stats[0]!.depth);
  });

  it("keeps every blob at least 1.5 times as long as deep and every bar piece twice as long as wide, so its orientation reads", () => {
    const view = viewAt(3, -40, -30, 480, 320);
    for (const hs of [0.5, 1, 2, 4, 8]) {
      for (const system of ["primary", "secondary", "windsea", "combined"] as const) {
        for (const tp of [6, 12, 18]) {
          const dashes = dashesOf(draw(view, [family(system, hs, tp, 270)], 3, true).quads);
          expect(dashes.length, `${system} at ${hs} m, ${tp} s`).toBeGreaterThan(50);
          // (A split bar: each piece either side of its gap.)
          for (const d of dashes) expect((d.length * (d.kind === "blob" ? 1 : (1 - d.gap) / 2)) / d.depth, `${system} at ${hs} m, ${tp} s`).toBeGreaterThanOrEqual((d.kind === "blob" ? 1.5 : 2) - 1e-9);
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
      drawDashes(r.ctx, VIEW, lattice, { t: 20 + k / 10, still: false, budget: UNBOUNDED, maxQuads: Infinity, state });
      alphas.push(dashesOf(r.quads).filter((d) => d.alpha <= 0.18).length);
    }
    // At any moment some dashes are faint (being born or fading out), not all at once.
    for (const n of alphas) expect(n).toBeGreaterThan(0);
  });
});

describe("the smear: behind each blob, the band its crest swept over the last SMEAR_SECONDS", () => {
  it.each([
    [8, 4.5],
    [10, 5.25],
    [16, 6.6],
  ])("is BLOB_HEAD_PX + v x SMEAR_SECONDS deep, capped at a share of the length and half the spacing (T = %i s: %f px at 5 m)", (tp, expected) => {
    const s = crestSpacingPx(tp);
    const v = crestSpeedPxPerS(tp, s);
    const hs = 5; // 10 px long: the speed term binds below 16 s, the share of the length (6.6 px) at 16 s
    const full = blobDepth(hs, "combined", v, s);
    expect(full).toBeCloseTo(Math.min(BLOB_HEAD_PX + v * SMEAR_SECONDS, 0.5 * s, BLOB_MAX_DEPTH_SHARE * dashLength(hs, "combined")), 12);
    expect(full).toBeCloseTo(expected, 1);
    const { quads, stats } = draw(VIEW, [family("combined", hs, tp, 270)], 9, false, UNBOUNDED);
    const blobs = blobsOf(quads);
    expect(stats.trails).toBeGreaterThan(0.8 * stats.crests);
    let atFull = 0;
    for (const d of blobs) {
      // Waves from the W travel east (+x): the body runs west of the leading edge.
      expect(d.bx).toBeCloseTo(-1, 9);
      expect(d.depth).toBeLessThanOrEqual(full + 1e-9);
      if (Math.abs(d.depth - full) < 1e-9) atFull++;
    }
    expect(atFull).toBeGreaterThan(0.5 * stats.crests);
    // Some dashes were born less than SMEAR_SECONDS ago: their smears are shorter (they reach back to the birth only).
    expect(blobs.some((d) => d.depth < full - 0.5)).toBe(true);
  });

  it("fades backwards: a bright leading edge, then a falloff to nothing at the back", () => {
    const peak = BLOB_PROFILE.findIndex((p) => p.alpha === 1);
    expect(BLOB_PROFILE[0]!.at).toBe(0);
    expect(BLOB_PROFILE.at(-1)!).toEqual({ at: 1, alpha: 0 });
    expect(BLOB_PROFILE[peak]!.at).toBeLessThanOrEqual(0.1);
    for (let i = peak + 1; i < BLOB_PROFILE.length; i++) expect(BLOB_PROFILE[i]!.alpha).toBeLessThan(BLOB_PROFILE[i - 1]!.alpha);
    // The front half carries more than the back half.
    const mean = (a: number, b: number) => {
      let sum = 0;
      for (let k = 0; k < 100; k++) {
        const y = a + ((k + 0.5) / 100) * (b - a);
        const i = BLOB_PROFILE.findIndex((p) => p.at >= y);
        const lo = BLOB_PROFILE[i - 1]!;
        const hi = BLOB_PROFILE[i]!;
        sum += lo.alpha + ((hi.alpha - lo.alpha) * (y - lo.at)) / (hi.at - lo.at);
      }
      return sum / 100;
    };
    expect(mean(0, 0.5)).toBeGreaterThan(1.5 * mean(0.5, 1));
  });

  it("draws full smears in the still frame (reduced motion) too", () => {
    const tp = 12;
    const s = crestSpacingPx(tp);
    const full = blobDepth(3, "combined", crestSpeedPxPerS(tp, s), s);
    const { quads, stats } = draw(VIEW, [family("combined", 3, tp, 180)], 0, true);
    // A swell from the S travels N (up the screen): its smears hang below (south of) its leading edges.
    const blobs = blobsOf(quads);
    for (const d of blobs) expect(d.by).toBeCloseTo(1, 9);
    // Every blob is at full depth, but where its back would leave the lattice (at the bottom of the view).
    expect(stats.trails).toBeGreaterThan(0.95 * stats.crests);
    for (const d of blobs) if (Math.abs(d.depth - full) > 1e-9) expect(d.y, `blob at ${d.x.toFixed(1)}, ${d.y.toFixed(1)} without its smear`).toBeGreaterThan(VIEW.height - full - 2 * LATTICE_PX);
  });
});

describe("tint: a lighter tint of the colour field under each dash", () => {
  // The Wave Height colour field of a SYNTHETIC sea whose height rises from 1 m in the west of
  // the view to 6 m in the east.
  const hsGrid = globalGrid((_lat, lon) => 1 + 5 * Math.min(1, Math.max(0, (lon + 60) / 60)));
  const scale = LAYER_SCALES.hs;
  const lut = buildLut(scale.stops);
  const tint = { position: (lat: number, lon: number) => fieldPositionAt({ grid: hsGrid, v: null, scale, minWeight: 0.5 }, lat, lon), lut };

  it("lifts every channel of the field colour (never pure white), and mixes in a partition's own colour", () => {
    for (const m of [0.5, 1, 2, 3, 5, 8]) {
      const field = colorAtValue(WAVE_HEIGHT_RAMP, m);
      const [r, g, b] = dashTint(field, "combined");
      for (const [c, f] of [r, g, b].map((x, i) => [x, field[i]!] as const)) {
        // Lifted by TINT_LIFT, keeping TINT_HEADROOM of the way to 255.
        expect(c).toBe(Math.round(Math.min(f + 150, 255 - 0.1 * (255 - f))));
        expect(c).toBeGreaterThan(f);
        expect(c).toBeLessThan(255);
      }
      expect([r, g, b], `${m} m`).not.toEqual([255, 255, 255]);
      // Partitions: 75 % their own colour (amber secondary, cyan wind sea), so they stay apart by colour too.
      const sec = dashTint(field, "secondary");
      const wind = dashTint(field, "windsea");
      expect(sec[0]).toBeGreaterThan(sec[2]); // amber: more red than blue
      expect(wind[2]).toBeGreaterThan(wind[0]); // cyan: more blue than red
    }
    expect(dashTint(null, "combined")).toEqual([255, 255, 255]);
  });

  it("takes each dash's tint from the field under it", () => {
    const view = viewAt(3, -40, -30);
    const { quads, lattice } = draw(view, [family("combined", hsGrid, 12, 270)], 2, false, UNBOUNDED, undefined, undefined, tint);
    expect(lattice.tintRgb).toHaveLength(TINT_LEVELS);
    const blobs = blobsOf(quads);
    expect(blobs.length).toBeGreaterThan(500);
    const lum = (d: Dash) => d.rgb.split(", ").reduce((a, c) => a + Number(c), 0);
    let checked = 0;
    for (const d of blobs) {
      const lat = mercLat((view.originY + d.y) / view.worldPx);
      const lon = mercLon((view.originX + d.x) / view.worldPx);
      // The tint level's field colour is within one level (and a lattice cell) of the field at the dash.
      const level = Math.min(TINT_LEVELS - 1, Math.floor(tint.position(lat, lon) * TINT_LEVELS));
      const near = [level - 1, level, level + 1].filter((k) => k >= 0 && k < TINT_LEVELS).map((k) => dashTint(lattice.tintRgb![k]!, "combined").join(", "));
      expect(near, `dash at ${lon.toFixed(2)}°`).toContain(d.rgb);
      checked++;
    }
    expect(checked).toBe(blobs.length);
    // Higher seas, lighter field, lighter dashes: the east of the view is lighter than the west.
    const west = blobs.filter((d) => d.x < 300);
    const east = blobs.filter((d) => d.x > 1100);
    expect(Math.min(...east.map(lum))).toBeGreaterThan(Math.max(...west.map(lum)));
  });
});

describe("masking: no dash on land or where the model has no data", () => {
  it("keeps every dash's leading edge, ends and back corners on valid lattice nodes", () => {
    // No data in 20°S..20°N, 40°W..40°E (a SYNTHETIC "continent"), plus a block of lattice
    // nodes flagged as land (as landAtNodes flags Natural Earth land).
    const hole = (lat: number, lon: number) => Math.abs(lat) <= 20 && Math.abs(lon) <= 40;
    const hs = globalGrid((lat, lon) => (hole(lat, lon) ? NaN : 2.5));
    const view = viewAt(3, 0, 0);
    const probe = buildLattice(view, []);
    const land = new Uint8Array(probe.nx * probe.ny);
    for (let j = 10; j < 40; j++) for (let i = 10; i < 50; i++) land[j * probe.nx + i] = 1;
    const { quads, stats, lattice } = draw(view, [family("combined", hs, 11, 250)], 5, false, UNBOUNDED, undefined, land);
    expect(stats.crests).toBeGreaterThan(100);
    const f = lattice.families[0]!;
    const nodeOk = (x: number, y: number) => {
      const i = Math.floor((x - lattice.x0) / LATTICE_PX);
      const j = Math.floor((y - lattice.y0) / LATTICE_PX);
      const k = j * lattice.nx + i;
      return !!(f.valid[k] && f.valid[k + 1] && f.valid[k + lattice.nx] && f.valid[k + lattice.nx + 1]);
    };
    for (const d of dashesOf(quads)) {
      const hx = (d.ux * d.length) / 2;
      const hy = (d.uy * d.length) / 2;
      const back = d.kind === "blob" ? d.depth : 0;
      for (const [x, y] of [
        [d.x, d.y],
        [d.x - hx, d.y - hy],
        [d.x + hx, d.y + hy],
        [d.x - hx + d.bx * back, d.y - hy + d.by * back],
        [d.x + hx + d.bx * back, d.y + hy + d.by * back],
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
  /** Dash leading edges in world px inside a central window of the view. */
  const worldCentres = (v: PixelView, quads: ReturnType<typeof draw>["quads"], margin = 200) =>
    new Set(
      dashesOf(quads)
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
      const pa = worldCentres(a, draw(a, fam, 5, false, UNBOUNDED, state).quads);
      const pb = worldCentres(b, draw(b, fam, 5, false, UNBOUNDED, state).quads);
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
    const key = (quads: ReturnType<typeof recorder>["quads"]) => JSON.stringify(dashesOf(quads).map((d) => [d.x.toFixed(6), d.y.toFixed(6), d.alpha.toFixed(6), d.depth.toFixed(6)]));
    const run = (hz: number) => {
      const state = new DashState();
      let last: ReturnType<typeof recorder>["quads"] = [];
      for (let k = 0; k <= 6 * hz; k++) {
        const r = recorder();
        drawDashes(r.ctx, view, lattice, { t: k / hz, still: false, budget: UNBOUNDED, maxQuads: Infinity, state });
        last = r.quads;
      }
      return key(last);
    };
    const fresh = recorder();
    drawDashes(fresh.ctx, view, lattice, { t: 6, still: false, budget: UNBOUNDED, maxQuads: Infinity });
    const at60 = run(60);
    expect(JSON.parse(at60).length).toBeGreaterThan(100);
    expect(run(120)).toBe(at60);
    expect(key(fresh.quads)).toBe(at60); // and the same as placing them from the clock alone
  });

  describe("cells that cannot draw in the view are skipped for the rest of their dash's life", () => {
    // SYNTHETIC seas whose period varies across the view (lives of different lengths), on a
    // view with land-like gaps, at a zoom where dashes born in the margin drift into view.
    const view = viewAt(3, -40, -30, 480, 320);
    const tp = globalGrid((_lat, lon) => 9 + 4 * Math.sin(lon / 4));
    const hs = globalGrid((lat, lon) => (Math.abs(lon + 30) < 3 && lat > -42 ? NaN : 2.5));
    const fam = [family("combined", hs, tp, 240)];
    const key = (quads: ReturnType<typeof recorder>["quads"]) => JSON.stringify(dashesOf(quads).map((d) => [d.x.toFixed(6), d.y.toFixed(6), d.alpha.toFixed(6), d.depth.toFixed(6)]));
    const animate = (skipIdle: boolean, still: (k: number) => boolean) => {
      const lattice = buildLattice(view, fam);
      const frame = (t: number, isStill: boolean, state?: DashState) => {
        const r = recorder();
        drawDashes(r.ctx, view, lattice, { t, still: isStill, budget: UNBOUNDED, maxQuads: Infinity, state, skipIdle });
        return r.quads;
      };
      const state = new DashState();
      const out: string[] = [];
      for (let k = 0; k <= 360; k++) {
        if (still(k)) expect(dashesOf(frame(0, true)).length).toBeGreaterThan(50);
        const quads = frame(k / 30, false, state);
        if (k % 15 === 0) out.push(key(quads));
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
    const before = dashesOf(draw(VIEW, [family("combined", 3, 12, 250)], 7, false, UNBOUNDED, state).quads);
    // Another step: higher and from another direction. Live dashes stay where they were (and
    // turn with the new field); only the cells whose dash ends meanwhile get a new one.
    const after = dashesOf(draw(VIEW, [family("combined", 4, 12, 240)], 7, false, UNBOUNDED, state).quads);
    const keys = (ds: Dash[]) => new Set(ds.map((d) => `${d.x.toFixed(3)},${d.y.toFixed(3)}`));
    const kb = keys(before);
    const kept = [...keys(after)].filter((k) => kb.has(k)).length;
    expect(kept).toBeGreaterThan(0.95 * before.length);
  });
});

describe("budget", () => {
  /** The systems' own colours (no colour field under them in these tests). */
  const RGB = { windsea: "156, 236, 255", secondary: "255, 207, 133", primary: "255, 255, 255" } as const;

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
    for (const [system, , tp, dir] of spec) {
      const v = crestSpeedPxPerS(tp, crestSpacingPx(tp)) / 30;
      const [px, py] = propagation(dir)!;
      motion.set(RGB[system], [px * v, py * v]);
    }
    const key = (x: number, y: number) => `${Math.round(x * 100)},${Math.round(y * 100)}`;
    let prev: Dash[] = [];
    const counts: number[] = [];
    for (let k = 0; k < 90; k++) {
      const r = recorder();
      const st = drawDashes(r.ctx, view, l, { t: k / 30, still: false, budget: 1000, state });
      expect(st.crests).toBeLessThanOrEqual(1000);
      const now = dashesOf(r.quads); // one quad per dash
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
        const white = now.filter((d) => d.rgb === RGB.primary).length; // primary
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
    // One filled quad a dash, a blob or a split bar (no path vertices).
    const one = draw(VIEW, [family("combined", 3, 12, 250)], 4, false, UNBOUNDED);
    expect(one.stats.trails).toBeGreaterThan(0.8 * one.stats.crests);
    expect(one.stats.quads).toBe(one.stats.crests);
    expect(one.quads.length).toBe(one.stats.quads);
    expect(one.stats.vertices).toBe(0);
    const split = draw(VIEW, [family("secondary", 2, 10, 250)], 4, false, UNBOUNDED);
    expect(split.stats.quads).toBe(split.stats.crests);
    for (const d of dashesOf(split.quads)) {
      expect(d.depth).toBeCloseTo(dashWidth(2, "secondary"), 9);
      expect(d.gap).toBeCloseTo(0.2, 9);
    }
  });

  it("never passes the quad budget (MAX_FRAME_QUADS a frame), shared between the systems like the dash budget", () => {
    const fam = [family("windsea", 1.8, 5, 300), family("secondary", 2, 9, 200), family("primary", 4, 12, 250)];
    for (const z of [2, 3, 4]) {
      for (const width of [1440, 2560]) {
        const { stats } = draw(viewAt(z, -30, -30, width, 1300), fam, 4);
        expect(stats.quads, `zoom ${z}, ${width} px`).toBeLessThanOrEqual(MAX_FRAME_QUADS);
        // Every system still gets its share (the top one is not the only one drawn).
        expect(stats.families).toEqual(["windsea", "secondary", "primary"]);
      }
    }
    // The cut is real: without it this view draws more.
    const busy = viewAt(2, -30, -30, 2560, 1300);
    expect(draw(busy, fam, 4, false, UNBOUNDED).stats.quads).toBeGreaterThan(MAX_FRAME_QUADS);
    // Each system in view leaves a share of the quads to the next.
    const { quads } = draw(busy, fam, 4);
    for (const rgb of Object.values(RGB)) expect(dashesOf(quads).filter((d) => d.rgb === rgb).length, rgb).toBeGreaterThan(100);
  });

  it("keeps the same dash density at every zoom (one dash per cell fixed on screen)", () => {
    const density = (z: number) => draw(viewAt(z, -40, -30), [family("combined", 3, 12, 250)], 3, false, UNBOUNDED).stats.crests;
    const d3 = density(3);
    for (const z of [4, 5, 7]) expect(Math.abs(density(z) - d3) / d3).toBeLessThan(0.12);
  });
});

describe("device-adaptive quad budget (D11's 5 ms frame draw p95)", () => {
  it("starts at MAX_FRAME_QUADS and stays there while frames draw fast", () => {
    const b = new QuadBudget();
    expect(b.cap).toBe(MAX_FRAME_QUADS);
    for (let k = 0; k < 200; k++) b.record(1.2, b.cap);
    expect(b.cap).toBe(MAX_FRAME_QUADS);
  });

  it("thins the frame in proportion when its draw time passes the target, never below MIN_FRAME_QUADS", () => {
    const b = new QuadBudget();
    // A slow device: draw time proportional to the quads (1 ms per 1,000).
    for (let k = 0; k < 400; k++) b.record(b.cap / 1000, b.cap);
    expect(b.cap).toBeLessThanOrEqual(DRAW_TARGET_MS * 1000);
    expect(b.cap).toBeGreaterThan(0.7 * DRAW_TARGET_MS * 1000);
    // A very slow one stops at the floor.
    const slow = new QuadBudget();
    for (let k = 0; k < 400; k++) slow.record(slow.cap / 100, slow.cap);
    expect(slow.cap).toBe(MIN_FRAME_QUADS);
  });

  it("cuts to what slow frames drew, also when the cap did not limit them", () => {
    const b = new QuadBudget();
    // 3,000 quads drawn in 4.5 ms (a busy view on a slow machine, under the 7,000 cap).
    for (let k = 0; k < 20; k++) b.record(4.5, 3000);
    expect(b.cap).toBe(Math.floor((3000 * 0.95 * DRAW_TARGET_MS) / 4.5));
    expect(b.cap).toBeLessThan(3000);
    // Frames just under the target leave it there (nothing says a fuller frame would fit).
    for (let k = 0; k < 100; k++) b.record(0.9 * DRAW_TARGET_MS, b.cap);
    expect(b.cap).toBe(Math.floor((3000 * 0.95 * DRAW_TARGET_MS) / 4.5));
  });

  it("grows back when the frames get cheap again, and ignores frames the budget did not limit", () => {
    const b = new QuadBudget();
    for (let k = 0; k < 400; k++) b.record(b.cap / 500, b.cap);
    const low = b.cap;
    expect(low).toBeLessThan(MAX_FRAME_QUADS);
    // A quiet view far under the cap says nothing about the cost of a full one.
    for (let k = 0; k < 400; k++) b.record(0.2, 100);
    expect(b.cap).toBe(low);
    for (let k = 0; k < 400; k++) b.record(b.cap / 4000, b.cap);
    expect(b.cap).toBe(MAX_FRAME_QUADS);
  });
});

describe("device-adaptive quad budget in the animator", () => {
  it("thins a slow device's frames and reports the cap each frame was drawn under", () => {
    vi.stubGlobal("requestAnimationFrame", () => 0);
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    // A slow device: every call of performance.now advances 4 ms, so a frame takes 4 ms to draw.
    let clock = 0;
    vi.spyOn(performance, "now").mockImplementation(() => (clock += 4));
    const anim = new WaveAnimator();
    const seen: Array<{ quads: number; cap: number }> = [];
    anim.setListener((st) => seen.push({ quads: st.quads, cap: st.quadCap }));
    anim.setFamilies([family("combined", 3, 12, 250)]);
    anim.start(recorder().ctx, viewAt(2, -30, -30, 2560, 1300), "animated");
    for (let k = 1; k <= 120; k++) anim.advance(k * 40);
    anim.stop();
    expect(seen.length).toBeGreaterThan(100);
    expect(seen[0]!.cap).toBe(MAX_FRAME_QUADS);
    expect(seen.at(-1)!.cap).toBeLessThan(MAX_FRAME_QUADS);
    // Never more quads than the cap the frame was drawn under, also on the frame that cut it.
    for (const f of seen) expect(f.quads).toBeLessThanOrEqual(f.cap);
  });
});

describe("reduced motion", () => {
  it("draws one still frame at full opacity, the same whatever the time, with arrowheads", () => {
    const fam = [family("combined", 3, 12, 180)];
    const a = draw(VIEW, fam, 0, true);
    const b = draw(VIEW, fam, 37, true);
    expect(a.stats.arrows).toBeGreaterThan(3);
    expect(a.stats.arrows).toBe(Math.ceil(a.stats.crests / ARROW_EVERY));
    expect(JSON.stringify(b.quads)).toBe(JSON.stringify(a.quads));
    expect(JSON.stringify(b.lines)).toBe(JSON.stringify(a.lines));
    // Full opacity for the height (no fade).
    for (const d of dashesOf(a.quads)) expect(d.alpha).toBeCloseTo(drawnAlpha(dashAlpha(3)), 9);
    // Arrowheads point where the waves travel: a swell from the S goes N (up the screen).
    const heads = a.lines.filter((l) => l.style === "rgba(255, 255, 255, 0.85)" && l.pts.length === 3);
    expect(heads.length).toBe(a.stats.arrows);
    expect(a.stats.vertices).toBe(3 * a.stats.arrows);
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
