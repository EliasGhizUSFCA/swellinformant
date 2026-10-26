import { describe, expect, it } from "vitest";

import {
  BLOB_HEAD_PX,
  BLOB_MAX_DEPTH_SHARE,
  blobDepth,
  crestSpacingPx,
  crestSpeedPxPerS,
  DASH_CELL_PX,
  dashAlpha,
  dashFade,
  dashLength,
  dashLifeSeconds,
  dashTint,
  dashWidth,
  DRAW_TARGET_MS,
  effectiveSystems,
  exaggeration,
  FAMILY_STYLES,
  formatFactor,
  MAX_DASHES,
  MAX_FRAME_QUADS,
  MAX_SPACING_PX,
  maxDashes,
  MIN_FRAME_QUADS,
  MIN_SPACING_PX,
  PHONE_MAX_DASHES,
  scaleStatement,
  SMEAR_MAX_SHARE,
  SMEAR_SECONDS,
  TIME_SCALE,
  TINT_HEADROOM,
  TINT_LIFT,
  wavelengthM,
  WAVE_SYSTEMS,
  type WaveSystem,
} from "@/lib/map-waves";

describe("deep-water physics and the stated scales", () => {
  it("uses L = g T^2 / (2 pi)", () => {
    // g = 9.80665 m/s^2 (standard gravity)
    expect(wavelengthM(10)).toBeCloseTo(156.1, 1);
    expect(wavelengthM(16)).toBeCloseTo(399.56, 1);
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
    }
    expect(TIME_SCALE).toBe(4);
    // Unclamped, the crest speed is S x c x tau (c = L / T): 7.5 px/s for a 10 s swell.
    expect(crestSpeedPxPerS(10, crestSpacingPx(10))).toBeCloseTo(0.12 * (wavelengthM(10) / 10) * 4, 6);
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
  it("draws higher systems as longer, deeper and brighter dashes, and very low ones not at all", () => {
    const sp = crestSpacingPx(12);
    const v = crestSpeedPxPerS(12, sp);
    let l = 0;
    let d = 0;
    let w = 0;
    let a = 0;
    for (let hs = 0; hs <= 12; hs += 0.25) {
      expect(dashLength(hs, "combined")).toBeGreaterThanOrEqual(l);
      expect(blobDepth(hs, "combined", v, sp)).toBeGreaterThanOrEqual(d);
      expect(dashWidth(hs, "secondary")).toBeGreaterThanOrEqual(w);
      expect(dashAlpha(hs)).toBeGreaterThanOrEqual(a);
      l = dashLength(hs, "combined");
      d = blobDepth(hs, "combined", v, sp);
      w = dashWidth(hs, "secondary");
      a = dashAlpha(hs);
    }
    expect(dashAlpha(4)).toBeGreaterThan(dashAlpha(1));
    expect(dashWidth(4, "secondary")).toBeGreaterThan(dashWidth(1, "secondary"));
    expect(dashLength(4, "primary")).toBeGreaterThan(dashLength(1, "primary"));
    // The smear is speed x time (the crest's motion), not height: never shallower for a higher sea.
    expect(blobDepth(4, "primary", v, sp)).toBeGreaterThanOrEqual(blobDepth(1, "primary", v, sp));
    // The stated formulas and their limits (docs/map-rendering.md section 4.6): length along
    // the crest clamp(4 + 0.7 Hs, 4.5, 7.2) px ...
    for (const [hs, len] of [
      [0, 4.5],
      [1.5, 5.05],
      [2, 5.4],
      [4, 6.8],
      [10, 7.2],
    ] as const)
      expect(dashLength(hs, "primary")).toBeCloseTo(len, 12);
    // ... the secondary's bar width clamp(1.8 + 0.3 Hs, 1.8, 3.4) ...
    for (const [hs, width] of [
      [0, 1.8],
      [2, 2.4],
      [10, 3.4],
    ] as const)
      expect(dashWidth(hs, "secondary")).toBeCloseTo(width, 12);
    // ... and the opacity: clamp(0.40 + 0.14 Hs, 0.40, 0.95).
    expect(dashAlpha(0)).toBe(0.4);
    expect(dashAlpha(2)).toBeCloseTo(0.68, 12);
    expect(dashAlpha(10)).toBe(0.95);
    // A long split dash for the secondary swell, a short shallow one for the wind sea (shape, not colour alone).
    expect(dashLength(4, "secondary")).toBeCloseTo(2.5 * dashLength(4, "primary"), 10);
    expect(dashLength(4, "windsea")).toBeCloseTo(0.65 * dashLength(4, "primary"), 10);
    expect(blobDepth(4, "windsea", v, sp)).toBeCloseTo(0.8 * Math.min(BLOB_HEAD_PX + v * SMEAR_SECONDS, SMEAR_MAX_SHARE * sp, BLOB_MAX_DEPTH_SHARE * dashLength(4, "windsea")), 12);
    expect(blobDepth(4, "combined", v, sp)).toBe(blobDepth(4, "primary", v, sp));
  });

  it("smears each blob by the way its crest moved over the last SMEAR_SECONDS of animation (a display choice)", () => {
    expect([BLOB_HEAD_PX, SMEAR_SECONDS, SMEAR_MAX_SHARE, BLOB_MAX_DEPTH_SHARE]).toEqual([2, 0.55, 0.5, 1.6]);
    for (const tp of [6, 8, 10, 12, 16, 20]) {
      const sp = crestSpacingPx(tp);
      const v = crestSpeedPxPerS(tp, sp);
      for (const hs of [0.5, 1, 3, 6]) {
        const depth = blobDepth(hs, "combined", v, sp);
        expect(depth).toBeCloseTo(Math.min(BLOB_HEAD_PX + v * SMEAR_SECONDS, SMEAR_MAX_SHARE * sp, BLOB_MAX_DEPTH_SHARE * dashLength(hs, "combined")), 12);
        // Like the reference's (about 5 along the crest by 7 along the travel): a typical sea's
        // blob (1 to 3 m, 10 s or more) is at least as deep as it is long, and from 10 s on no
        // blob lies across its swell (a higher sea's longer crest reaches 0.85 of its depth).
        if (tp >= 10 && hs <= 3) expect(depth, `${hs} m, ${tp} s`).toBeGreaterThanOrEqual(dashLength(hs, "combined"));
        if (tp >= 10) expect(depth, `${hs} m, ${tp} s`).toBeGreaterThanOrEqual(0.8 * dashLength(hs, "combined"));
      }
    }
    // 6.1 px for a 10 s sea of 3 m at the default spacing (2 + 0.55 s x 7.5 px/s; 6.1 px long) ...
    const s10 = crestSpacingPx(10);
    expect(blobDepth(3, "combined", crestSpeedPxPerS(10, s10), s10)).toBeCloseTo(6.12, 1);
    expect(dashLength(3, "combined")).toBeCloseTo(6.1, 12);
    // ... at most half the crest spacing (a 4 s wind sea: 8 px spacing, rows stay apart) ...
    expect(blobDepth(8, "combined", crestSpeedPxPerS(4, 8), 8)).toBe(4);
    // ... and only the bright head without a speed, nothing without a spacing.
    expect(blobDepth(3, "combined", 0, 18)).toBe(BLOB_HEAD_PX);
    expect(blobDepth(3, "combined", NaN, 18)).toBe(BLOB_HEAD_PX);
    expect(blobDepth(3, "combined", 8, 0)).toBe(0);
  });

  it("tints each dash from the colour field under it: every channel lifted by TINT_LIFT, never to white", () => {
    expect([TINT_LIFT, TINT_HEADROOM]).toEqual([95, 0.02]);
    // Violet (2 m): R and G lifted by 95 (192, 174); B keeps 2 % of its way to 255 (253.4), a
    // lift of 77: about the same in every channel, so the blob stays violet-lavender, not grey.
    expect(dashTint([97, 79, 176], "combined")).toEqual([192, 174, 253]);
    expect(dashTint([97, 79, 176], "primary")).toEqual([192, 174, 253]);
    // Pink (8 m): the channels near 255 keep 2 % of their way there, so the tint stays pink.
    expect(dashTint([223, 134, 184], "combined")).toEqual([254, 229, 254]);
    // The partitions: 75 % their own colour.
    expect(dashTint([97, 79, 176], "secondary")).toEqual([Math.round(192 + (255 - 192) * 0.75), Math.round(174 + (207 - 174) * 0.75), Math.round(253.42 + (133 - 253.42) * 0.75)]);
    expect(dashTint(null, "windsea")).toEqual([156, 236, 255]);
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
    expect(DASH_CELL_PX).toEqual({ primary: 12, combined: 12, secondary: 13, windsea: 18 });
    expect(maxDashes(1440)).toBe(MAX_DASHES);
    expect(maxDashes(412)).toBe(PHONE_MAX_DASHES);
    expect([MAX_DASHES, PHONE_MAX_DASHES]).toEqual([8000, 3000]);
    // One filled quad per blob, adapted to the device between these, for D11's 5 ms draw p95 (docs/map-rendering.md 4.8).
    expect([MIN_FRAME_QUADS, MAX_FRAME_QUADS]).toEqual([1_500, 7_000]);
    expect(DRAW_TARGET_MS).toBeLessThan(5);
  });

  it("tells the systems apart by dash shape and colour, never colour alone", () => {
    const styles = WAVE_SYSTEMS.map((s) => FAMILY_STYLES[s]);
    const partitions = (["primary", "secondary", "windsea"] as const).map((s) => FAMILY_STYLES[s]);
    // Shape: soft blobs, long split bars (a gap of 0.2 of the length) and short blobs.
    expect(new Set(partitions.map((s) => `${s.shape}/${s.lengthFactor}/${s.gap}`)).size).toBe(3);
    expect(new Set(partitions.map((s) => s.color)).size).toBe(3);
    expect(FAMILY_STYLES.primary).toMatchObject({ shape: "blob", gap: 0, lengthFactor: 1, depthFactor: 1, color: "#ffffff", tintMix: 0 });
    expect(FAMILY_STYLES.secondary).toMatchObject({ shape: "bar", gap: 0.2, lengthFactor: 2.5, depthFactor: 1, color: "#ffcf85", tintMix: 0.75 });
    // The blobs were made shorter along the crest (the reference's proportions); the
    // secondary's bars kept their length: 2.5 x the new base, within 4.5 % of 1.8 x the old one.
    for (const hs of [0.5, 1, 2, 3, 4, 6, 8]) {
      const before = 1.8 * Math.min(10, Math.max(6.5, 6 + 0.8 * hs));
      expect(Math.abs(dashLength(hs, "secondary") / before - 1), `${hs} m`).toBeLessThan(0.045);
    }
    expect(FAMILY_STYLES.windsea).toMatchObject({ shape: "blob", gap: 0, lengthFactor: 0.65, depthFactor: 0.8, color: "#9cecff", tintMix: 0.75 });
    expect(FAMILY_STYLES.combined).toMatchObject({ shape: "blob", gap: 0, lengthFactor: 1, depthFactor: 1, color: "#ffffff", tintMix: 0 });
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
