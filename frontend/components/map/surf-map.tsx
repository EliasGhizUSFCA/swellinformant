"use client";

import "leaflet/dist/leaflet.css";

import type { LatLngExpression, Map as LeafletMap } from "leaflet";
import { CircleMarker, MapContainer, TileLayer, Tooltip, useMap } from "react-leaflet";
import { memo, useCallback, useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";

import { MAP_MAX_BOUNDS, minZoomFor, nearestCopy, RECENTRE_LNG, wrapCentre, wrapKey } from "@/lib/map-bounds";
import { tileConfig } from "@/lib/map-tiles";
import { colorForScore } from "@/lib/quality";
import { score10 } from "@/lib/rating";
import type { SpotSummary } from "@/types/api";

/** What a marker shows for the selected forecast time (null = no usable forecast). */
export interface MarkerState {
  score: number | null;
  tooltip: string;
  /** A second tooltip line (swell and wind in words). */
  detail?: string | null;
  highlight?: boolean;
  /** Directions FROM (degrees) of the swell that drives the surf and of the wind, for the
   * marker's arrows (components/map/spot-glyphs); null or absent = no arrow. */
  swellFromDeg?: number | null;
  windFromDeg?: number | null;
  /** The marker's height label, e.g. "4–6 ft" (our predicted surf); null = none. */
  label?: string | null;
  /** The spot's served forecast is out of date (its `freshness.stale`). */
  stale?: boolean;
}

/**
 * Navigation options shared by both world maps (lib/map-bounds.ts): the latitude band with a
 * hard edge, no rubber band past the zoom limits, and no worldCopyJump (markers are placed in
 * the world copy nearest the view instead; the two together put markers a world away).
 */
export const BOUNDED_MAP_OPTIONS = {
  maxBounds: MAP_MAX_BOUNDS,
  maxBoundsViscosity: 1,
  bounceAtZoomLimits: false,
  worldCopyJump: false,
} as const;

/**
 * Keeps the minimum zoom at one world width for the container's current size (re-measured
 * whenever it resizes: window, rotation, layout) and re-centres the view on the same meridian,
 * without animation, once a long pan has taken its longitude past ±540°. `onRecentre` runs
 * first with the new centre longitude, so markers can move to their new copy in the same task.
 */
export function MapBounds({ onRecentre }: { onRecentre?: (lng: number) => void }) {
  const map = useMap();
  const recentred = useEffectEvent((lng: number) => onRecentre?.(lng));
  useEffect(() => {
    const fit = () => {
      const size = map.getSize();
      map.setMinZoom(minZoomFor(size.x, size.y));
    };
    const recentre = () => {
      const c = map.getCenter();
      if (Math.abs(c.lng) <= RECENTRE_LNG) return;
      const lng = wrapCentre(c.lng);
      recentred(lng);
      map.setView([c.lat, lng], map.getZoom(), { animate: false });
    };
    fit();
    // Leaflet only re-measures on window resize; also follow the container's own size.
    const observer = new ResizeObserver(() => {
      map.invalidateSize();
      fit();
    });
    observer.observe(map.getContainer());
    map.on("moveend", recentre);
    return () => {
      observer.disconnect();
      map.off("moveend", recentre);
    };
  }, [map]);
  return null;
}

/**
 * Tracks the view centre's longitude for placing markers in their nearest world copy. It
 * reports a new centre only when some longitude in `lons` changes copy (it crosses the
 * centre ± 180°), so a pan re-renders the markers a few times per world, not every frame.
 */
export function useWrapCentre(
  lons: readonly number[],
  initial: number,
): { centreLng: number; onMove: (lng: number) => void; onRecentre: (lng: number) => void } {
  const [centreLng, setCentreLng] = useState(initial);
  const key = useRef<string | null>(null);
  const onMove = useCallback(
    (lng: number) => {
      const k = wrapKey(lons, lng);
      if (k === key.current) return;
      key.current = k;
      setCentreLng(lng);
    },
    [lons],
  );
  // The re-centre moves the view by whole worlds: re-place the markers before it happens, in
  // the same task, so no frame shows them a world away.
  const onRecentre = useCallback(
    (lng: number) => {
      key.current = wrapKey(lons, lng);
      flushSync(() => setCentreLng(lng));
    },
    [lons],
  );
  return { centreLng, onMove, onRecentre };
}

/** Calls `onCentre` with the centre longitude on every map move (cheap: see useWrapCentre). */
export function CentreWatcher({ onCentre }: { onCentre: (lng: number) => void }) {
  const map = useMap();
  useEffect(() => {
    const report = () => onCentre(map.getCenter().lng);
    report();
    map.on("move moveend", report);
    return () => {
      map.off("move moveend", report);
    };
  }, [map, onCentre]);
  return null;
}

/**
 * Moves the map to `centre` the way a flight should: animated, or in one jump when the user
 * prefers reduced motion (Leaflet's own animations do not check that preference).
 */
export function flyOrJump(map: LeafletMap, centre: LatLngExpression, zoom: number, duration: number) {
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) map.setView(centre, zoom, { animate: false });
  else map.flyTo(centre, zoom, { duration });
}

