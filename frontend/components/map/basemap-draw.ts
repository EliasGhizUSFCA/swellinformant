/**
 * Draws the dark vector basemap layers on one canvas, bottom to top: mid-grey land (opaque, so
 * it also masks the colour field and the wave dashes at coastlines; translucent on the Wind
 * layer, where the wind over land matters) with a dark coastline drawn along its edge, country
 * borders, isobars with value labels, then country and city labels with greedy collision
 * avoidance. Geometry is pre-projected to Mercator units, so a redraw is a scale-and-offset
 * per vertex per visible world copy (the Pacific view needs the copies either side of the
 * antimeridian).
 *
 * Rings are not closed with closePath(): fill() closes every subpath itself, and the Natural
 * Earth rings repeat their first point, so the coastline stroke is unchanged. closePath()
 * after each of the ~2,500 rings cost 214 ms of a 234 ms redraw at zoom 2 (measured).
 */
import { mercX, mercY, pathCopies, placeBoxes, worldCopies, type Box, type MercPath, type PixelView, type PlaceLabel } from "@/lib/map-geo";

export interface BasemapGeometry {
  land: MercPath[];
  borders: MercPath[];
  countries: PlaceLabel[];
}

export interface CityLabel {
  name: string;
  lon: number;
  lat: number;
}

export interface IsobarPath extends MercPath {
  value: number;
}

export interface BasemapDrawOptions {
  geometry: BasemapGeometry | null;
  /** Dark basemap: land, borders and labels. Streets: only isobars (the tiles have the rest). */
  vector: boolean;
  isobars: IsobarPath[] | null;
  cities: CityLabel[] | null;
  zoom: number;
  /** Land at LAND_ALPHA over the colour field (the Wind layer) instead of opaque. */
  translucentLand?: boolean;
}

/**
 * Land on the dark basemap: the owner's reference's flat mid grey #808080 (L* 53.6, sampled at
 * every land point of the screenshot), lighter than the ocean colours of seas up to about
 * 5.5 m (L* 12 to 51.5 up to 5 m) and without their hue, so land never reads as sea. (Upgrade 4
 * first drew #3a3a3c, then #737376, L* 48.5: darker than the reference side by side.)
 */
export const LAND_COLOR = "#808080";
/** Land opacity with `translucentLand`: the wind colour and particles show through, dimmed. */
export const LAND_ALPHA = 0.55;
const TRANSLUCENT_LAND = `rgba(128, 128, 128, ${LAND_ALPHA})`;
/** The coastline drawn along the land's edge on the dark basemap: a dark line, as in the reference. */
export const COASTLINE_COLOR = "rgba(24, 24, 28, 0.8)";
export const COASTLINE_WIDTH_PX = 1;
/** Country borders on the land, a faint dark line. */
export const BORDER_COLOR = "rgba(30, 30, 34, 0.35)";
/** Country and city labels: near-white text on a dark halo, legible on the grey land and on the sea. */
const COUNTRY_LABEL_COLOR = "#e8e8e8";
const CITY_LABEL_COLOR = "#f4f4f4";
const CITY_DOT_COLOR = "#ededed";
const LABEL_FONT = '500 12px "Inter Variable", ui-sans-serif, system-ui, sans-serif';
const CITY_FONT = '400 11px "Inter Variable", ui-sans-serif, system-ui, sans-serif';
const ISOBAR_FONT = '600 10px "Inter Variable", ui-sans-serif, system-ui, sans-serif';

function visible(p: MercPath, v: PixelView, dx: number, margin = 2): boolean {
  const w = v.worldPx;
  return (
    p.maxX * w + dx - v.originX >= -margin &&
    p.minX * w + dx - v.originX <= v.width + margin &&
    p.maxY * w - v.originY >= -margin &&
    p.minY * w - v.originY <= v.height + margin
  );
}

/** Adds the paths to the current canvas path, skipping vertices closer than ~0.7 px. */
export function tracePaths(ctx: Pick<CanvasRenderingContext2D, "moveTo" | "lineTo">, paths: MercPath[], v: PixelView): void {
  const w = v.worldPx;
  for (const k of pathCopies(v)) {
    const dx = k * w;
    for (const p of paths) {
      if (!visible(p, v, dx)) continue;
      const pts = p.pts;
      let lx = pts[0]! * w + dx - v.originX;
      let ly = pts[1]! * w - v.originY;
      ctx.moveTo(lx, ly);
      for (let i = 2; i < pts.length; i += 2) {
        const x = pts[i]! * w + dx - v.originX;
        const y = pts[i + 1]! * w - v.originY;
        if (Math.abs(x - lx) + Math.abs(y - ly) < 0.7 && i < pts.length - 2) continue;
        ctx.lineTo(x, y);
        lx = x;
        ly = y;
      }
    }
  }
}

const textWidths = new Map<string, number>();
function measure(ctx: CanvasRenderingContext2D, font: string, text: string): number {
  const key = `${font}|${text}`;
  let w = textWidths.get(key);
  if (w == null) {
    ctx.font = font;
    w = ctx.measureText(text).width;
    textWidths.set(key, w);
  }
  return w;
}

function haloText(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, fill: string): void {
  ctx.strokeText(text, x, y);
  ctx.fillStyle = fill;
  ctx.fillText(text, x, y);
}

