"use client";

import "leaflet/dist/leaflet.css";

import { svg, type Map as LeafletMap, type PathOptions } from "leaflet";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CircleMarker, MapContainer, Tooltip, useMap, useMapEvents, ZoomControl } from "react-leaflet";

import { minZoomFor, nearestCopy } from "@/lib/map-bounds";
import type { LayerScale } from "@/lib/map-colors";
import type { FieldGrid } from "@/lib/map-fields";
import { mercX, mercY, type IsoLine } from "@/lib/map-geo";
import { readout, type DisplayUnits, type LayerId, type PointSample } from "@/lib/map-layers";
import type { MapSettings } from "@/lib/map-settings";
import { colorForScore } from "@/lib/quality";
import type { FieldSet } from "@/hooks/map-fields";
import type { SpotSummary } from "@/types/api";

import type { CityLabel, IsobarPath } from "./basemap-draw";
import { fieldSampler, MapDataLayers, type ParticleMode } from "./map-canvas-layers";
import { BOUNDED_MAP_OPTIONS, CentreWatcher, flyOrJump, FlyToSpot, MapBounds, TileErrors, TILES, useWrapCentre, type MapCover, type MarkerState } from "./surf-map";

/** The map's sea colour under the dark basemap (and under the layers while they load). */
export const OCEAN_BACKGROUND = "#151a21";

/** Natural Earth 1:50m outlines turn visibly angular beyond this zoom; Streets go further. */
const DARK_MAX_ZOOM = 8;

/**
 * The first view: Atlantic-centred, at one world width (or zoom 2 where that is closer). At
 * 8°N the spots (43°S to 59°N) sit midway between the layer tabs and the timeline on desktop
 * screens; at the old 15°N the southern ones crowded the timeline and the zoom buttons.
 */
const INITIAL_CENTRE: [number, number] = [8, 10];
const INITIAL_ZOOM = 2;
/** Spots open at this zoom or closer. */
const SPOT_ZOOM = 5;
/** The user's position marker (one shared object, so a page render does not restyle it). */
const USER_PATH: PathOptions = { color: "#ffffff", weight: 2.5, fillColor: "#3b82f6", fillOpacity: 1 };

