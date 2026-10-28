"use client";

import { divIcon } from "leaflet";
import { memo, useMemo, useState } from "react";
import { Marker, useMap, useMapEvents } from "react-leaflet";

import { nearestCopy } from "@/lib/map-bounds";
import { arrowSegment, declutter, glyphRotation, labelBox, labelWidth, SWELL_ARROW_LEN, WIND_ARROW_LEN, type GlyphLayout, type LabelSide } from "@/lib/map-markers";
import type { SpotSummary } from "@/types/api";

import type { MarkerState } from "./surf-map";

/** Half the glyph's box (px): room for the largest marker and its longest arrow. */
const HALF = 32;

/** The marker radius SpotMarker draws for these states (forecast-map.tsx). */
export function markerRadius(selected: boolean, highlight: boolean): number {
  return selected ? 11 : highlight ? 8 : 6;
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const f = (v: number) => Math.round(v * 100) / 100;

/** The swell arrow (filled shaft and head) and the wind arrow (a line with an open head),
 * both drawn pointing north from the marker's edge and rotated into place. */
function swellPath(radius: number): string {
  const [a, b] = arrowSegment(0, radius, SWELL_ARROW_LEN).map((p) => p.y) as [number, number];
  const head = 6.5;
  return `M0,${f(b)}L4.4,${f(b + head)}L1.4,${f(b + head)}L1.4,${f(a)}L-1.4,${f(a)}L-1.4,${f(b + head)}L-4.4,${f(b + head)}Z`;
}

function windPath(radius: number): string {
  const [a, b] = arrowSegment(0, radius, WIND_ARROW_LEN).map((p) => p.y) as [number, number];
  return `M0,${f(a)}L0,${f(b)}M-3.2,${f(b + 4)}L0,${f(b)}L3.2,${f(b + 4)}`;
}

/** The glyph's HTML (an SVG of the arrows, the stale ring and the label). Exported for tests. */
export function glyphHtml({
  slug,
  radius,
  swellRot,
  windRot,
  label,
  side,
  stale,
}: {
  slug: string;
  radius: number;
  swellRot: number | null;
  windRot: number | null;
  label: string | null;
  side: LabelSide | null;
  stale: boolean;
}): string {
  const parts: string[] = [];
  if (stale) parts.push(`<circle class="sg-stale" r="${radius + 3}" />`);
  if (swellRot != null) parts.push(`<path class="sg-swell" d="${swellPath(radius)}" transform="rotate(${f(swellRot)})" />`);
  if (windRot != null) {
    const d = windPath(radius);
    parts.push(`<g transform="rotate(${f(windRot)})"><path class="sg-wind-halo" d="${d}" /><path class="sg-wind" d="${d}" /></g>`);
  }
  let labelHtml = "";
  if (label && side) {
    const box = labelBox(side, radius, labelWidth(label));
    labelHtml = `<span class="sg-label${stale ? " sg-label-stale" : ""}" data-side="${side}" style="left:${f(HALF + box.x)}px;top:${f(HALF + box.y)}px;width:${f(box.w)}px">${esc(label)}</span>`;
  }
  const attrs = [
    `data-testid="spot-glyph"`,
    `data-slug="${esc(slug)}"`,
    swellRot != null ? `data-swell-rot="${f(swellRot)}"` : "",
    windRot != null ? `data-wind-rot="${f(windRot)}"` : "",
    side ? `data-label-side="${side}"` : "",
    stale ? `data-stale=""` : "",
  ]
    .filter(Boolean)
    .join(" ");
  const svgPart = parts.length ? `<svg width="${2 * HALF}" height="${2 * HALF}" viewBox="${-HALF} ${-HALF} ${2 * HALF} ${2 * HALF}" aria-hidden="true" focusable="false">${parts.join("")}</svg>` : "";
  return `<div class="spot-glyph" ${attrs}>${svgPart}${labelHtml}</div>`;
}

/**
 * One spot's arrows and label: a non-interactive Leaflet marker above the spot's circle
 * (clicks, focus and the tooltip stay with the circle, SpotMarker). Its icon is rebuilt only
 * when what it shows changes.
 */
const SpotGlyph = memo(function SpotGlyph({ lat, lng, html }: { lat: number; lng: number; html: string }) {
  const position = useMemo<[number, number]>(() => [lat, lng], [lat, lng]);
  const icon = useMemo(() => divIcon({ html, className: "spot-glyph-icon", iconSize: [2 * HALF, 2 * HALF], iconAnchor: [HALF, HALF] }), [html]);
  return <Marker position={position} icon={icon} interactive={false} keyboard={false} />;
});

/**
 * The direction arrows (the swell that drives the surf and the wind, both pointing where they
 * go) and the predicted surf-height labels of the visible spots, de-cluttered for the current
 * zoom (lib/map-markers declutter): re-laid out when a zoom ends, the world copy changes or
 * the data does, never while panning.
 */
export function SpotGlyphs({
  spots,
  markers,
  selected,
  centreLng,
}: {
  spots: readonly SpotSummary[];
  markers: Record<string, MarkerState>;
  selected: string | null;
  centreLng: number;
}) {
  const map = useMap();
  const [zoom, setZoom] = useState(() => map.getZoom());
  useMapEvents({ zoomend: () => setZoom(map.getZoom()) });

  const glyphs = useMemo(() => {
    const items = spots.map((s) => {
      const m = markers[s.slug];
      const lng = nearestCopy(s.longitude, centreLng);
      const isSelected = s.slug === selected;
      const radius = markerRadius(isSelected, !!m?.highlight);
      const p = map.project([s.latitude, lng], zoom);
      const hasData = m?.score != null;
      return {
        spot: s,
        lng,
        stale: !!m?.stale,
        input: {
          key: s.slug,
          x: p.x,
          y: p.y,
          radius,
          selected: isSelected,
          priority: isSelected ? 1e6 : (m?.score ?? -1),
          swellRot: hasData ? glyphRotation(m?.swellFromDeg) : null,
          windRot: hasData ? glyphRotation(m?.windFromDeg) : null,
          label: hasData ? (m?.label ?? null) : null,
        },
      };
    });
    const layout = declutter(
      items.map((i) => i.input),
      zoom,
    );
    return items.map((i) => {
      const l: GlyphLayout = layout.get(i.input.key) ?? { arrows: false, label: null };
      const html = glyphHtml({
        slug: i.spot.slug,
        radius: i.input.radius,
        swellRot: l.arrows ? i.input.swellRot : null,
        windRot: l.arrows ? i.input.windRot : null,
        label: l.label ? i.input.label : null,
        side: l.label,
        stale: i.stale,
      });
      const empty = !l.arrows && !l.label && !i.stale;
      return { slug: i.spot.slug, lat: i.spot.latitude, lng: i.lng, html, empty };
    });
  }, [spots, markers, selected, centreLng, zoom, map]);

  return (
    <>
      {glyphs.map((g) => (g.empty ? null : <SpotGlyph key={g.slug} lat={g.lat} lng={g.lng} html={g.html} />))}
    </>
  );
}
