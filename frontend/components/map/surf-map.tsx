"use client";

import "leaflet/dist/leaflet.css";

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
  highlight?: boolean;
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
 * Flies to a spot's nearest world copy when `slug` changes, and only then: a filter that hides
 * and re-shows the selected spot, or a new forecast hour, never moves the map. With `cover`
 * (`top` CSS pixels under the top controls, `bottom` the fraction of the height under a bottom
 * sheet) the spot lands in the middle of the strip left uncovered, not at the map centre.
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
  cover?: { top: number; bottom: number };
}) {
  const map = useMap();
  const fly = useEffectEvent(() => {
    if (lat == null || lon == null) return;
    const zoom = Math.max(map.getZoom(), minZoom);
    const target: [number, number] = [lat, nearestCopy(lon, map.getCenter().lng)];
    const h = map.getSize().y;
    const dy = cover && cover.bottom > 0 ? h / 2 - (cover.top + h * (1 - cover.bottom)) / 2 : 0;
    map.flyTo(dy ? map.unproject(map.project(target, zoom).add([0, dy]), zoom) : target, zoom, { duration: 0.8 });
  });
  useEffect(() => {
    if (slug) fly();
  }, [slug]);
  return null;
}

const TILES = tileConfig(
  process.env.NEXT_PUBLIC_MAP_TILE_URL,
  process.env.NEXT_PUBLIC_MAP_ATTRIBUTION,
  process.env.NEXT_PUBLIC_CARTO_API_KEY,
);
// Shown when tiles keep failing and none has loaded (blocked network or wrong URL). A missing
// CARTO key cannot be detected here: CARTO returns HTTP 200 "API KEY REQUIRED" images.
const FAILED_TILES_BEFORE_NOTICE = 4;

const MiniMarker = memo(function MiniMarker({
  spot,
  lng,
  score,
  tooltip,
  selected,
  emphasised,
  onSelect,
}: {
  spot: SpotSummary;
  lng: number;
  score: number | null;
  tooltip: string;
  selected: boolean;
  emphasised: boolean;
  onSelect?: (slug: string) => void;
}) {
  const center = useMemo<[number, number]>(() => [spot.latitude, lng], [spot.latitude, lng]);
  const fill = colorForScore(score);
  const pathOptions = useMemo(
    () => ({ color: selected ? "#06263a" : "#ffffff", weight: selected ? 3 : 1.5, fillColor: fill, fillOpacity: 0.95 }),
    [selected, fill],
  );
  const slug = spot.slug;
  const eventHandlers = useMemo(() => ({ click: () => onSelect?.(slug) }), [onSelect, slug]);
  return (
    <CircleMarker
      center={center}
      radius={selected ? 11 : emphasised ? 8 : 6}
      pathOptions={pathOptions}
      // Set at creation (Leaflet ignores className changes in later setStyle calls).
      className={`spot-marker spot-${slug}`}
      eventHandlers={eventHandlers}
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
 * World map of monitored spots. Marker colour = our predicted quality at the selected
 * forecast time when ``markers`` is given, otherwise the best quality in the next 7 days.
 */
export default function SurfMap({
  spots,
  selected,
  onSelect,
  markers,
  height = "100%",
  zoom = 2,
  center = [15, 10] as [number, number],
}: {
  spots: SpotSummary[];
  selected?: string | null;
  onSelect?: (slug: string) => void;
  markers?: Record<string, MarkerState>;
  height?: string;
  zoom?: number;
  center?: [number, number];
}) {
  const selectedSpot = spots.find((s) => s.slug === selected);
  const tileCounts = useRef({ loaded: 0, failed: 0 });
  const [tilesFailed, setTilesFailed] = useState(false);
  const lons = useMemo(() => spots.map((s) => s.longitude), [spots]);
  const { centreLng, onMove, onRecentre } = useWrapCentre(lons, center[1]);
  return (
    // `isolation: isolate` keeps Leaflet's internal z-indexes (400–1000) from painting over
    // the sticky site header when the page scrolls.
    <div
      role="region"
      aria-label="Map of monitored surf spots"
      style={{ height, width: "100%", position: "relative", isolation: "isolate" }}
    >
      <MapContainer
        center={center}
        zoom={zoom}
        minZoom={1}
        {...BOUNDED_MAP_OPTIONS}
        scrollWheelZoom
        style={{ height: "100%", width: "100%", borderRadius: "1rem" }}
      >
        <TileLayer
          url={TILES.url}
          attribution={TILES.attribution}
          maxZoom={TILES.maxZoom}
          // OpenStreetMap's tile policy requires a Referer; keep it explicit per request.
          referrerPolicy="strict-origin-when-cross-origin"
          eventHandlers={{
            tileload: () => {
              tileCounts.current.loaded += 1;
              setTilesFailed(false);
            },
            tileerror: () => {
              tileCounts.current.failed += 1;
              if (tileCounts.current.loaded === 0 && tileCounts.current.failed >= FAILED_TILES_BEFORE_NOTICE) {
                setTilesFailed(true);
              }
            },
          }}
        />
        {spots.map((s) => {
          const m = markers?.[s.slug];
          const score = markers ? (m?.score ?? null) : (s.best_upcoming?.score ?? s.current?.score ?? null);
          return (
            <MiniMarker
              key={s.slug}
              spot={s}
              lng={nearestCopy(s.longitude, centreLng)}
              score={score}
              tooltip={
                markers
                  ? (m?.tooltip ?? "No usable forecast")
                  : score != null
                    ? `Best next 7 days: ${score10(score)}/10`
                    : "No usable forecast yet"
              }
              selected={s.slug === selected}
              emphasised={markers ? !!m?.highlight : !!s.next_event}
              onSelect={onSelect}
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
