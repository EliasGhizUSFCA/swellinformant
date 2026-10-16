/**
 * Navigation limits of the world maps (the forecast map and the spot-page mini map).
 *
 * Latitude: the wave-model data band, 84°N to 78.5°S (the extent of the GFS-Wave grid as
 * measured on a stored live cycle), inside the Mercator edge at ±85.05°. Dragging stops at the
 * band (maxBoundsViscosity 1), so the screen never shows rows past the edge of the world.
 *
 * Longitude: effectively unbounded. The basemap, colour fields and tiles repeat in every world
 * copy, so the map pans continuously across the date line; markers and flights use the copy
 * nearest the view centre (`nearestCopy`), and the view is re-centred silently once its
 * longitude runs far from zero (`RECENTRE_LNG`), so pixel coordinates stay small.
 *
 * Zoom: the smallest zoom shows each longitude at most once (the world is at least as wide as
 * the map) and fills the map's height with the band (`minZoomFor`).
 */
import { mercY } from "@/lib/map-geo";

export const LAT_NORTH = 84;
export const LAT_SOUTH = -78.5;
/** Longitude is effectively unbounded; Leaflet needs finite numbers. */
export const LNG_LIMIT = 100_000;
export const MAP_MAX_BOUNDS: [[number, number], [number, number]] = [
  [LAT_SOUTH, -LNG_LIMIT],
  [LAT_NORTH, LNG_LIMIT],
];
/** Mercator height of the band, in world units (0..1): 0.8347. */
export const BAND_HEIGHT = mercY(LAT_SOUTH) - mercY(LAT_NORTH);
/** Leaflet's tile size: a zoom-z world is 256 * 2^z CSS pixels wide. */
export const WORLD_PX_AT_ZOOM_0 = 256;
/** The view is re-centred on the equivalent longitude in [-180, 180) beyond this. */
export const RECENTRE_LNG = 540;

/**
 * Smallest zoom at which a `width` x `height` map shows no longitude twice and the band
 * covers its whole height; `floor` when the size is unknown (not yet laid out).
 */
export function minZoomFor(width: number, height: number, floor = 1): number {
  if (!(width > 0 && height > 0)) return floor;
  return Math.max(floor, Math.log2(width / WORLD_PX_AT_ZOOM_0), Math.log2(height / (WORLD_PX_AT_ZOOM_0 * BAND_HEIGHT)));
}

/** The longitude of `lon`'s world copy nearest `centreLng` (|result - centreLng| <= 180). */
export const nearestCopy = (lon: number, centreLng: number): number => lon + 360 * Math.round((centreLng - lon) / 360);

/** The same meridian in [-180, 180). */
export const wrapCentre = (lng: number): number => ((((lng + 180) % 360) + 360) % 360) - 180;

/**
 * Which world copy each longitude is drawn in around `centreLng`, as one string. It changes
 * only when some longitude crosses `centreLng ± 180`, so markers are re-placed only then.
 */
export const wrapKey = (lons: readonly number[], centreLng: number): string => lons.map((l) => Math.round((centreLng - l) / 360)).join(",");
