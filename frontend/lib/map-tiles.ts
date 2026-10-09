/**
 * Map tile configuration, shared by the Leaflet map and the CSP in next.config.ts.
 *
 * Default: the standard OpenStreetMap tiles. They need no API key, but the OSM tile usage
 * policy applies (attribution, light use, no bulk downloading):
 * https://operations.osmfoundation.org/policies/tiles/
 *
 * Configured at build time:
 *   NEXT_PUBLIC_CARTO_API_KEY    free CARTO key → CARTO Voyager tiles (sharper on Retina screens).
 *                                CARTO answers key-less raster requests with "API KEY REQUIRED" tiles.
 *   NEXT_PUBLIC_MAP_TILE_URL     any Leaflet URL template (takes precedence over the CARTO key)
 *   NEXT_PUBLIC_MAP_ATTRIBUTION  attribution HTML required by that provider
 *   MAP_TILE_HOSTS               extra CSP img-src sources (the tile URL's own host is added automatically)
 */

export const DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
export const CARTO_VOYAGER_URL = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png";

const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const CARTO_ATTRIBUTION = `${OSM_ATTRIBUTION} &copy; <a href="https://carto.com/attributions">CARTO</a>`;

export interface TileConfig {
  url: string;
  attribution: string;
  maxZoom: number;
}

const isCartoUrl = (url: string) => /\.basemaps\.cartocdn\.com\//i.test(url);

export function tileConfig(url?: string, attribution?: string, cartoKey?: string): TileConfig {
  const key = cartoKey?.trim();
  const resolved = url?.trim() || (key ? `${CARTO_VOYAGER_URL}?key=${encodeURIComponent(key)}` : DEFAULT_TILE_URL);
  const isCarto = isCartoUrl(resolved);
  return {
    url: resolved,
    attribution: attribution?.trim() || (isCarto ? CARTO_ATTRIBUTION : OSM_ATTRIBUTION),
    maxZoom: resolved === DEFAULT_TILE_URL ? 19 : 18,
  };
}

/**
 * CSP source ("scheme://host") for a Leaflet URL template. Placeholders in the host such as
 * "{s}" become a leftmost wildcard, the only position CSP allows: "{s}.tile.example.com" →
 * "*.tile.example.com", "tiles-{s}.example.com" → "*.example.com". Returns null when the
 * template is not an absolute http(s) URL.
 */
export function tileHostSource(template: string): string | null {
  const [, scheme, authority] = /^(https?):\/\/([^/?#]+)/i.exec(template.trim()) ?? [];
  if (!scheme || !authority) return null;
  const labels = authority.toLowerCase().split(".");
  let last = -1;
  labels.forEach((label, i) => {
    if (label.includes("{")) last = i;
  });
  const host = last === -1 ? labels.join(".") : ["*", ...labels.slice(last + 1)].join(".");
  if (host === "*" || !/^[*a-z0-9.:-]+$/.test(host)) return null;
  return `${scheme.toLowerCase()}://${host}`;
}

/** True for a CARTO tile URL without a key: CARTO then serves "API KEY REQUIRED" placeholder tiles. */
export function cartoKeyMissing(url: string): boolean {
  return isCartoUrl(url) && !/[?&](key|api_key)=[^&]+/i.test(url);
}

/** img-src sources for the configured tile URL plus any extra MAP_TILE_HOSTS entries. */
export function tileCspSources(url?: string, extraHosts?: string, cartoKey?: string): string[] {
  const sources = [tileHostSource(tileConfig(url, undefined, cartoKey).url), ...(extraHosts ?? "").split(/\s+/)];
  return [...new Set(sources.filter((s): s is string => !!s))];
}
