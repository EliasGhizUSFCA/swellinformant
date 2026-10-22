import { describe, expect, it } from "vitest";

import {
  crestSpacingPx,
  crestSpeedPxPerS,
  crestTangent,
  DASH_CELL_PX,
  dashAlpha,
  dashFade,
  dashLength,
  dashLifeSeconds,
  dashWidth,
  drawnHeight,
  effectiveSystems,
  envelopeSpeedPxPerS,
  exaggeration,
  FAMILY_STYLES,
  formatFactor,
  groupSpeed,
  MAX_DASHES,
  MAX_FRAME_VERTICES,
  MAX_SPACING_PX,
  maxDashes,
  MIN_SPACING_PX,
  PHONE_MAX_DASHES,
  phaseSpeed,
  propagation,
  scaleStatement,
  TIME_SCALE,
  toward,
  TRAIL_MAX_PX,
  TRAIL_MAX_SHARE,
  TRAIL_SECONDS,
  TRAIL_STEPS,
  trailLength,
  wavelengthM,
  WAVE_SYSTEMS,
  type WaveSystem,
} from "@/lib/map-waves";

describe("wave directions (nautical degrees FROM -> screen propagation, x east, y down)", () => {
  it("turns FROM into TOWARD", () => {
    expect(toward(225)).toBe(45); // a SW swell travels NE
    expect(toward(0)).toBe(180);
    expect(toward(360)).toBe(180);
    expect(toward(90)).toBe(270);
    expect(toward(NaN)).toBeNaN();
  });

  it("gives the screen vector of the travel direction", () => {
    const sw = propagation(225)!;
    expect(sw[0]).toBeCloseTo(Math.SQRT1_2, 4); // east
    expect(sw[1]).toBeCloseTo(-Math.SQRT1_2, 4); // north is up the screen
    const w = propagation(270)!;
    expect(w[0]).toBeCloseTo(1, 12); // from the west: travels east
    expect(w[1]).toBeCloseTo(0, 12);
    const n = propagation(0)!;
    expect(n[0]).toBeCloseTo(0, 12);
    expect(n[1]).toBeCloseTo(1, 12); // from the north: travels down the screen
    expect(propagation(NaN)).toBeNull();
    expect(propagation(Infinity)).toBeNull();
    expect(crestTangent(NaN)).toBeNull();
  });

  it("draws crests perpendicular to the travel direction for every bearing", () => {
    for (let d = 0; d < 360; d++) {
      const p = propagation(d)!;
      const t = crestTangent(d)!;
      expect(Math.abs(p[0] * t[0] + p[1] * t[1])).toBeLessThan(1e-12);
      expect(Math.hypot(...t)).toBeCloseTo(1, 12);
    }
    // From the west the crests are vertical; from the SW they run NW-SE, along (0.707, 0.707).
    const w = crestTangent(270)!;
    expect(Math.abs(w[0])).toBeLessThan(1e-12);
    expect(Math.abs(w[1])).toBeCloseTo(1, 12);
    const sw = crestTangent(225)!;
    expect(sw[0]).toBeCloseTo(Math.SQRT1_2, 6);
    expect(sw[1]).toBeCloseTo(Math.SQRT1_2, 6);
  });
});

