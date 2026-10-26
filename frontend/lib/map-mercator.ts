/**
 * Web Mercator in world units (the projection Leaflet uses, EPSG:3857): x, y in 0..1 across
 * the whole world (x east from -180°, y south from the top at ~85.05°N). Kept apart from
 * lib/map-geo so the spot page's mini map does not load the basemap and contour code.
 */
export const MAX_LAT = 85.0511287798;
const RAD = Math.PI / 180;

export const mercX = (lon: number) => (lon + 180) / 360;
export function mercY(lat: number): number {
  const phi = Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) * RAD;
  return 0.5 - Math.log(Math.tan(Math.PI / 4 + phi / 2)) / (2 * Math.PI);
}
export const mercLon = (x: number) => x * 360 - 180;
export const mercLat = (y: number) => (2 * Math.atan(Math.exp(Math.PI * (1 - 2 * y))) - Math.PI / 2) / RAD;
