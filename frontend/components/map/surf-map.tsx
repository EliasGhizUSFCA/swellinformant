"use client";

import "leaflet/dist/leaflet.css";

import { CircleMarker, MapContainer, TileLayer, Tooltip, useMap } from "react-leaflet";
import { useEffect } from "react";

import { colorForScore } from "@/lib/quality";
import type { SpotSummary } from "@/types/api";

const TILE_URL =
  process.env.NEXT_PUBLIC_MAP_TILE_URL || "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png";
const ATTRIBUTION =
  process.env.NEXT_PUBLIC_MAP_ATTRIBUTION ||
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>';

function FlyTo({ spot }: { spot?: SpotSummary }) {
  const map = useMap();
  useEffect(() => {
    if (spot) map.flyTo([spot.latitude, spot.longitude], Math.max(map.getZoom(), 5), { duration: 0.8 });
  }, [spot, map]);
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
  return (
    <div role="region" aria-label="Map of monitored surf spots" style={{ height, width: "100%" }}>
      <MapContainer
        center={center}
        zoom={zoom}
        minZoom={2}
        worldCopyJump
        scrollWheelZoom
        style={{ height: "100%", width: "100%", borderRadius: "1rem" }}
      >
        <TileLayer url={TILE_URL} attribution={ATTRIBUTION} />
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
      </MapContainer>
    </div>
  );
}
