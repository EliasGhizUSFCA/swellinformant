"use client";

/**
 * The map's data layers inside Leaflet: the ocean colour field, the wave crest dashes (wave
 * layers), the wind particles (Wind layer, or the wind overlay on a wave layer), the basemap
 * (Natural Earth land, isobars, city labels) and the field credit in the attribution strip.
 * Each canvas lives in its own pane (order in `Z`). The map shell (forecast-map.tsx) renders
 * <MapDataLayers/> inside its MapContainer and samples its hover readout with `fieldSampler`.
 * Rendering conventions and scales: docs/map-rendering.md.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMap, useMapEvents } from "react-leaflet";

import { publishMapFlowStatus, type FieldSet } from "@/hooks/map-fields";
import { buildLut, type LayerScale } from "@/lib/map-colors";
import { directionFromUV, sampleDirection, sampleGrid, waveEnergyGrid, windSpeedGrid, type FieldGrid, type Grid } from "@/lib/map-fields";
import type { LayerId, PartitionSample, PointSample } from "@/lib/map-layers";
import { fieldPositionAt, renderField, type FieldPaint } from "@/lib/map-render";
import type { Basemap, MapSettings } from "@/lib/map-settings";
import { effectiveSystems, exaggeration, PARTITION_SYSTEMS, SYSTEM_VARIABLES, type WaveSystem } from "@/lib/map-waves";

import { loadBasemap, NATURAL_EARTH_ATTRIBUTION } from "./basemap-data";
import { drawBasemap, tracePaths, type BasemapGeometry, type CityLabel, type IsobarPath } from "./basemap-draw";
import { CanvasLayer } from "./canvas-layer";
import { ParticleAnimator, type Flow, type TrailMode } from "./particles";
import { MAX_CORNER_SPREAD_DEG, WaveAnimator, type DashTintSource, type WaveFamilyGrids, type WaveMode } from "./wavefronts";

/**
 * CSS pixels per pixel of the colour-field buffer: the field is painted at half resolution,
 * then scaled onto the canvas (one pixel per CSS pixel) with imageSmoothingQuality "high".
 */
const FIELD_PX = 2;

/** Pane order, bottom to top (Leaflet's tiles are 200, vector overlays/markers 400). The
 * basemap covers the field and the crests with opaque land (translucent on the Wind layer). */
const Z = { field: 210, waves: 220, particles: 225, basemap: 230 };

/** Wind particles: CSS px per 1/60 s per m/s (12 px/s at 1 m/s), a visual scale. */
export const WIND_PARTICLE_SPEED = 0.2;

export type ParticleMode = "animated" | "static" | "off";

// Derived grids per downloaded field set: the colour layer and the map's hover readout both
// ask for the same grid, which is computed once.
const derived = new WeakMap<FieldSet, Map<LayerId, FieldGrid | null>>();

/** The colour-layer grid for a layer, derived from the downloaded fields (wind: node speeds). */
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

/** What the colour canvas paints: a scalar grid, or for wind the (u, v) pair whose
 * interpolated vector length is painted (the same value the readout shows). */
function layerPaint(layer: LayerId, f: FieldSet | null): { grid: Grid; v: Grid | null } | null {
  if (layer === "wind") return f?.wind_u && f.wind_v ? { grid: f.wind_u, v: f.wind_v } : null;
  const g = layerGrid(layer, f);
  return g ? { grid: g, v: null } : null;
}

/** The wave systems whose three fields are in the set. */
export function systemsIn(f: FieldSet | null): Set<WaveSystem> {
  const out = new Set<WaveSystem>();
  if (!f) return out;
  for (const [system, v] of Object.entries(SYSTEM_VARIABLES) as Array<[WaveSystem, (typeof SYSTEM_VARIABLES)[WaveSystem]]>) {
    if (f[v.hs] && f[v.tp] && f[v.dir]) out.add(system);
  }
  return out;
}

/** The crest families to draw for a layer: none on the Wind layer; otherwise the chosen systems
 * that were downloaded, or the combined sea. */