describe("deep-water physics and the stated scales", () => {
  it("uses L = g T^2 / (2 pi), c = L / T and cg = c / 2", () => {
    // g = 9.80665 m/s^2 (standard gravity)
    expect(wavelengthM(10)).toBeCloseTo(156.1, 1);
    expect(wavelengthM(16)).toBeCloseTo(399.56, 1);
    expect(phaseSpeed(10)).toBeCloseTo(wavelengthM(10) / 10, 10);
    expect(groupSpeed(10)).toBeCloseTo(phaseSpeed(10) / 2, 10);
  });

  it("spaces crests at 0.12 px per metre of wavelength, clamped to 8..64 px", () => {
    expect(crestSpacingPx(8)).toBeCloseTo(12.0, 1);
    expect(crestSpacingPx(10)).toBeCloseTo(18.7, 1);
    expect(Math.abs(crestSpacingPx(16) - 48.0)).toBeLessThan(0.1); // 47.95
    expect(crestSpacingPx(5)).toBe(MIN_SPACING_PX); // 4.7 px unclamped
    expect(crestSpacingPx(20)).toBe(MAX_SPACING_PX); // 74.9 px unclamped
    expect(crestSpacingPx(0)).toBeNaN();
    expect(crestSpacingPx(NaN)).toBeNaN();
    // Longer periods are never closer together.
    for (let t = 2; t < 25; t += 0.5) expect(crestSpacingPx(t + 0.5)).toBeGreaterThanOrEqual(crestSpacingPx(t));
  });

  it("draws true scale only when zoomed in beyond 1 / 0.12 m per px", () => {
    expect(crestSpacingPx(10, 1000)).toBeCloseTo(18.7, 1); // the scale S applies
    expect(crestSpacingPx(10, 2)).toBeCloseTo(wavelengthM(10) / 2, 6); // true wavelength, 78 px
    expect(crestSpacingPx(10, 0.1)).toBe(256); // capped
  });

  it("passes a crest every T / tau seconds, also when the spacing is clamped", () => {
    for (const t of [5, 8, 10, 12, 16, 20]) {
      const s = crestSpacingPx(t);
      const v = crestSpeedPxPerS(t, s);
      expect(s / v).toBeCloseTo(t / TIME_SCALE, 10);
      expect(envelopeSpeedPxPerS(t, s)).toBeCloseTo(v / 2, 10); // group speed: half the crest speed
    }
    expect(TIME_SCALE).toBe(4);
    // Unclamped, the crest speed is S x c x tau: 7.5 px/s for a 10 s swell.
    expect(crestSpeedPxPerS(10, crestSpacingPx(10))).toBeCloseTo(0.12 * phaseSpeed(10) * 4, 6);
    expect(crestSpeedPxPerS(0, 10)).toBeNaN();
  });

  it("states the spacing exaggeration per zoom and latitude", () => {
    expect(exaggeration(2, 0)).toBeGreaterThan(4695);
    expect(exaggeration(2, 0)).toBeLessThan(4697);
    expect(exaggeration(8, 0)).toBeCloseTo(73.4, 1);
    expect(exaggeration(2, 60)).toBeCloseTo(exaggeration(2, 0) / 2, 6); // Mercator scale at 60 deg
    expect(exaggeration(20, 0)).toBe(1); // true scale
    expect(scaleStatement(2, 0)).toBe("Crest spacing ×4,700 at the map centre; time runs 4× faster than real.");
    expect(scaleStatement(8, 0)).toContain("×73 ");
    expect(scaleStatement(20, 0)).toContain("true scale");
    expect([formatFactor(4696), formatFactor(73.4), formatFactor(1.64), formatFactor(NaN)]).toEqual(["4,700", "73", "1.6", "—"]);
  });
});