/** Draws everything; returns counts for tests and diagnostics. */
export function drawBasemap(ctx: CanvasRenderingContext2D, v: PixelView, o: BasemapDrawOptions): { isobars: number; labels: number } {
  const g = o.geometry;
  if (o.vector && g) {
    ctx.beginPath();
    tracePaths(ctx, g.land, v);
    ctx.fillStyle = o.translucentLand ? TRANSLUCENT_LAND : LAND_COLOR;
    ctx.fill("evenodd");
    ctx.strokeStyle = COASTLINE_COLOR;
    ctx.lineWidth = COASTLINE_WIDTH_PX;
    ctx.stroke();
    ctx.beginPath();
    tracePaths(ctx, g.borders, v);
    ctx.strokeStyle = BORDER_COLOR;
    ctx.lineWidth = 0.7;
    ctx.stroke();
  }

  const taken: Box[] = [];
  let isobarCount = 0;
  if (o.isobars?.length) {
    const copies = pathCopies(v);
    isobarCount = o.isobars.filter((p) => copies.some((k) => visible(p, v, k * v.worldPx))).length;
    ctx.beginPath();
    tracePaths(ctx, o.isobars, v);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.6)";
    ctx.lineWidth = 1;
    ctx.stroke();
    // Value labels along each line on a small dark pill: about every 320 px, or, zoomed out
    // to the whole world (zoom < 3), only on every second isobar (8 hPa) about every 480 px
    // and further apart, so the labels do not bury the field.
    ctx.font = ISOBAR_FONT;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const w = v.worldPx;
    const zoomedOut = w < 2048;
    const spacing = zoomedOut ? 480 : 320;
    const gap = zoomedOut ? 12 : 2;
    for (const k of pathCopies(v)) {
      const dx = k * w;
      for (const p of o.isobars) {
        if (!visible(p, v, dx)) continue;
        if (zoomedOut && Math.round(p.value) % 8 !== 0) continue;
        let run = spacing / 2; // first label half a spacing in
        for (let i = 2; i < p.pts.length; i += 2) {
          const x0 = p.pts[i - 2]! * w + dx - v.originX;
          const y0 = p.pts[i - 1]! * w - v.originY;
          const x1 = p.pts[i]! * w + dx - v.originX;
          const y1 = p.pts[i + 1]! * w - v.originY;
          run += Math.hypot(x1 - x0, y1 - y0);
          if (run < spacing) continue;
          const text = String(Math.round(p.value));
          const tw = measure(ctx, ISOBAR_FONT, text) + 8;
          const box = { x: x1 - tw / 2, y: y1 - 7, w: tw, h: 14 };
          if (placeBoxes([box], v, gap, taken).length) {
            taken.push(box);
            ctx.fillStyle = "rgba(24, 24, 24, 0.8)";
            ctx.beginPath();
            ctx.roundRect(box.x, box.y, box.w, box.h, 7);
            ctx.fill();
            ctx.fillStyle = "#f2f2f2";
            ctx.fillText(text, x1, y1 + 0.5);
            run = 0;
          }
        }
      }
    }
  }

  let labels = 0;
  if (o.vector && g) {
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "rgba(0, 0, 0, 0.6)";
    ctx.lineWidth = 3;
    const z = Math.floor(o.zoom + 1e-6);
    const w = v.worldPx;
    const candidates: Array<{ text: string; x: number; y: number; font: string; color: string; dot: boolean }> = [];
    for (const k of worldCopies(v)) {
      for (const c of g.countries) {
        if (c.minZoom > z) continue;
        candidates.push({ text: c.name, x: mercX(c.lon) * w + k * w - v.originX, y: mercY(c.lat) * w - v.originY, font: LABEL_FONT, color: COUNTRY_LABEL_COLOR, dot: false });
      }
    }
    if (o.cities && z >= 4) {
      for (const k of worldCopies(v)) {
        for (const c of o.cities) {
          candidates.push({ text: c.name, x: mercX(c.lon) * w + k * w - v.originX, y: mercY(c.lat) * w - v.originY, font: CITY_FONT, color: CITY_LABEL_COLOR, dot: true });
        }
      }
    }
    const boxes = candidates.map((c) => {
      const tw = measure(ctx, c.font, c.text);
      return c.dot ? { x: c.x - 3, y: c.y - 7, w: tw + 10, h: 14 } : { x: c.x - tw / 2, y: c.y - 8, w: tw, h: 16 };
    });
    for (const i of placeBoxes(boxes, v, 3, taken)) {
      const c = candidates[i]!;
      ctx.font = c.font;
      if (c.dot) {
        ctx.fillStyle = CITY_DOT_COLOR;
        ctx.beginPath();
        ctx.arc(c.x, c.y, 2.2, 0, Math.PI * 2);
        ctx.fill();
        // A thin dark ring (the label halo's colour), so the light dot shows on the grey land.
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.lineWidth = 3;
        ctx.textAlign = "left";
        haloText(ctx, c.text, c.x + 6, c.y, c.color);
        ctx.textAlign = "center";
      } else {
        haloText(ctx, c.text, c.x, c.y, c.color);
      }
      labels++;
    }
  }
  return { isobars: isobarCount, labels };
}
