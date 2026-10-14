"use client";

/**
 * The map's data layers inside Leaflet: the ocean colour field, the flow particles, the
 * basemap (Natural Earth land, isobars, city labels) and the field credit in the attribution
 * strip. Each canvas lives in its own pane (order in `Z`). The map shell (forecast-map.tsx)
 * renders <MapDataLayers/> inside its MapContainer and samples its hover readout with
 * `fieldSampler`.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMap } from "react-leaflet";

import { buildLut, type LayerScale } from "@/lib/map-colors";
import { directionFromUV, sampleDirection, sampleGrid, waveEnergyGrid, waveFlowGrids, windSpeedGrid, type FieldGrid } from "@/lib/map-fields";
import type { LayerId, PointSample } from "@/lib/map-layers";
import { renderField, type FieldPaint } from "@/lib/map-render";
import type { Basemap, MapSettings } from "@/lib/map-settings";
import type { FieldSet } from "@/hooks/map-fields";

import { loadBasemap, NATURAL_EARTH_ATTRIBUTION } from "./basemap-data";
import { drawBasemap, type BasemapGeometry, type CityLabel, type IsobarPath } from "./basemap-draw";
import { CanvasLayer } from "./canvas-layer";
import { ParticleAnimator, type Flow } from "./particles";

/** CSS pixels per colour-field pixel: half resolution at 1x, smoothed by the browser. */
const FIELD_PX = 2;

/** Pane order, bottom to top (Leaflet's tiles are 200, vector overlays/markers 400). */
const Z = { field: 210, particles: 220, basemap: 230 };

export type ParticleMode = "animated" | "static" | "off";

// Derived grids per downloaded field set: the colour layer and the map's hover readout both
// ask for the same grid, which is computed once.
const derived = new WeakMap<FieldSet, Map<LayerId, FieldGrid | null>>();

/** The colour-layer grid for a layer, derived from the downloaded fields. */
export function layerGrid(layer: LayerId, f: FieldSet | null): FieldGrid | null {
  if (!f) return null;
  if (layer === "hs") return f.hs ?? null;
  if (layer === "tp") return f.tp ?? null;
  let cache = derived.get(f);
  if (!cache) derived.set(f, (cache = new Map()));
  if (!cache.has(layer)) {
    if (layer === "energy") cache.set(layer, f.hs && f.tp ? waveEnergyGrid(f.hs, f.tp) : null);
    else cache.set(layer, f.wind_u && f.wind_v ? windSpeedGrid(f.wind_u, f.wind_v) : null);
  }
  return cache.get(layer) ?? null;
}

/**
 * The values under one map point for the hover readout (lib/map-layers `readout`), sampled
 * from the downloaded fields and the pressure field; null without any ocean data.
 */
export function fieldSampler(layer: LayerId, fields: FieldSet | null, pressure: FieldGrid | null): ((lat: number, lon: number) => PointSample) | null {
  if (!fields && !pressure) return null;
  const grid = layerGrid(layer, fields);
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
        canvas.dataset.zoom = String(Math.round(Math.log2(view.worldPx / 256) * 100) / 100);
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
      // The trails fade by dimming the previous frame, and 8-bit alpha stops decaying at
      // 6/255: a faint white veil that covered a quarter of the canvas after ten seconds of
      // live NOAA winds. A half-float canvas (where supported) fades to nothing.
      contextSettings: { colorType: "float16" },
      draw: (ctx, view, canvas) => {
        animator.stop();
        canvas.dataset.mode = modeRef.current;
        canvas.dataset.colorType = (ctx.getContextAttributes?.() as { colorType?: string } | undefined)?.colorType ?? "unorm8";
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

export interface MapDataLayersProps {
  layer: LayerId;
  /** Fields for the active layer at `fieldsAt` (null = no ocean data). */
  fields: FieldSet | null;
  pressure: FieldGrid | null;
  settings: MapSettings;
  particles: ParticleMode;
  basemap: Basemap;
  /** Colour scale of the active layer (from the field index where it gives a range). */
  scale: LayerScale;
  /** Accessible description of the colour layer and time, e.g. "Offshore wave height, 8am Sun PDT". */
  fieldLabel?: string;
  fieldsAt?: string | null;
  cities?: CityLabel[] | null;
  isobarPaths?: IsobarPath[] | null;
  /** Short credit for the ocean layers in Leaflet's attribution strip (plain text, escaped here). */
  fieldAttribution?: string | null;
}

/** Every data canvas of the map, in pane order, plus the field credit. */
export function MapDataLayers(props: MapDataLayersProps) {
  const { layer, fields, particles, basemap, scale } = props;
  const grid = useMemo(() => layerGrid(layer, fields), [layer, fields]);
  const flow = useMemo(() => layerFlow(layer, fields), [layer, fields]);
  return (
    <>
      <FieldCanvas grid={grid} scale={scale} basemap={basemap} label={props.fieldLabel ?? ""} at={props.fieldsAt ?? null} layer={layer} />
      <ParticleCanvas flow={flow} mode={particles} />
      <BasemapCanvas vector={basemap === "dark"} isobars={props.isobarPaths ?? null} cities={basemap === "dark" ? (props.cities ?? null) : null} />
      <AttributionText text={fields && props.fieldAttribution ? escapeHtml(props.fieldAttribution) : undefined} />
    </>
  );
}