export function waveFamilies(layer: LayerId, f: FieldSet | null, chosen: readonly WaveSystem[]): WaveFamilyGrids[] {
  if (layer === "wind" || !f) return [];
  return effectiveSystems(chosen, systemsIn(f)).map((system) => {
    const v = SYSTEM_VARIABLES[system];
    return { system, hs: f[v.hs]!, tp: f[v.tp]!, dir: f[v.dir]! };
  });
}

/**
 * The values under one map point for the hover readout (lib/map-layers `readout`), sampled
 * from the downloaded fields and the pressure field; null without any ocean data. Wind speed
 * is the length of the interpolated (u, v), as the colour field paints it.
 */
export function fieldSampler(layer: LayerId, fields: FieldSet | null, pressure: FieldGrid | null): ((lat: number, lon: number) => PointSample) | null {
  if (!fields && !pressure) return null;
  const grid = layerGrid(layer, fields);
  const systems = systemsIn(fields);
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
    const parts: PartitionSample[] = [];
    for (const system of PARTITION_SYSTEMS) {
      if (!systems.has(system) || !fields) continue;
      const v = SYSTEM_VARIABLES[system];
      const hs = sampleGrid(fields[v.hs]!, lat, lon);
      if (!Number.isFinite(hs)) continue;
      parts.push({ system, hs, tp: sampleGrid(fields[v.tp]!, lat, lon), dir: sampleDirection(fields[v.dir]!, lat, lon, 0.5, MAX_CORNER_SPREAD_DEG) });
    }
    if (parts.length) s.partitions = parts;
    if (pressure) s.pressure = sampleGrid(pressure, lat, lon);
    return s;
  };
}

function layerFlow(f: FieldSet | null): Flow | null {
  return f?.wind_u && f.wind_v ? { u: f.wind_u, v: f.wind_v, speed: WIND_PARTICLE_SPEED } : null;
}

/** Natural Earth land and borders, loaded once for the basemap and the ocean masks. */
function useBasemapGeometry(): BasemapGeometry | null {
  const [geometry, setGeometry] = useState<BasemapGeometry | null>(null);
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
  return geometry;
}

interface FieldCanvasProps {
  paint: { grid: Grid; v: Grid | null } | null;
  scale: LayerScale;
  /** RGBA lookup table of the scale (buildLut). */
  lut: Uint8ClampedArray;
  basemap: Basemap;
  label: string;
  at: string | null;
  layer: LayerId;
  /** Land rings to clip the wave colour off land where no opaque land is drawn (Streets). */
  clipLand: BasemapGeometry["land"] | null;
}

