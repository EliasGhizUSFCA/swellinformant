"use client";

import "leaflet/dist/leaflet.css";

import type { Map as LeafletMap } from "leaflet";
import { useEffect, useMemo, useRef, useState } from "react";
import { CircleMarker, MapContainer, TileLayer, Tooltip, useMap, useMapEvents, ZoomControl } from "react-leaflet";

import { buildLut, type LayerScale } from "@/lib/map-colors";
import {
  directionFromUV,
  sampleDirection,
  sampleGrid,
  waveEnergyGrid,
  waveFlowGrids,
  windSpeedGrid,
  type FieldGrid,
} from "@/lib/map-fields";
import { mercX, mercY, type IsoLine } from "@/lib/map-geo";
import { readout, type DisplayUnits, type LayerId, type PointSample } from "@/lib/map-layers";
import { renderField, type FieldPaint } from "@/lib/map-render";
import type { Basemap } from "@/lib/map-settings";
import { tileConfig } from "@/lib/map-tiles";
import { colorForScore } from "@/lib/quality";
import type { FieldSet } from "@/hooks/map-fields";
import type { SpotSummary } from "@/types/api";

import { loadBasemap, NATURAL_EARTH_ATTRIBUTION } from "./basemap-data";
import { drawBasemap, type BasemapGeometry, type CityLabel, type IsobarPath } from "./basemap-draw";
import { CanvasLayer } from "./canvas-layer";
import { ParticleAnimator, type Flow } from "./particles";
import type { MarkerState } from "./surf-map";

const TILES = tileConfig(process.env.NEXT_PUBLIC_MAP_TILE_URL, process.env.NEXT_PUBLIC_MAP_ATTRIBUTION, process.env.NEXT_PUBLIC_CARTO_API_KEY);
const FAILED_TILES_BEFORE_NOTICE = 4;
/** CSS pixels per colour-field pixel: half resolution at 1x, smoothed by the browser. */
const FIELD_PX = 2;
const OCEAN_BACKGROUND = "#151a21";

/** Natural Earth 1:50m outlines turn visibly angular beyond this zoom; Streets go further. */
const DARK_MAX_ZOOM = 8;

/** Pane order, bottom to top (Leaflet's tiles are 200, vector overlays/markers 400). */
const Z = { field: 210, particles: 220, basemap: 230 };

export type ParticleMode = "animated" | "static" | "off";

/** The colour-layer grid for a layer, derived from the downloaded fields. */
export function layerGrid(layer: LayerId, f: FieldSet | null): FieldGrid | null {
  if (!f) return null;
  if (layer === "hs") return f.hs ?? null;
  if (layer === "tp") return f.tp ?? null;
  if (layer === "energy") return f.hs && f.tp ? waveEnergyGrid(f.hs, f.tp) : null;
  return f.wind_u && f.wind_v ? windSpeedGrid(f.wind_u, f.wind_v) : null;
}

function layerFlow(layer: LayerId, f: FieldSet | null): Flow | null {
  if (!f) return null;
  if (layer === "wind") return f.wind_u && f.wind_v ? { u: f.wind_u, v: f.wind_v, speed: 0.2 } : null;
  if (!f.wave_dir || !f.tp) return null;
  return { ...waveFlowGrids(f.wave_dir, f.tp), speed: 1.1 };
}

function FieldCanvas({ grid, scale, basemap, label, at, layer }: { grid: FieldGrid | null; scale: LayerScale; basemap: Basemap; label: string; at: string | null; layer: LayerId }) {
  const map = useMap();
  const layerRef = useRef<CanvasLayer | null>(null);
  const paintRef = useRef<FieldPaint | null>(null);
  const metaRef = useRef({ label, at, layer });

  useEffect(() => {
    let img: ImageData | null = null;
    let draws = 0;
    const l = new CanvasLayer({
      pane: "fieldPane",
      zIndex: Z.field,
      padding: 0.15,
      cssPxPerPixel: () => FIELD_PX,
      className: "map-field-canvas",
      draw: (ctx, view, canvas) => {
        canvas.setAttribute("role", "img");
        canvas.setAttribute("aria-label", metaRef.current.label);
        canvas.dataset.layer = metaRef.current.layer;
        canvas.dataset.at = metaRef.current.at ?? "";
        const p = paintRef.current;
        if (!p) {
          canvas.dataset.painted = "0";
          return;
        }
        const t0 = performance.now();
        if (!img || img.width !== canvas.width || img.height !== canvas.height) img = ctx.createImageData(canvas.width, canvas.height);
        const painted = renderField(img, view, FIELD_PX, p);
        ctx.putImageData(img, 0, 0);
        draws += 1;
        canvas.dataset.drawMs = (performance.now() - t0).toFixed(1);
        canvas.dataset.painted = String(painted);
        canvas.dataset.draws = String(draws);
      },
    });
    l.addTo(map);
    layerRef.current = l;
    return () => {
      l.remove();
      layerRef.current = null;
    };
  }, [map]);

  const lut = useMemo(() => buildLut(scale.stops), [scale.stops]);
  useEffect(() => {
    metaRef.current = { label, at, layer };
    // On the dark basemap the field may reach slightly under the land mask (no coastal gap);
    // over street tiles only cells with real data are painted, semi-transparent.
    paintRef.current = grid ? { grid, scale, lut, minWeight: basemap === "dark" ? 0.01 : 0.5, alpha: basemap === "dark" ? 255 : 150 } : null;
    layerRef.current?.redraw();
  }, [grid, scale, lut, basemap, label, at, layer]);
  return null;
}

