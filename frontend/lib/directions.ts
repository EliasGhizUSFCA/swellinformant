/**
 * Direction conventions shared by the map and the charts. The API's directions are nautical
 * degrees FROM (0 = north, clockwise): a "SW swell" (225) comes from the south-west and
 * travels towards the north-east. Arrows point where the wind or waves are GOING, the same
 * way the map's particles move, so a FROM direction is turned by 180 degrees for drawing.
 */

export const ARROW_CONVENTION_TEXT = "Arrows point where the wind or waves are going.";

const norm = (deg: number) => ((deg % 360) + 360) % 360;

/** Clockwise rotation (degrees, [0, 360)) of an arrow drawn pointing north for something
 * coming FROM `fromDeg`: it points where it is going. */
export function arrowRotationDeg(fromDeg: number): number {
  return norm(fromDeg + 180);
}

/** [u, v] (east, north) of a wind of `speed` FROM `fromDeg`: u = -V sin θ, v = -V cos θ. */
export function uvFromWind(speed: number, fromDeg: number): [number, number] {
  const t = (fromDeg * Math.PI) / 180;
  return [-speed * Math.sin(t), -speed * Math.cos(t)];
}