function FlyToUser({ at }: { at: { lat: number; lon: number; key: number } | null }) {
  const map = useMap();
  useEffect(() => {
    if (at) flyOrJump(map, [at.lat, nearestCopy(at.lon, map.getCenter().lng)], Math.max(map.getZoom(), 7), 1);
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
  const under = (m: LeafletMap, p: ReadoutPoint): ReadoutPoint => {
    const ll = m.containerPointToLatLng([p.x, p.y]);
    return { ...p, lat: ll.lat, lon: ll.lng };
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
    // A map moved under a still mouse (the spot list's fly-to, the recentre button, keys)
    // puts another place under the cursor: read the value there, never the old place's.
    move() {
      if (!focused.current) setPoint((p) => (p?.kind === "mouse" ? under(map, p) : p));
    },
    moveend() {
      if (focused.current) setPoint(centre(map));
      else setPoint((p) => (p?.kind === "mouse" ? under(map, p) : p));
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
        data-lat={point.lat.toFixed(3)}
        data-lon={point.lon.toFixed(3)}
        className="pointer-events-none absolute z-[1000] max-w-[220px] rounded-lg bg-[#14181c]/90 px-2.5 py-1.5 text-xs text-white shadow-lg ring-1 ring-white/10"
        style={{ left: Math.max(8, left), top }}
      >
        <div className="text-[10px] font-semibold tracking-wide text-white/70 uppercase">{r.title}</div>
        <div className="font-medium tabular-nums">{r.text}</div>
      </div>
    </>
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
  /**
   * What covers a map of the given size while a spot is open (the top controls, a bottom sheet
   * on phones, a side sheet in landscape). A selected spot then lands in the middle of the rest
   * instead of at the map centre.
   */
  cover?: (size: { x: number; y: number }) => MapCover;
}

function ZoomWatcher({ onZoom }: { onZoom?: (z: number) => void }) {
  const map = useMapEvents({ zoomend: () => onZoom?.(map.getZoom()) });
  return null;
}

/**
 * One spot marker, an SVG path `path.spot-<slug>` coloured by our surf-quality prediction.
 * Memoised with stable props, so a new forecast hour restyles only the markers whose colour
 * changed and a zoom or pan touches none; `lng` is the spot's world copy nearest the view.
 * `surface` "dark" (the dark basemap) uses the brighter-is-better ramp made for dark
 * backgrounds (at least 3:1 against them); "light" (the Streets tiles) the default ramp.
 */
export const SpotMarker = memo(function SpotMarker({
  spot,
  lng,
  score,
  tooltip,
  selected,
  highlight,
  onSelect,
  surface = "light",
}: {
  spot: SpotSummary;
  lng: number;
  score: number | null;
  tooltip: string;
  selected: boolean;
  highlight: boolean;
  onSelect: (slug: string) => void;
  surface?: "light" | "dark";
}) {
  const center = useMemo<[number, number]>(() => [spot.latitude, lng], [spot.latitude, lng]);
  const fill = colorForScore(score, surface);
  const pathOptions = useMemo(() => ({ color: "#ffffff", weight: selected ? 3 : 1.5, fillColor: fill, fillOpacity: 0.95 }), [selected, fill]);
  const slug = spot.slug;
  const eventHandlers = useMemo(
    () => ({
      click: () => onSelect(slug),
      // Leaflet makes markers focusable (their tooltip opens on focus): Enter or Space opens
      // the spot, as a click does.
      keydown: (e: { originalEvent: KeyboardEvent }) => {
        if (e.originalEvent.key === "Enter" || e.originalEvent.key === " ") {
          e.originalEvent.preventDefault();
          onSelect(slug);
        }
      },
    }),
    [onSelect, slug],
  );
  return (
    <CircleMarker
      center={center}
      radius={selected ? 11 : highlight ? 8 : 6}
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

/** The first view for a map of this size: one world wide, or zoom 2 where that is closer. */
function initialView(el: HTMLElement): { zoom: number; minZoom: number } {
  const r = el.getBoundingClientRect();
  const minZoom = minZoomFor(r.width, r.height);
  return { minZoom, zoom: Math.max(INITIAL_ZOOM, minZoom) };
}

/**
 * Full-screen forecast map: ocean colour field, flow particles, dark Natural Earth basemap
 * (or OpenStreetMap tiles), isobars, labels and the spot markers on top. Markers stay SVG
 * paths (`path.spot-<slug>`) coloured by our surf-quality prediction and always clickable.
 *
 * Navigation (lib/map-bounds.ts): the latitude band 84°N to 78.5°S with a hard edge, a minimum
 * zoom of one world width for the container's size, endless east-west panning with markers,
 * flights and the user's position in the world copy nearest the view.
 */
function ForecastMap(props: ForecastMapProps) {
  const { spots, markers, selected, onSelect, settings, fields, pressure, isobars, scale, particles, units, cover } = props;
  const { layer, basemap } = settings;
  const [tilesFailed, setTilesFailed] = useState(false);
  const selectedSpot = spots.find((s) => s.slug === selected);
  const user = props.userLocation;
  // The user's position changes world copy like a spot does, so it is part of the wrap key.
  const lons = useMemo(() => (user ? [...spots.map((s) => s.longitude), user.lon] : spots.map((s) => s.longitude)), [spots, user]);
  const { centreLng, onMove, onRecentre } = useWrapCentre(lons, INITIAL_CENTRE[1]);
  // Re-placed only when the position or its nearest world copy changes, as the spot markers are.
  const userLng = user ? nearestCopy(user.lon, centreLng) : null;
  const userCentre = useMemo<[number, number] | null>(() => (user && userLng != null ? [user.lat, userLng] : null), [user, userLng]);
  // The container is measured before the map exists, so it opens at its final zoom.
  const [start, setStart] = useState<{ zoom: number; minZoom: number } | null>(null);
  const measure = useCallback((el: HTMLDivElement | null) => {
    if (el) setStart((s) => s ?? initialView(el));
  }, []);
  // Leaflet's SVG renderer re-clips its paths only when a move ends; a wider margin keeps
  // markers from popping in late during long inertial pans.
  const renderer = useMemo(() => svg({ padding: 0.5 }), []);
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
    <div ref={measure} role="region" aria-label="Map of monitored surf spots" className="forecast-map absolute inset-0" style={{ isolation: "isolate" }}>
      {start && (
        <MapContainer
          center={INITIAL_CENTRE}
          zoom={start.zoom}
          minZoom={start.minZoom}
          {...BOUNDED_MAP_OPTIONS}
          // No whole-level snapping: the one-world minimum is usually fractional (2.49 at
          // 1440x807, 2.91 at 1920x987), and snapping rounded 2.91 up to 3, so zooming out
          // never got back to it. A wheel notch is still about one level; +/- step by one.
          zoomSnap={0}
          renderer={renderer}
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
            return (
              <SpotMarker
                key={s.slug}
                spot={s}
                lng={nearestCopy(s.longitude, centreLng)}
                score={m?.score ?? null}
                tooltip={m?.tooltip ?? "No usable forecast"}
                selected={s.slug === selected}
                highlight={!!m?.highlight}
                onSelect={onSelect}
                surface={basemap === "dark" ? "dark" : "light"}
              />
            );
          })}
          {userCentre && <CircleMarker center={userCentre} radius={7} pathOptions={USER_PATH} className="user-location-marker" interactive={false} />}
          <Readout sample={sample} layer={layer} units={units} />
          <FlyToSpot slug={selected} lat={selectedSpot?.latitude} lon={selectedSpot?.longitude} minZoom={SPOT_ZOOM} cover={cover} />
          <FlyToUser at={user} />
          <ZoomWatcher onZoom={props.onZoom} />
          <MaxZoom zoom={basemap === "dark" ? DARK_MAX_ZOOM : TILES.maxZoom} />
          <MapBounds onRecentre={onRecentre} />
          <CentreWatcher onCentre={onMove} />
        </MapContainer>
      )}
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

export default memo(ForecastMap);