describe("height and drawing styles", () => {
  it("draws higher systems as longer, wider and brighter dashes, and very low ones not at all", () => {
    let l = 0;
    let w = 0;
    let a = 0;
    for (let hs = 0; hs <= 12; hs += 0.25) {
      expect(dashLength(hs, "combined")).toBeGreaterThanOrEqual(l);
      expect(dashWidth(hs)).toBeGreaterThanOrEqual(w);
      expect(dashAlpha(hs)).toBeGreaterThanOrEqual(a);
      l = dashLength(hs, "combined");
      w = dashWidth(hs);
      a = dashAlpha(hs);
    }
    for (const f of [dashWidth, dashAlpha]) expect(f(4)).toBeGreaterThan(f(1));
    expect(dashLength(4, "primary")).toBeGreaterThan(dashLength(1, "primary"));
    // The stated formulas and their limits (docs/map-rendering.md section 4.6).
    expect([dashLength(0, "primary"), dashLength(2, "primary"), dashLength(4, "primary"), dashLength(10, "primary")]).toEqual([6, 8, 11, 14]);
    expect([dashWidth(0), dashWidth(2), dashWidth(10)]).toEqual([1.8, 2.4, 3.4]);
    // D11's opacity: clamp(0.40 + 0.09 Hs, 0.40, 0.90) (the dashes without trails had 0.50 + 0.06 Hs).
    expect(dashAlpha(0)).toBe(0.4);
    expect(dashAlpha(2)).toBeCloseTo(0.58, 12);
    expect(dashAlpha(10)).toBe(0.9);
    // A long split dash for the secondary swell, a short thin one for the wind sea (shape, not colour alone).
    expect(dashLength(4, "secondary")).toBeCloseTo(1.4 * dashLength(4, "primary"), 10);
    expect(dashLength(4, "windsea")).toBeCloseTo(0.55 * dashLength(4, "primary"), 10);
    expect(dashWidth(4, "secondary")).toBeCloseTo(0.8 * dashWidth(4), 10);
    expect(dashWidth(4, "windsea")).toBeCloseTo(0.7 * dashWidth(4), 10);
    expect(dashWidth(4, "combined")).toBe(dashWidth(4));
    expect(drawnHeight(0.1)).toBe(false);
    expect(drawnHeight(0.19)).toBe(false);
    expect(drawnHeight(0.2)).toBe(true);
    expect(drawnHeight(NaN)).toBe(false);
  });

  it("lives three crest passages and fades in and out (a display choice)", () => {
    expect(dashLifeSeconds(10)).toBeCloseTo((3 * 10) / TIME_SCALE, 12); // 7.5 s
    expect(dashLifeSeconds(0)).toBeNaN();
    expect(dashFade(0)).toBe(0);
    expect(dashFade(0.1)).toBeCloseTo(0.5, 10); // half way through the fade-in (0-20 %)
    for (const age of [0.2, 0.45, 0.7]) expect(dashFade(age)).toBe(1);
    expect(dashFade(0.85)).toBeCloseTo(0.5, 10); // half way through the fade-out (70-100 %)
    expect(dashFade(1)).toBe(0);
  });

  it("keeps the same density at every zoom, with a budget per frame", () => {
    expect(DASH_CELL_PX).toEqual({ primary: 18, combined: 18, secondary: 18, windsea: 20 });
    expect(maxDashes(1440)).toBe(MAX_DASHES);
    expect(maxDashes(412)).toBe(PHONE_MAX_DASHES);
    expect([MAX_DASHES, PHONE_MAX_DASHES]).toEqual([4000, 1500]);
    expect(MAX_FRAME_VERTICES).toBe(6_000); // under D11's 12,000, for its 5 ms draw p95 (docs/map-rendering.md 4.8)
  });

  it("trails each dash by the way it moved over the last second of animation (a display choice)", () => {
    expect([TRAIL_SECONDS, TRAIL_MAX_SHARE, TRAIL_MAX_PX]).toEqual([1, 0.5, 16]);
    expect(TRAIL_STEPS).toEqual([
      { length: 1, alpha: 0.25 },
      { length: 0.5, alpha: 0.25 },
    ]);
    // v x 1 s: 7.5 px for a 10 s swell at the default spacing (18.7 px, 7.5 px/s) ...
    const s10 = crestSpacingPx(10);
    expect(trailLength(crestSpeedPxPerS(10, s10), s10)).toBeCloseTo(7.49, 2);
    const s16 = crestSpacingPx(16);
    expect(trailLength(crestSpeedPxPerS(16, s16), s16)).toBeCloseTo(11.99, 2);
    // ... at most half the crest spacing (a 4 s wind sea: 8 px spacing, 8 px/s) ...
    expect(trailLength(crestSpeedPxPerS(4, 8), 8)).toBe(4);
    // ... and 16 px (zoomed in to true scale, crests move fast) ...
    expect(trailLength(crestSpeedPxPerS(16, 256), 256)).toBe(16);
    // ... and none without a speed.
    expect(trailLength(0, 18)).toBe(0);
    expect(trailLength(NaN, 18)).toBe(0);
  });

  it("tells the systems apart by dash shape and colour, never colour alone", () => {
    const styles = WAVE_SYSTEMS.map((s) => FAMILY_STYLES[s]);
    const partitions = (["primary", "secondary", "windsea"] as const).map((s) => FAMILY_STYLES[s]);
    // Shape: solid, long split (a gap of 0.2 of the length) and short dashes.
    expect(new Set(partitions.map((s) => `${s.lengthFactor}/${s.gap}`)).size).toBe(3);
    expect(new Set(partitions.map((s) => s.color)).size).toBe(3);
    expect(FAMILY_STYLES.primary).toMatchObject({ gap: 0, lengthFactor: 1, widthFactor: 1, color: "#ffffff", trail: true });
    expect(FAMILY_STYLES.secondary).toMatchObject({ gap: 0.2, lengthFactor: 1.4, widthFactor: 0.8, color: "#ffd59a", trail: false });
    expect(FAMILY_STYLES.windsea).toMatchObject({ gap: 0, lengthFactor: 0.55, widthFactor: 0.7, color: "#b8f0ff", trail: true });
    expect(FAMILY_STYLES.combined).toMatchObject({ gap: 0, lengthFactor: 1, widthFactor: 1, color: "#ffffff", trail: true });
    expect(new Set(styles.map((s) => s.label)).size).toBe(4);
    expect(FAMILY_STYLES.combined.label).not.toMatch(/primary/i);
  });

  it("draws the chosen systems the run has, else the combined sea, never an absent one", () => {
    const all = new Set<WaveSystem>(["primary", "secondary", "windsea", "combined"]);
    expect(effectiveSystems(["primary", "secondary"], all)).toEqual(["secondary", "primary"]); // primary on top
    expect(effectiveSystems(["windsea", "primary"], all)).toEqual(["windsea", "primary"]);
    expect(effectiveSystems(["primary", "secondary"], new Set<WaveSystem>(["primary", "combined"]))).toEqual(["primary"]);
    expect(effectiveSystems(["secondary"], new Set<WaveSystem>(["primary", "combined"]))).toEqual(["combined"]);
    expect(effectiveSystems(["primary"], new Set<WaveSystem>(["combined"]))).toEqual(["combined"]);
    expect(effectiveSystems(["primary"], new Set<WaveSystem>())).toEqual([]);
  });
});
