"use client";

import "leaflet/dist/leaflet.css";

import type { Map as LeafletMap } from "leaflet";
import { useEffect, useMemo, useRef, useState } from "react";
import { CircleMarker, MapContainer, TileLayer, Tooltip, useMap, useMapEvents, ZoomControl } from "react-leaflet";

import type { LayerScale } from "@/lib/map-colors";
import type { FieldGrid } from "@/lib/map-fields";
import { mercX, mercY, type IsoLine } from "@/lib/map-geo";
import { readout, type DisplayUnits, type LayerId, type PointSample } from "@/lib/map-layers";
import type { MapSettings } from "@/lib/map-settings";
import { tileConfig } from "@/lib/map-tiles";
import { colorForScore } from "@/lib/quality";
import type { FieldSet } from "@/hooks/map-fields";
import type { SpotSummary } from "@/types/api";

import type { CityLabel, IsobarPath } from "./basemap-draw";
import { fieldSampler, MapDataLayers, type ParticleMode } from "./map-canvas-layers";
import type { MarkerState } from "./surf-map";

const TILES = tileConfig(process.env.NEXT_PUBLIC_MAP_TILE_URL, process.env.NEXT_PUBLIC_MAP_ATTRIBUTION, process.env.NEXT_PUBLIC_CARTO_API_KEY);
const FAILED_TILES_BEFORE_NOTICE = 4;
const OCEAN_BACKGROUND = "#151a21";

/** Natural Earth 1:50m outlines turn visibly angular beyond this zoom; Streets go further. */
const DARK_MAX_ZOOM = 8;

function FlyTo({ spot }: { spot?: SpotSummary }) {
  const map = useMap();
  useEffect(() => {
    if (spot) map.flyTo([spot.latitude, spot.longitude], Math.max(map.getZoom(), 5), { duration: 0.8 });
  }, [spot, map]);
  return null;
}

function FlyToUser({ at }: { at: { lat: number; lon: number; key: number } | null }) {
  const map = useMap();
  useEffect(() => {
    if (at) map.flyTo([at.lat, at.lon], Math.max(map.getZoom(), 7), { duration: 1 });
  }, [at, map]);
  return null;
}

