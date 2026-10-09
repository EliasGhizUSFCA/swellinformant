"use client";

import "leaflet/dist/leaflet.css";

import { CircleMarker, MapContainer, TileLayer, Tooltip, useMap } from "react-leaflet";
import { useEffect, useRef, useState } from "react";

import { tileConfig } from "@/lib/map-tiles";
import { colorForScore } from "@/lib/quality";
import type { SpotSummary } from "@/types/api";

const TILES = tileConfig(
  process.env.NEXT_PUBLIC_MAP_TILE_URL,
  process.env.NEXT_PUBLIC_MAP_ATTRIBUTION,
  process.env.NEXT_PUBLIC_CARTO_API_KEY,
);
// Shown when tiles keep failing and none has loaded (blocked network or wrong URL). A missing
// CARTO key cannot be detected here: CARTO returns HTTP 200 "API KEY REQUIRED" images.
const FAILED_TILES_BEFORE_NOTICE = 4;

function FlyTo({ spot }: { spot?: SpotSummary }) {
  const map = useMap();
  useEffect(() => {
    if (spot) map.flyTo([spot.latitude, spot.longitude], Math.max(map.getZoom(), 5), { duration: 0.8 });
  }, [spot, map]);
  return null;
}

/** Leaflet only re-measures on window resize; also follow the container's own size. */
function InvalidateOnResize() {
  const map = useMap();
  useEffect(() => {
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(map.getContainer());
    return () => observer.disconnect();
  }, [map]);
  return null;
}

/** World map of monitored spots; marker colour = best forecast quality in the next 7 days. */
export default function SurfMap({
  spots,
  selected,
  onSelect,
  height = "100%",
  zoom = 2,
  center = [15, 10] as [number, number],
}: {
  spots: SpotSummary[];
  selected?: string | null;
  onSelect?: (slug: string) => void;
  height?: string;
  zoom?: number;
  center?: [number, number];
}) {
  const selectedSpot = spots.find((s) => s.slug === selected);
  const tileCounts = useRef({ loaded: 0, failed: 0 });
  const [tilesFailed, setTilesFailed] = useState(false);
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
        minZoom={2}
        worldCopyJump
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
          const score = s.best_upcoming?.score ?? s.current?.score ?? null;
          const isSel = s.slug === selected;
          return (
            <CircleMarker
              key={s.slug}
              center={[s.latitude, s.longitude]}
              radius={isSel ? 11 : s.next_event ? 8 : 6}
              pathOptions={{
                color: isSel ? "#06263a" : "#ffffff",
                weight: isSel ? 3 : 1.5,
                fillColor: colorForScore(score),
                fillOpacity: 0.95,
              }}
              eventHandlers={{ click: () => onSelect?.(s.slug) }}
            >
              <Tooltip direction="top" offset={[0, -6]}>
                <strong>{s.name}</strong>
                <br />
                {score != null ? `Best next 7 days: ${score}/100` : "No forecast yet"}
              </Tooltip>
            </CircleMarker>
          );
        })}
        <FlyTo spot={selectedSpot} />
        <InvalidateOnResize />
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
