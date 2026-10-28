/**
 * The spot markers' direction arrows and height labels (spec 23): geometry and the
 * de-cluttering rule, kept free of Leaflet and React so they can be tested on their own.
 *
 * Conventions (lib/directions.ts, the same as the spot charts and the map's particles): the
 * API's directions are degrees FROM; an arrow points where the swell or the wind is GOING,
 * so it is drawn rotated by FROM + 180 degrees, starting at the marker's edge.
 *
 * De-cluttering (`declutter`): at world zoom many spots sit a few pixels apart, so
 * * a marker's arrows are drawn only when no other marker lies within ARROW_CLEAR_PX of it
 *   (the selected spot always keeps them);
 * * height labels are placed in priority order (the selected spot, then the best rated), on
 *   the first side (right, left, below, above) whose box hits neither the marker's own
 *   arrows, another marker, another marker's arrows nor a label already placed; a label
 *   with no free side is left out, and labels only appear from LABEL_MIN_ZOOM in.
 * Everything hidden stays in the marker's tooltip and the spot panel.
 */
import { arrowRotationDeg } from "@/lib/directions";

/** Gap (px) between the marker's edge and the start of its arrows. */
export const ARROW_GAP = 1.5;
/** Arrow lengths beyond the gap (px): the swell arrow is the longer, filled one. */
export const SWELL_ARROW_LEN = 15;
export const WIND_ARROW_LEN = 12;
/** No other marker within this distance (px, centre to centre): the arrows are drawn. */
export const ARROW_CLEAR_PX = 26;
/** Height labels from this zoom in (the one-world view is about 2.5 on a desktop screen). */
export const LABEL_MIN_ZOOM = 3;
/** Label box: text height (px) and padding; the width is estimated from the text. */
export const LABEL_H = 15;
const LABEL_GAP = 3;
/** Rough width (px) of the label text at 10.5px Inter semibold, plus padding. */
export const labelWidth = (text: string) => Math.ceil(text.length * 6.1 + 9);

export interface Point {
  x: number;
  y: number;
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type LabelSide = "right" | "left" | "below" | "above";

/** One marker as the de-cluttering sees it: its projected position (px at the current zoom),
 * its radius, the arrows it would draw (clockwise degrees from north, already turned to
 * point where things go) and its label text. */
export interface GlyphInput {
  key: string;
  x: number;
  y: number;
  radius: number;
  /** Larger first; the selected spot should carry the largest. */
  priority: number;
  selected?: boolean;
  swellRot: number | null;
  windRot: number | null;
  label: string | null;
}

export interface GlyphLayout {
  arrows: boolean;
  label: LabelSide | null;
}

const norm = (deg: number) => ((deg % 360) + 360) % 360;

/** Rotation (clockwise degrees from north) of an arrow for something coming FROM `fromDeg`,
 * or null without a usable direction. */
export function glyphRotation(fromDeg: number | null | undefined): number | null {
  return fromDeg == null || !Number.isFinite(fromDeg) ? null : arrowRotationDeg(norm(fromDeg));
}

/** The arrow's start and tip relative to the marker centre (screen px, y down). */
export function arrowSegment(rotDeg: number, radius: number, len: number): [Point, Point] {
  const t = (rotDeg * Math.PI) / 180;
  const dx = Math.sin(t);
  const dy = -Math.cos(t);
  const a = radius + ARROW_GAP;
  const b = a + len;
  return [
    { x: dx * a, y: dy * a },
    { x: dx * b, y: dy * b },
  ];
}

/** The label's box relative to the marker centre, on `side`. */
export function labelBox(side: LabelSide, radius: number, width: number): Box {
  const h = LABEL_H;
  switch (side) {
    case "right":
      return { x: radius + LABEL_GAP, y: -h / 2, w: width, h };
    case "left":
      return { x: -radius - LABEL_GAP - width, y: -h / 2, w: width, h };
    case "below":
      return { x: -width / 2, y: radius + LABEL_GAP, w: width, h };
    case "above":
      return { x: -width / 2, y: -radius - LABEL_GAP - h, w: width, h };
  }
}

const SIDES: readonly LabelSide[] = ["right", "left", "below", "above"];

export function boxesOverlap(a: Box, b: Box): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/** Whether the segment p-q passes through box `b` (sampled every 2 px, ends included). */
export function segmentHitsBox(p: Point, q: Point, b: Box): boolean {
  const n = Math.max(2, Math.ceil(Math.hypot(q.x - p.x, q.y - p.y) / 2));
  for (let i = 0; i <= n; i++) {
    const x = p.x + ((q.x - p.x) * i) / n;
    const y = p.y + ((q.y - p.y) * i) / n;
    if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) return true;
  }
  return false;
}

const shift = (b: Box, o: Point, pad = 0): Box => ({ x: b.x + o.x - pad, y: b.y + o.y - pad, w: b.w + 2 * pad, h: b.h + 2 * pad });
const add = (p: Point, o: Point): Point => ({ x: p.x + o.x, y: p.y + o.y });

/** Absolute arrow segments of a marker (only the arrows it has a direction for). */
function arrowsOf(g: GlyphInput): Array<[Point, Point]> {
  const out: Array<[Point, Point]> = [];
  if (g.swellRot != null) out.push(arrowSegment(g.swellRot, g.radius, SWELL_ARROW_LEN));
  if (g.windRot != null) out.push(arrowSegment(g.windRot, g.radius, WIND_ARROW_LEN));
  return out.map(([p, q]) => [add(p, g), add(q, g)]);
}

/** Which markers draw arrows and where each label goes (see the module docstring). */
export function declutter(items: readonly GlyphInput[], zoom: number): Map<string, GlyphLayout> {
  const out = new Map<string, GlyphLayout>();
  const arrows = new Map<string, Array<[Point, Point]>>();
  for (const g of items) {
    const crowded = items.some((o) => o.key !== g.key && Math.hypot(o.x - g.x, o.y - g.y) < ARROW_CLEAR_PX);
    const has = (g.swellRot != null || g.windRot != null) && (g.selected || !crowded);
    if (has) arrows.set(g.key, arrowsOf(g));
    out.set(g.key, { arrows: has, label: null });
  }
  if (zoom < LABEL_MIN_ZOOM) return out;

  const circles = items.map((g) => ({ key: g.key, box: { x: g.x - g.radius - 1, y: g.y - g.radius - 1, w: 2 * g.radius + 2, h: 2 * g.radius + 2 } }));
  const placed: Box[] = [];
  const order = [...items].filter((g) => g.label).sort((a, b) => b.priority - a.priority || a.key.localeCompare(b.key));
  for (const g of order) {
    const width = labelWidth(g.label!);
    for (const side of SIDES) {
      const box = shift(labelBox(side, g.radius, width), g);
      const hit = shift(box, { x: 0, y: 0 }, 1);
      if (placed.some((b) => boxesOverlap(b, hit))) continue;
      if (circles.some((c) => c.key !== g.key && boxesOverlap(c.box, hit))) continue;
      // Every drawn arrow, this marker's own included.
      if ([...arrows.values()].some((segs) => segs.some(([p, q]) => segmentHitsBox(p, q, hit)))) continue;
      placed.push(box);
      out.set(g.key, { arrows: out.get(g.key)!.arrows, label: side });
      break;
    }
  }
  return out;
}