function MaxZoom({ zoom }: { zoom: number }) {
  const map = useMap();
  useEffect(() => {
    map.setMaxZoom(zoom);
  }, [map, zoom]);
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

interface ReadoutPoint {
  x: number;
  y: number;
  lat: number;
  lon: number;
  kind: "mouse" | "touch" | "keyboard";
}

/**
 * The value under the cursor (mouse), at a tapped point for a few seconds (touch) or at the
 * map centre while the map has keyboard focus.
 */
function Readout({ sample, layer, units }: { sample: ((lat: number, lon: number) => PointSample) | null; layer: LayerId; units: DisplayUnits }) {
  const [point, setPoint] = useState<ReadoutPoint | null>(null);
  const pointerType = useRef("mouse");
  const focused = useRef(false);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const centre = (m: LeafletMap): ReadoutPoint => {
    const size = m.getSize();
    const c = m.getCenter();
    return { x: size.x / 2, y: size.y / 2, lat: c.lat, lon: c.lng, kind: "keyboard" };
  };
  const map = useMapEvents({
    mousemove(e) {
      if (pointerType.current !== "mouse") return;
      setPoint({ x: e.containerPoint.x, y: e.containerPoint.y, lat: e.latlng.lat, lon: e.latlng.lng, kind: "mouse" });
    },
    mouseout() {
      if (pointerType.current === "mouse" && !focused.current) setPoint(null);
    },
    click(e) {
      if (pointerType.current === "mouse") return;
      setPoint({ x: e.containerPoint.x, y: e.containerPoint.y, lat: e.latlng.lat, lon: e.latlng.lng, kind: "touch" });
      if (hideTimer.current) clearTimeout(hideTimer.current);
      hideTimer.current = setTimeout(() => setPoint(null), 4000);
    },
    movestart() {
      setPoint((p) => (p?.kind === "touch" ? null : p));
    },
    moveend() {
      if (focused.current) setPoint(centre(map));
    },
  });

  useEffect(() => {
    const el = map.getContainer();
    const onPointer = (e: PointerEvent) => {
      pointerType.current = e.pointerType || "mouse";
    };
    // Only real keyboard focus (not a click on the map) shows the centre readout.
    const onFocus = () => {
      if (!el.matches(":focus-visible")) return;
      focused.current = true;
      setPoint(centre(map));
    };
    const onBlur = () => {
      focused.current = false;
      setPoint(null);
    };
    el.addEventListener("pointerdown", onPointer);
    el.addEventListener("pointermove", onPointer);
    el.addEventListener("focus", onFocus);
    el.addEventListener("blur", onBlur);
    return () => {
      el.removeEventListener("pointerdown", onPointer);
      el.removeEventListener("pointermove", onPointer);
      el.removeEventListener("focus", onFocus);
      el.removeEventListener("blur", onBlur);
      if (hideTimer.current) clearTimeout(hideTimer.current);
    };
  }, [map]);

  const r = point && sample ? readout(layer, sample(point.lat, point.lon), units) : null;
  if (!point || !r) return null;
  const left = Math.min(point.x + 14, map.getSize().x - 220);
  const top = Math.max(point.y - 52, 8);
  return (
    <>
      {point.kind === "keyboard" && (
        <div
          aria-hidden
          data-testid="map-readout-centre"
          className="pointer-events-none absolute z-[1000] size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white/90 shadow"
          style={{ left: point.x, top: point.y }}
        />
      )}
      <div
        role="status"
        data-testid="map-readout"
        className="pointer-events-none absolute z-[1000] max-w-[220px] rounded-lg bg-[#14181c]/90 px-2.5 py-1.5 text-xs text-white shadow-lg ring-1 ring-white/10"
        style={{ left: Math.max(8, left), top }}
      >
        <div className="text-[10px] font-semibold tracking-wide text-white/70 uppercase">{r.title}</div>
        <div className="font-medium tabular-nums">{r.text}</div>
      </div>
    </>
  );
}

function TileErrors({ onFailed }: { onFailed: (failed: boolean) => void }) {
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

export interface ForecastMapProps {
  spots: SpotSummary[];
  markers: Record<string, MarkerState>;
  selected: string | null;
  onSelect: (slug: string) => void;
  /** Display settings (layer, basemap, units, ...): passed whole to the data layers. */
  settings: MapSettings;
  /** Fields for the active layer at `fieldsAt` (null = no ocean data). */
  fields: FieldSet | null;
  fieldsAt: string | null;
  pressure: FieldGrid | null;
  isobars: IsoLine[] | null;
  scale: LayerScale;
  particles: ParticleMode;
  units: DisplayUnits;
  cities: CityLabel[] | null;
  onZoom?: (zoom: number) => void;
  userLocation: { lat: number; lon: number; key: number } | null;
  /** Accessible description of the colour layer and time, e.g. "Offshore wave height, 8am Sun PDT". */
  fieldLabel: string;
  /** Short credit for the ocean layers in Leaflet's attribution strip (plain text, escaped here). */
  fieldAttribution?: string;
}

function ZoomWatcher({ onZoom }: { onZoom?: (z: number) => void }) {
  const map = useMapEvents({ zoomend: () => onZoom?.(map.getZoom()) });
  return null;
}

/**
 * Full-screen forecast map: ocean colour field, flow particles, dark Natural Earth basemap
 * (or OpenStreetMap tiles), isobars, labels and the spot markers on top. Markers stay SVG
 * paths (`path.spot-<slug>`) coloured by our surf-quality prediction and always clickable.
 */
export default function ForecastMap(props: ForecastMapProps) {
  const { spots, markers, selected, onSelect, settings, fields, pressure, isobars, scale, particles, units } = props;
  const { layer, basemap } = settings;
  const [tilesFailed, setTilesFailed] = useState(false);
  const selectedSpot = spots.find((s) => s.slug === selected);
  const isobarPaths = useMemo<IsobarPath[] | null>(() => {
    if (!isobars) return null;
    return isobars.map((l) => {
      const n = l.pts.length / 2;
      const pts = new Float64Array(l.pts.length);
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      for (let i = 0; i < n; i++) {
        const x = mercX(l.pts[i * 2]!);
        const y = mercY(l.pts[i * 2 + 1]!);
        pts[i * 2] = x;
        pts[i * 2 + 1] = y;
        minX = Math.min(minX, x);
        maxX = Math.max(maxX, x);
        minY = Math.min(minY, y);
        maxY = Math.max(maxY, y);
      }
      return { value: l.value, pts, minX, minY, maxX, maxY };
    });
  }, [isobars]);

  // The hover readout's sampler is defined with the data layers (map-canvas-layers).
  const sample = useMemo(() => fieldSampler(layer, fields, pressure), [layer, fields, pressure]);

  return (
    // `isolation: isolate` keeps Leaflet's internal z-indexes (400–1000) from painting over
    // the sticky site header and the floating map controls.
    <div role="region" aria-label="Map of monitored surf spots" className="forecast-map absolute inset-0" style={{ isolation: "isolate" }}>
      <MapContainer
        center={[15, 10]}
        zoom={2}
        minZoom={2}
        worldCopyJump
        scrollWheelZoom
        zoomControl={false}
        style={{ height: "100%", width: "100%", background: OCEAN_BACKGROUND }}
      >
        {basemap === "streets" && <TileErrors onFailed={setTilesFailed} />}
        <MapDataLayers
          layer={layer}
          fields={fields}
          pressure={pressure}
          settings={settings}
          particles={particles}
          basemap={basemap}
          scale={scale}
          fieldLabel={props.fieldLabel}
          fieldsAt={props.fieldsAt}
          cities={props.cities}
          isobarPaths={isobarPaths}
          fieldAttribution={props.fieldAttribution}
        />
        <ZoomControl position="bottomright" />
        {spots.map((s) => {
          const m = markers[s.slug];
          const score = m?.score ?? null;
          const isSel = s.slug === selected;
          return (
            <CircleMarker
              key={s.slug}
              center={[s.latitude, s.longitude]}
              radius={isSel ? 11 : m?.highlight ? 8 : 6}
              pathOptions={{ color: "#ffffff", weight: isSel ? 3 : 1.5, fillColor: colorForScore(score), fillOpacity: 0.95 }}
              // Set at creation (Leaflet ignores className changes in later setStyle calls).
              className={`spot-marker spot-${s.slug}`}
              eventHandlers={{
                click: () => onSelect(s.slug),
                // Leaflet makes markers focusable (their tooltip opens on focus): Enter or Space
                // opens the spot, as a click does.
                keydown: (e) => {
                  if (e.originalEvent.key === "Enter" || e.originalEvent.key === " ") {
                    e.originalEvent.preventDefault();
                    onSelect(s.slug);
                  }
                },
              }}
            >
              <Tooltip direction="top" offset={[0, -6]}>
                <strong>{s.name}</strong>
                <br />
                {m?.tooltip ?? "No usable forecast"}
              </Tooltip>
            </CircleMarker>
          );
        })}
        {props.userLocation && (
          <CircleMarker
            center={[props.userLocation.lat, props.userLocation.lon]}
            radius={7}
            pathOptions={{ color: "#ffffff", weight: 2.5, fillColor: "#3b82f6", fillOpacity: 1 }}
            className="user-location-marker"
            interactive={false}
          />
        )}
        <Readout sample={sample} layer={layer} units={units} />
        <FlyTo spot={selectedSpot} />
        <FlyToUser at={props.userLocation} />
        <ZoomWatcher onZoom={props.onZoom} />
        <MaxZoom zoom={basemap === "dark" ? DARK_MAX_ZOOM : TILES.maxZoom} />
        <InvalidateOnResize />
      </MapContainer>
      {tilesFailed && basemap === "streets" && (
        <div
          role="status"
          className="pointer-events-none absolute inset-x-3 top-16 z-[1100] mx-auto max-w-md rounded-lg border border-amber-300 bg-amber-50/95 px-3 py-2 text-center text-xs text-amber-900 shadow-sm"
        >
          Map tiles couldn&apos;t load. Switch back to the dark basemap in Settings, or configure another tile server with
          NEXT_PUBLIC_MAP_TILE_URL.
        </div>
      )}
    </div>
  );
}