function ParticleCanvas({ flow, mode }: { flow: Flow | null; mode: ParticleMode }) {
  const map = useMap();
  const layerRef = useRef<CanvasLayer | null>(null);
  const modeRef = useRef(mode);
  const [animator] = useState(() => new ParticleAnimator());

  useEffect(() => {
    const l = new CanvasLayer({
      pane: "particlePane",
      zIndex: Z.particles,
      cssPxPerPixel: () => 1 / Math.min(2, window.devicePixelRatio || 1),
      className: "map-particle-canvas",
      draw: (ctx, view, canvas) => {
        animator.stop();
        canvas.dataset.mode = modeRef.current;
        if (modeRef.current === "animated") animator.start(ctx, view);
        else if (modeRef.current === "static") canvas.dataset.arrows = String(animator.drawArrows(ctx, view));
      },
      onMoveStart: () => {
        animator.stop();
        const c = layerRef.current?.canvas;
        const ctx = c?.getContext("2d");
        if (c && ctx) {
          ctx.save();
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.clearRect(0, 0, c.width, c.height);
          ctx.restore();
        }
      },
    });
    l.addTo(map);
    layerRef.current = l;
    return () => {
      animator.stop();
      l.remove();
      layerRef.current = null;
    };
  }, [map, animator]);

  useEffect(() => {
    modeRef.current = flow ? mode : "off";
    animator.setFlow(flow);
    layerRef.current?.redraw();
  }, [flow, mode, animator]);
  return null;
}

function BasemapCanvas({ vector, isobars, cities }: { vector: boolean; isobars: IsobarPath[] | null; cities: CityLabel[] | null }) {
  const map = useMap();
  const layerRef = useRef<CanvasLayer | null>(null);
  const [geometry, setGeometry] = useState<BasemapGeometry | null>(null);
  const stateRef = useRef({ vector, isobars, cities, geometry });

  useEffect(() => {
    let cancelled = false;
    loadBasemap().then(
      (g) => !cancelled && setGeometry(g),
      () => undefined, // without the land data the map still shows the field and markers
    );
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const l = new CanvasLayer({
      pane: "basemapPane",
      zIndex: Z.basemap,
      padding: 0.15,
      className: "map-basemap-canvas",
      attribution: vector ? NATURAL_EARTH_ATTRIBUTION : undefined,
      draw: (ctx, view, canvas) => {
        const s = stateRef.current;
        const r = drawBasemap(ctx, view, { geometry: s.geometry, vector: s.vector, isobars: s.isobars, cities: s.cities, zoom: map.getZoom() });
        canvas.dataset.isobars = String(r.isobars);
        canvas.dataset.labels = String(r.labels);
        canvas.dataset.land = s.vector && s.geometry ? "1" : "0";
      },
    });
    l.addTo(map);
    layerRef.current = l;
    return () => {
      l.remove();
      layerRef.current = null;
    };
  }, [map, vector]);

  useEffect(() => {
    stateRef.current = { vector, isobars, cities, geometry };
    layerRef.current?.redraw();
  }, [vector, isobars, cities, geometry]);
  return null;
}

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
  basemap: Basemap;
  layer: LayerId;
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
  fieldAttribution?: string;
}

function ZoomWatcher({ onZoom }: { onZoom?: (z: number) => void }) {
  const map = useMapEvents({ zoomend: () => onZoom?.(map.getZoom()) });
  return null;
}

function AttributionText({ text }: { text?: string }) {
  const map = useMap();
  useEffect(() => {
    if (!text) return;
    const control = map.attributionControl;
    control?.addAttribution(text);
    return () => {
      control?.removeAttribution(text);
    };
  }, [map, text]);
  return null;
}

const escapeHtml = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/**
 * Full-screen forecast map: ocean colour field, flow particles, dark Natural Earth basemap
 * (or OpenStreetMap tiles), isobars, labels and the spot markers on top. Markers stay SVG
 * paths (`path.spot-<slug>`) coloured by our surf-quality prediction and always clickable.
 */
export default function ForecastMap(props: ForecastMapProps) {
  const { spots, markers, selected, onSelect, basemap, layer, fields, pressure, isobars, scale, particles, units } = props;
  const [tilesFailed, setTilesFailed] = useState(false);
  const selectedSpot = spots.find((s) => s.slug === selected);
  const grid = useMemo(() => layerGrid(layer, fields), [layer, fields]);
  const flow = useMemo(() => layerFlow(layer, fields), [layer, fields]);
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

  const sample = useMemo(() => {
    if (!fields && !pressure) return null;
    return (lat: number, lon: number): PointSample => {
      const s: PointSample = {};
      if (fields?.hs) s.hs = sampleGrid(fields.hs, lat, lon);
      if (fields?.tp) s.tp = sampleGrid(fields.tp, lat, lon);
      if (fields?.wave_dir) s.waveDir = sampleDirection(fields.wave_dir, lat, lon);
      if (grid && layer === "energy") s.energy = sampleGrid(grid, lat, lon);
      if (fields?.wind_u && fields?.wind_v) {
        const u = sampleGrid(fields.wind_u, lat, lon);
        const v = sampleGrid(fields.wind_v, lat, lon);
        s.windSpeed = Math.hypot(u, v);
        s.windDir = directionFromUV(u, v);
      }
      if (pressure) s.pressure = sampleGrid(pressure, lat, lon);
      return s;
    };
  }, [fields, pressure, grid, layer]);

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
        <FieldCanvas grid={grid} scale={scale} basemap={basemap} label={props.fieldLabel} at={props.fieldsAt} layer={layer} />
        <ParticleCanvas flow={flow} mode={particles} />
        <BasemapCanvas vector={basemap === "dark"} isobars={isobarPaths} cities={basemap === "dark" ? props.cities : null} />
        <AttributionText text={fields && props.fieldAttribution ? escapeHtml(props.fieldAttribution) : undefined} />
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
              eventHandlers={{ click: () => onSelect(s.slug) }}
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