/** CSS pixels of the map hidden under floating controls and panels, by edge. */
export interface MapCover {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

/**
 * Flies to a spot's nearest world copy when `slug` changes, and only then: a filter that hides
 * and re-shows the selected spot, or a new forecast hour, never moves the map. With `cover`
 * (what the controls and the open spot panel hide of a map of the given size: a bottom sheet on
 * phones, a side sheet in landscape) the spot lands in the middle of the part left uncovered,
 * not at the map centre.
 */
export function FlyToSpot({
  slug,
  lat,
  lon,
  minZoom = 5,
  cover,
}: {
  slug: string | null;
  lat?: number;
  lon?: number;
  minZoom?: number;
  cover?: (size: { x: number; y: number }) => MapCover;
}) {
  const map = useMap();
  const fly = useEffectEvent(() => {
    if (lat == null || lon == null) return;
    const zoom = Math.max(map.getZoom(), minZoom);
    const target: [number, number] = [lat, nearestCopy(lon, map.getCenter().lng)];
    const c = cover?.(map.getSize());
    // The map centre goes this far right of and below the spot, which then sits in the middle
    // of the uncovered part.
    const dx = c ? (c.right - c.left) / 2 : 0;
    const dy = c ? (c.bottom - c.top) / 2 : 0;
    flyOrJump(map, dx || dy ? map.unproject(map.project(target, zoom).add([dx, dy]), zoom) : target, zoom, 0.8);
  });
  useEffect(() => {
    if (slug) fly();
  }, [slug]);
  return null;
}

/** The street tiles of both world maps (lib/map-tiles). */
export const TILES = tileConfig(
  process.env.NEXT_PUBLIC_MAP_TILE_URL,
  process.env.NEXT_PUBLIC_MAP_ATTRIBUTION,
  process.env.NEXT_PUBLIC_CARTO_API_KEY,
);
// Shown when tiles keep failing and none has loaded (blocked network or wrong URL). A missing
// CARTO key cannot be detected here: CARTO returns HTTP 200 "API KEY REQUIRED" images.
const FAILED_TILES_BEFORE_NOTICE = 4;

/** The street tile layer; `onFailed(true)` once FAILED_TILES_BEFORE_NOTICE tiles failed before any loaded. */
export function TileErrors({ onFailed }: { onFailed: (failed: boolean) => void }) {
  const counts = useRef({ loaded: 0, failed: 0 });
  return (
    <TileLayer
      url={TILES.url}
      attribution={TILES.attribution}
      maxZoom={TILES.maxZoom}
      // OpenStreetMap's tile policy requires a Referer; keep it explicit per request.
      referrerPolicy="strict-origin-when-cross-origin"
      eventHandlers={{
        tileload: () => {
          counts.current.loaded += 1;
          onFailed(false);
        },
        tileerror: () => {
          counts.current.failed += 1;
          if (counts.current.loaded === 0 && counts.current.failed >= FAILED_TILES_BEFORE_NOTICE) onFailed(true);
        },
      }}
    />
  );
}

const MiniMarker = memo(function MiniMarker({
  spot,
  lng,
  score,
  tooltip,
  selected,
  emphasised,
}: {
  spot: SpotSummary;
  lng: number;
  score: number | null;
  tooltip: string;
  selected: boolean;
  emphasised: boolean;
}) {
  const center = useMemo<[number, number]>(() => [spot.latitude, lng], [spot.latitude, lng]);
  const fill = colorForScore(score);
  const pathOptions = useMemo(
    () => ({ color: selected ? "#06263a" : "#ffffff", weight: selected ? 3 : 1.5, fillColor: fill, fillOpacity: 0.95 }),
    [selected, fill],
  );
  const slug = spot.slug;
  return (
    <CircleMarker
      center={center}
      radius={selected ? 11 : emphasised ? 8 : 6}
      pathOptions={pathOptions}
      // Set at creation (Leaflet ignores className changes in later setStyle calls).
      className={`spot-marker spot-${slug}`}
    >
      <Tooltip direction="top" offset={[0, -6]}>
        <strong>{spot.name}</strong>
        <br />
        {tooltip}
      </Tooltip>
    </CircleMarker>
  );
});

/**
 * World map of monitored spots (the spot page's mini map). Marker colour = the best quality
 * in the next 7 days.
 */
export default function SurfMap({
  spots,
  selected,
  zoom = 2,
  center = [15, 10] as [number, number],
}: {
  spots: SpotSummary[];
  selected?: string | null;
  zoom?: number;
  center?: [number, number];
}) {
  const selectedSpot = spots.find((s) => s.slug === selected);
  const [tilesFailed, setTilesFailed] = useState(false);
  const lons = useMemo(() => spots.map((s) => s.longitude), [spots]);
  const { centreLng, onMove, onRecentre } = useWrapCentre(lons, center[1]);
  return (
    // `isolation: isolate` keeps Leaflet's internal z-indexes (400–1000) from painting over
    // the sticky site header when the page scrolls.
    <div
      role="region"
      aria-label="Map of monitored surf spots"
      style={{ height: "100%", width: "100%", position: "relative", isolation: "isolate" }}
    >
      <MapContainer
        center={center}
        zoom={zoom}
        minZoom={1}
        {...BOUNDED_MAP_OPTIONS}
        scrollWheelZoom
        style={{ height: "100%", width: "100%", borderRadius: "1rem" }}
      >
        <TileErrors onFailed={setTilesFailed} />
        {spots.map((s) => {
          const score = s.best_upcoming?.score ?? s.current?.score ?? null;
          return (
            <MiniMarker
              key={s.slug}
              spot={s}
              lng={nearestCopy(s.longitude, centreLng)}
              score={score}
              tooltip={score != null ? `Best next 7 days: ${score10(score)}/10` : "No usable forecast yet"}
              selected={s.slug === selected}
              emphasised={!!s.next_event}
            />
          );
        })}
        <FlyToSpot slug={selected ?? null} lat={selectedSpot?.latitude} lon={selectedSpot?.longitude} />
        <MapBounds onRecentre={onRecentre} />
        <CentreWatcher onCentre={onMove} />
      </MapContainer>
      {tilesFailed && (
        <div
          role="status"
          className="pointer-events-none absolute inset-x-3 top-3 z-[1100] mx-auto max-w-md rounded-lg border border-amber-300 bg-amber-50/95 px-3 py-2 text-center text-xs text-amber-900 shadow-sm"
        >
          Map tiles couldn&apos;t load, so only the spot markers are shown. Check your connection, or configure
          another tile server with NEXT_PUBLIC_MAP_TILE_URL.
        </div>
      )}
    </div>
  );
}