function FieldCanvas({ paint, scale, lut, basemap, label, at, layer, clipLand }: FieldCanvasProps) {
  const map = useMap();
  const layerRef = useRef<CanvasLayer | null>(null);
  const paintRef = useRef<FieldPaint | null>(null);
  const metaRef = useRef({ label, at, layer, clipLand });

  useEffect(() => {
    let img: ImageData | null = null;
    let buffer: HTMLCanvasElement | null = null;
    let draws = 0;
    const l = new CanvasLayer({
      pane: "fieldPane",
      zIndex: Z.field,
      padding: 0.15,
      cssPxPerPixel: () => 1,
      className: "map-field-canvas",
      draw: (ctx, view, canvas) => {
        const m = metaRef.current;
        canvas.setAttribute("role", "img");
        canvas.setAttribute("aria-label", m.label);
        canvas.dataset.layer = m.layer;
        canvas.dataset.at = m.at ?? "";
        canvas.dataset.zoom = String(Math.round(Math.log2(view.worldPx / 256) * 100) / 100);
        canvas.dataset.view = [view.originX, view.originY, view.width, view.height, view.worldPx].join(",");
        const p = paintRef.current;
        if (!p) {
          canvas.dataset.painted = "0";
          return;
        }
        const t0 = performance.now();
        const bw = Math.max(1, Math.ceil(view.width / FIELD_PX));
        const bh = Math.max(1, Math.ceil(view.height / FIELD_PX));
        buffer ??= document.createElement("canvas");
        if (buffer.width !== bw || buffer.height !== bh) {
          buffer.width = bw;
          buffer.height = bh;
        }
        const bctx = buffer.getContext("2d");
        if (!bctx) return;
        if (!img || img.width !== bw || img.height !== bh) img = bctx.createImageData(bw, bh);
        const painted = renderField(img, view, FIELD_PX, p);
        bctx.putImageData(img, 0, 0);
        // Half-resolution buffer → canvas, smoothed (the 0.5° field is smooth; this keeps it so).
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = "high";
        ctx.drawImage(buffer, 0, 0, bw * FIELD_PX, bh * FIELD_PX);
        if (m.clipLand) {
          // Wave values are 0.5° box means that reach a little inland: cut them at the coast.
          ctx.save();
          ctx.globalCompositeOperation = "destination-out";
          ctx.beginPath();
          tracePaths(ctx, m.clipLand, view);
          ctx.fillStyle = "#000";
          ctx.fill("evenodd");
          ctx.restore();
        }
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

  useEffect(() => {
    metaRef.current = { label, at, layer, clipLand };
    // On the dark basemap the field may reach slightly under the land mask (no coastal gap);
    // over street tiles only cells with real data are painted, semi-transparent.
    paintRef.current = paint ? { grid: paint.grid, v: paint.v, scale, lut, minWeight: basemap === "dark" ? 0.01 : 0.5, alpha: basemap === "dark" ? 255 : 150 } : null;
    layerRef.current?.redraw();
  }, [paint, scale, lut, basemap, label, at, layer, clipLand]);
  return null;
}

function colorTypeOf(ctx: CanvasRenderingContext2D): string {
  return (ctx.getContextAttributes?.() as { colorType?: string } | undefined)?.colorType ?? "unorm8";
}

/** Particles of the wind overlay on a wave layer, relative to the Wind layer: fewer, so the
 * crest dashes stay readable underneath. */
const OVERLAY_DENSITY = 0.45;

function ParticleCanvas({ flow, mode, overlay }: { flow: Flow | null; mode: ParticleMode; overlay: boolean }) {
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
      // 6/255: a faint white veil. A half-float canvas (where supported) fades to nothing;
      // elsewhere (Firefox, Safari, iOS) the explicit trail renderer is used instead.
      contextSettings: { colorType: "float16" },
      draw: (ctx, view, canvas) => {
        animator.stop();
        const colorType = colorTypeOf(ctx);
        const trails: TrailMode = colorType === "float16" ? "fade" : "explicit";
        canvas.dataset.mode = modeRef.current;
        canvas.dataset.colorType = colorType;
        canvas.dataset.trails = trails;
        publishMapFlowStatus({ windMode: modeRef.current, trailMode: trails });
        if (modeRef.current === "animated") animator.start(ctx, view, trails);
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
      publishMapFlowStatus({ windMode: "off", trailMode: null });
    };
  }, [map, animator]);

  useEffect(() => {
    animator.setDensity(overlay ? OVERLAY_DENSITY : 1);
    layerRef.current?.redraw();
  }, [overlay, animator]);

  // A new flow (the next forecast step) while animating keeps the particles and their trails;
  // anything else (first flow, mode change, flow gone) redraws.
  const prevMode = useRef<ParticleMode | null>(null);
  useEffect(() => {
    const next: ParticleMode = flow ? mode : "off";
    animator.setFlow(flow);
    const keep = next === "animated" && prevMode.current === "animated" && animator.isRunning;
    modeRef.current = next;
    prevMode.current = next;
    if (!keep) layerRef.current?.redraw();
  }, [flow, mode, animator]);
  return null;
}

function WaveCanvas({
  families,
  tint,
  mode,
  land,
  oceanMask,
}: {
  families: WaveFamilyGrids[];
  /** The colour field under the dashes, for their tint (null: the systems' own colours). */
  tint: DashTintSource | null;
  mode: WaveMode;
  land: BasemapGeometry["land"] | null;
  oceanMask: boolean;
}) {
  const map = useMap();
  const layerRef = useRef<CanvasLayer | null>(null);
  const [animator] = useState(() => new WaveAnimator());
  const effective: WaveMode = families.length ? mode : "off";
  const modeRef = useRef(effective);
  const familiesRef = useRef(families);

  useEffect(() => {
    let frames = 0;
    const l = new CanvasLayer({
      pane: "wavePane",
      zIndex: Z.waves,
      cssPxPerPixel: () => 1 / Math.min(2, window.devicePixelRatio || 1),
      className: "map-wave-canvas",
      draw: (ctx, view, canvas) => {
        canvas.setAttribute("aria-hidden", "true");
        canvas.dataset.style = "dashes";
        canvas.dataset.mode = modeRef.current;
        canvas.dataset.families = familiesRef.current.map((f) => f.system).join(",");
        canvas.dataset.view = [view.originX, view.originY, view.width, view.height, view.worldPx].join(",");
        const zoom = Math.log2(view.worldPx / 256);
        const centre = map.getCenter();
        canvas.dataset.exaggeration = String(Math.round(exaggeration(zoom, centre.lat)));
        if (modeRef.current === "off") {
          canvas.dataset.crests = "0";
          animator.stop();
          return;
        }
        animator.start(ctx, view, modeRef.current);
      },
      // The last frame moves with the map while it is dragged; the animation resumes, with the
      // same clock, when the map settles.
      onMoveStart: () => animator.stop(),
    });
    animator.setListener((s) => {
      const c = l.canvas;
      if (!c) return;
      frames++;
      c.dataset.crests = String(s.crests);
      c.dataset.quads = String(s.quads);
      c.dataset.quadCap = String(s.quadCap);
      c.dataset.vertices = String(s.vertices);
      c.dataset.strokes = String(s.strokes);
      c.dataset.dashTrails = String(s.trails);
      c.dataset.drawn = s.families.join(",");
      if (s.arrows) c.dataset.arrows = String(s.arrows);
      if (frames % 10 === 1 || modeRef.current === "static") {
        c.dataset.drawMs = s.drawMs.toFixed(2);
        c.dataset.drawMsP50 = s.drawMsP50.toFixed(2);
        c.dataset.drawMsP95 = s.drawMsP95.toFixed(2);
        c.dataset.latticeMs = s.latticeMs.toFixed(1);
        c.dataset.frames = String(s.frames);
      }
    });
    l.addTo(map);
    layerRef.current = l;
    return () => {
      animator.stop();
      animator.setListener(null);
      l.remove();
      layerRef.current = null;
    };
  }, [map, animator]);

  useEffect(() => {
    animator.setLand(land);
  }, [animator, land]);

  useEffect(() => {
    animator.setOceanMask(oceanMask);
    layerRef.current?.canvas?.setAttribute("data-ocean-mask", oceanMask ? "1" : "0");
  }, [animator, oceanMask]);

  // New fields keep the live dashes and the clock: the lattice is rebuilt for the same view.
  useEffect(() => {
    familiesRef.current = families;
    animator.setFamilies(families, tint);
    layerRef.current?.canvas?.setAttribute("data-families", families.map((f) => f.system).join(","));
  }, [families, tint, animator]);

  useEffect(() => {
    const changed = modeRef.current !== effective;
    modeRef.current = effective;
    if (changed || !animator.isRunning) layerRef.current?.redraw();
  }, [effective, animator]);
  return null;
}

function BasemapCanvas({
  vector,
  isobars,
  cities,
  geometry,
  translucentLand,
}: {
  vector: boolean;
  isobars: IsobarPath[] | null;
  cities: CityLabel[] | null;
  geometry: BasemapGeometry | null;
  translucentLand: boolean;
}) {
  const map = useMap();
  const layerRef = useRef<CanvasLayer | null>(null);
  const stateRef = useRef({ vector, isobars, cities, geometry, translucentLand });

  useEffect(() => {
    const l = new CanvasLayer({
      pane: "basemapPane",
      zIndex: Z.basemap,
      padding: 0.15,
      className: "map-basemap-canvas",
      attribution: vector ? NATURAL_EARTH_ATTRIBUTION : undefined,
      draw: (ctx, view, canvas) => {
        const s = stateRef.current;
        const t0 = performance.now();
        const r = drawBasemap(ctx, view, { geometry: s.geometry, vector: s.vector, isobars: s.isobars, cities: s.cities, zoom: map.getZoom(), translucentLand: s.translucentLand });
        canvas.dataset.drawMs = (performance.now() - t0).toFixed(1);
        canvas.dataset.isobars = String(r.isobars);
        canvas.dataset.labels = String(r.labels);
        canvas.dataset.land = s.vector && s.geometry ? "1" : "0";
        canvas.dataset.landAlpha = s.translucentLand ? "translucent" : "opaque";
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
    stateRef.current = { vector, isobars, cities, geometry, translucentLand };
    layerRef.current?.redraw();
  }, [vector, isobars, cities, geometry, translucentLand]);
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

/** Publishes the zoom and centre latitude (for the legend's stated crest exaggeration). */
function ViewStatus() {
  const map = useMapEvents({
    zoomend: () => publish(),
    moveend: () => publish(),
  });
  const publish = () => publishMapFlowStatus({ zoom: map.getZoom(), centreLat: map.getCenter().lat });
  useEffect(() => {
    publishMapFlowStatus({ zoom: map.getZoom(), centreLat: map.getCenter().lat });
  }, [map]);
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

/**
 * Every data canvas of the map, in pane order, plus the field credit. Wave layers draw
 * crest dashes (never particles); the Wind layer draws particles, and so does a wave layer
 * with the wind overlay on. `particles` (animated / static / off) applies to both.
 */
export function MapDataLayers(props: MapDataLayersProps) {
  const { layer, fields, particles, basemap, scale, settings } = props;
  const geometry = useBasemapGeometry();
  const paint = useMemo(() => layerPaint(layer, fields), [layer, fields]);
  const lut = useMemo(() => buildLut(scale.stops), [scale.stops]);
  // The crest dashes are tinted from the colour field under them (the same values and colours).
  const tint = useMemo<DashTintSource | null>(
    () => (paint ? { position: (lat, lon) => fieldPositionAt({ grid: paint.grid, v: paint.v, scale, minWeight: 0.5 }, lat, lon), lut } : null),
    [paint, scale, lut],
  );
  const windParticles = layer === "wind" || settings.windOverlay;
  const flow = useMemo(() => (windParticles ? layerFlow(fields) : null), [windParticles, fields]);
  const families = useMemo(() => waveFamilies(layer, fields, settings.waveSystems), [layer, fields, settings.waveSystems]);
  const land = geometry?.land ?? null;
  const waveMode: WaveMode = particles;

  useEffect(() => {
    publishMapFlowStatus({ systems: families.map((f) => f.system), waveMode: families.length ? waveMode : "off", validTime: props.fieldsAt ?? null });
  }, [families, waveMode, props.fieldsAt]);

  return (
    <>
      <FieldCanvas
        paint={paint}
        scale={scale}
        lut={lut}
        basemap={basemap}
        label={props.fieldLabel ?? ""}
        at={props.fieldsAt ?? null}
        layer={layer}
        clipLand={basemap === "streets" && layer !== "wind" ? land : null}
      />
      {layer !== "wind" && <WaveCanvas families={families} tint={tint} mode={waveMode} land={land} oceanMask={basemap !== "dark"} />}
      {windParticles && <ParticleCanvas flow={flow} mode={particles} overlay={layer !== "wind"} />}
      <BasemapCanvas
        vector={basemap === "dark"}
        isobars={props.isobarPaths ?? null}
        cities={basemap === "dark" ? (props.cities ?? null) : null}
        geometry={geometry}
        translucentLand={layer === "wind"}
      />
      <ViewStatus />
      <AttributionText text={fields && props.fieldAttribution ? escapeHtml(props.fieldAttribution) : undefined} />
    </>
  );
}
