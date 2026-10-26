import { describe, expect, it } from "vitest";

import { ARROW_CONVENTION_TEXT, arrowRotationDeg, uvFromWind } from "@/lib/directions";
import { compass } from "@/lib/format";

describe("arrow convention", () => {
  it("points arrows where the wind or waves go (FROM + 180°)", () => {
    expect(arrowRotationDeg(0)).toBe(180); // a northerly blows towards the south
    expect(arrowRotationDeg(90)).toBe(270); // an easterly blows towards the west
    expect(arrowRotationDeg(225)).toBe(45); // a SW swell travels towards the NE
    expect(arrowRotationDeg(180)).toBe(0);
    expect(arrowRotationDeg(360)).toBe(180);
    expect(arrowRotationDeg(-90)).toBe(90);
    expect(ARROW_CONVENTION_TEXT).toBe("Arrows point where the wind or waves are going.");
  });

  it("agrees with the (u, v) the map particles move along", () => {
    for (let from = 0; from < 360; from += 15) {
      const [u, v] = uvFromWind(10, from);
      // Screen bearing of the motion vector (clockwise from north).
      const motion = ((Math.atan2(u, v) * 180) / Math.PI + 360) % 360;
      const diff = Math.abs(((motion - arrowRotationDeg(from) + 540) % 360) - 180);
      expect(diff).toBeLessThan(1e-9);
    }
  });
});

describe("uvFromWind", () => {
  it("uses u = -V sin θ, v = -V cos θ (u east, v north, θ FROM)", () => {
    const [nu, nv] = uvFromWind(10, 0); // northerly: air moves south
    expect(nu).toBeCloseTo(0, 12);
    expect(nv).toBeCloseTo(-10, 12);
    const [eu, ev] = uvFromWind(10, 90); // easterly: air moves west
    expect(eu).toBeCloseTo(-10, 12);
    expect(ev).toBeCloseTo(0, 12);
    const [wu, wv] = uvFromWind(10, 270); // westerly: air moves east
    expect(wu).toBeCloseTo(10, 12);
    expect(wv).toBeCloseTo(0, 12);
    const [su, sv] = uvFromWind(Math.SQRT2, 225); // south-westerly: air moves north-east
    expect(su).toBeCloseTo(1, 12);
    expect(sv).toBeCloseTo(1, 12);
  });
});

describe("compass", () => {
  it("names the 16 points and wraps", () => {
    expect(compass(0)).toBe("N");
    expect(compass(11.2)).toBe("N");
    expect(compass(11.25)).toBe("NNE");
    expect(compass(225)).toBe("SW");
    expect(compass(290)).toBe("WNW");
    expect(compass(359)).toBe("N");
    expect(compass(-45)).toBe("NW");
    expect(compass(720 + 90)).toBe("E");
    expect(compass(Number.NaN)).toBe("—");
  });
});
