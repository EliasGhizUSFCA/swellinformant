/**
 * A Leaflet layer that owns one <canvas> covering the map view (plus optional padding so a
 * short drag does not expose blank edges) and calls `draw` whenever the view settles.
 * While the zoom changes (animated zoom, flyTo, pinch) the last frame is CSS-scaled like
 * Leaflet's own canvas renderer.
 *
 * Used for the ocean colour field, the wave crest dashes, the particles and the dark vector
 * basemap. Leaflet's L.Canvas renderer was measured at ~165 ms per zoom step for the Natural
 * Earth land and borders with the three world copies the Pacific needs (it reprojects every
 * vertex), so the basemap is drawn here from pre-projected Mercator coordinates instead.
 */
import L from "leaflet";

import type { PixelView } from "@/lib/map-geo";

export interface CanvasLayerOptions {
  /** Pane name; created on first use with `zIndex` and no pointer events. */
  pane: string;
  zIndex: number;
  attribution?: string;
  /** Extra canvas around the view on each side, as a fraction of the view size. */
  padding?: number;
  /** CSS pixels per canvas pixel (2 = half resolution, smoothed when the browser scales it). */
  cssPxPerPixel?: () => number;
  className?: string;
  /** Settings for the 2D context (browsers ignore members they do not know). */
  contextSettings?: CanvasContextSettings;
  draw: (ctx: CanvasRenderingContext2D, view: PixelView, canvas: HTMLCanvasElement) => void;
  /** Called when the map starts moving or zooming (e.g. to pause an animation). */
  onMoveStart?: () => void;
}

/** CanvasRenderingContext2DSettings plus `colorType` (newer than this TypeScript's DOM types). */
export type CanvasContextSettings = CanvasRenderingContext2DSettings & { colorType?: "unorm8" | "float16" };

export class CanvasLayer extends L.Layer {
  canvas: HTMLCanvasElement | null = null;
  private view: PixelView | null = null;
  private zoom = 0;
  private frame = 0;
  private readonly opts: CanvasLayerOptions;

  constructor(opts: CanvasLayerOptions) {
    super();
    // L.Layer has no initialize(), so options must be set explicitly.
    L.Util.setOptions(this, { pane: opts.pane, attribution: opts.attribution });
    this.opts = opts;
  }

  override onAdd(map: L.Map): this {
    let pane = map.getPane(this.opts.pane);
    if (!pane) {
      pane = map.createPane(this.opts.pane);
      pane.style.zIndex = String(this.opts.zIndex);
      pane.style.pointerEvents = "none";
    }
    const canvas = L.DomUtil.create("canvas", `leaflet-zoom-animated ${this.opts.className ?? ""}`) as HTMLCanvasElement;
    canvas.style.pointerEvents = "none";
    canvas.style.position = "absolute";
    pane.appendChild(canvas);
    this.canvas = canvas;
    this.reset(map);
    return this;
  }

  override onRemove(): this {
    cancelAnimationFrame(this.frame);
    this.canvas?.remove();
    this.canvas = null;
    return this;
  }

  override getEvents(): Record<string, L.LeafletEventHandlerFn> {
    return {
      moveend: () => this.redraw(),
      resize: () => this.redraw(),
      viewreset: () => this.redraw(),
      zoomanim: (e) => this.animateZoom(e as L.ZoomAnimEvent),
      zoom: () => this.followZoom(),
      movestart: () => this.opts.onMoveStart?.(),
      zoomstart: () => this.opts.onMoveStart?.(),
    };
  }

  /** Redraw on the next animation frame (calls within one frame coalesce). */
  redraw(): void {
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(() => {
      if (this._map) this.reset(this._map);
    });
  }

  private reset(map: L.Map): void {
    const canvas = this.canvas;
    if (!canvas) return;
    const size = map.getSize();
    const pad = this.opts.padding ?? 0;
    const padX = Math.round(size.x * pad);
    const padY = Math.round(size.y * pad);
    const width = size.x + 2 * padX;
    const height = size.y + 2 * padY;
    const k = this.opts.cssPxPerPixel?.() ?? 1 / (window.devicePixelRatio || 1);
    const pw = Math.max(1, Math.ceil(width / k));
    const ph = Math.max(1, Math.ceil(height / k));
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    canvas.style.width = `${pw * k}px`;
    canvas.style.height = `${ph * k}px`;
    const topLeft = map.containerPointToLayerPoint([-padX, -padY]);
    L.DomUtil.setPosition(canvas, topLeft);
    const origin = topLeft.add(map.getPixelOrigin());
    this.zoom = map.getZoom();
    this.view = { originX: origin.x, originY: origin.y, width, height, worldPx: 256 * Math.pow(2, this.zoom) };
    const ctx = canvas.getContext("2d", this.opts.contextSettings);
    if (!ctx) return;
    ctx.setTransform(1 / k, 0, 0, 1 / k, 0, 0);
    ctx.clearRect(0, 0, width, height);
    this.opts.draw(ctx, this.view, canvas);
  }

  /**
   * Keeps the last frame aligned while the zoom changes frame by frame without a zoom
   * animation (flyTo, pinch zoom): scaled about its own global pixel origin, like Leaflet's
   * canvas renderer does. The next `moveend` redraws it sharp.
   */
  private followZoom(): void {
    const map = this._map;
    if (!map || !this.canvas || !this.view) return;
    const scale = map.getZoomScale(map.getZoom(), this.zoom);
    const pos = L.point(this.view.originX, this.view.originY).multiplyBy(scale).subtract(map.getPixelOrigin());
    L.DomUtil.setTransform(this.canvas, pos, scale);
  }

  private animateZoom(e: L.ZoomAnimEvent): void {
    const map = this._map;
    if (!map || !this.canvas || !this.view) return;
    const scale = map.getZoomScale(e.zoom, this.zoom);
    const half = map.getSize().divideBy(2);
    const panePos = L.DomUtil.getPosition(map.getPane("mapPane")!);
    const newOrigin = map.project(e.center, e.zoom).subtract(half).add(panePos).round();
    const pos = L.point(this.view.originX, this.view.originY).multiplyBy(scale).subtract(newOrigin);
    L.DomUtil.setTransform(this.canvas, pos, scale);
  }
}
