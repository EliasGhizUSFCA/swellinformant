import { render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Map as LeafletMap } from "leaflet";
import { MapContainer } from "react-leaflet";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { glyphHtml, markerRadius, SpotGlyphs } from "@/components/map/spot-glyphs";
import type { MarkerState } from "@/components/map/surf-map";
import { uvFromWind } from "@/lib/directions";
import {
  ARROW_CLEAR_PX,
  arrowSegment,
  boxesOverlap,
  declutter,
  glyphRotation,
  LABEL_MIN_ZOOM,
  labelBox,
  labelWidth,
  segmentHitsBox,
  SWELL_ARROW_LEN,
  type GlyphInput,
} from "@/lib/map-markers";
import type { SpotSummary } from "@/types/api";

// Positions of the 50 monitored spots from the seed data; every score, direction and label
// below is SYNTHETIC test input, not a forecast.
const SPOTS = (
  JSON.parse(readFileSync(resolve(process.cwd(), "../data/surf_spots/spots.json"), "utf8")) as Array<{ slug: string; name: string; latitude: number; longitude: number }>
).map((s) => ({ slug: s.slug, name: s.name, latitude: s.latitude, longitude: s.longitude }) as SpotSummary);

const glyph = (over: Partial<GlyphInput> = {}): GlyphInput => ({ key: "a", x: 0, y: 0, radius: 6, priority: 50, swellRot: 0, windRot: 90, label: "4–6 ft", ...over });

describe("marker arrows point where the swell and wind go (FROM + 180)", () => {
  it("turns a FROM direction by half a turn, as the charts do", () => {
    expect(glyphRotation(225)).toBe(45); // a SW swell travels NE
    expect(glyphRotation(0)).toBe(180);
    expect(glyphRotation(350)).toBe(170);
    expect(glyphRotation(-90)).toBe(90); // normalised
    expect(glyphRotation(null)).toBeNull();
    expect(glyphRotation(Number.NaN)).toBeNull();
  });

  it("draws the arrow along the wind vector (u east, v north; screen y grows south)", () => {
    for (const from of [0, 45, 90, 200, 290]) {
      const [u, v] = uvFromWind(10, from);
      const [a, b] = arrowSegment(glyphRotation(from)!, 6, SWELL_ARROW_LEN);
      // The tip lies further out than the start, in the direction the wind blows.
      expect(Math.hypot(b.x, b.y)).toBeGreaterThan(Math.hypot(a.x, a.y));
      const sgn = (n: number) => (Math.abs(n) < 1e-6 ? 0 : Math.sign(n));
      expect(sgn(b.x)).toBe(sgn(u));
      expect(sgn(-b.y)).toBe(sgn(v));
    }
    // A northerly (FROM 0) points south: straight down on screen.
    const [, tip] = arrowSegment(glyphRotation(0)!, 6, 10);
    expect(tip.x).toBeCloseTo(0, 9);
    expect(tip.y).toBeGreaterThan(0);
  });
});

describe("declutter", () => {
  it("draws arrows for isolated markers only (the selected one always keeps them)", () => {
    const items = [glyph({ key: "far", x: 0 }), glyph({ key: "a", x: 500 }), glyph({ key: "b", x: 500 + ARROW_CLEAR_PX - 1 }), glyph({ key: "sel", x: 900, selected: true }), glyph({ key: "near-sel", x: 905 })];
    const out = declutter(items, 2);
    expect(out.get("far")!.arrows).toBe(true);
    expect(out.get("a")!.arrows).toBe(false);
    expect(out.get("b")!.arrows).toBe(false);
    expect(out.get("sel")!.arrows).toBe(true);
    expect(out.get("near-sel")!.arrows).toBe(false);
  });

  it("draws no arrows without a direction", () => {
    expect(declutter([glyph({ swellRot: null, windRot: null })], 5).get("a")!.arrows).toBe(false);
  });

  it(`shows labels only from zoom ${LABEL_MIN_ZOOM}`, () => {
    expect(declutter([glyph()], LABEL_MIN_ZOOM - 0.01).get("a")!.label).toBeNull();
    expect(declutter([glyph()], LABEL_MIN_ZOOM).get("a")!.label).not.toBeNull();
  });

  it("puts a label on a side its own arrows leave free", () => {
    // Arrows pointing east (90) and north-east (45): the right side is taken, the left is free.
    expect(declutter([glyph({ swellRot: 90, windRot: 45 })], 5).get("a")!.label).toBe("left");
    // Arrows pointing west and east: neither side, so below.
    expect(declutter([glyph({ swellRot: 270, windRot: 90 })], 5).get("a")!.label).toBe("below");
    // Pointing south (180) and north: right is free.
    expect(declutter([glyph({ swellRot: 180, windRot: 0 })], 5).get("a")!.label).toBe("right");
  });

  it("never overlaps labels, other markers or other markers' arrows; the higher priority wins", () => {
    // Two markers 40 px apart on a line, both without arrows: the better one gets its label first.
    const items = [glyph({ key: "low", x: 0, priority: 10, swellRot: null, windRot: null }), glyph({ key: "high", x: 40, priority: 90, swellRot: null, windRot: null })];
    const out = declutter(items, 5);
    const box = (k: string) => {
      const g = items.find((i) => i.key === k)!;
      const b = labelBox(out.get(k)!.label!, g.radius, labelWidth(g.label!));
      return { ...b, x: b.x + g.x, y: b.y + g.y };
    };
    expect(out.get("high")!.label).toBe("right");
    expect(out.get("low")!.label).not.toBeNull();
    expect(boxesOverlap(box("low"), box("high"))).toBe(false);
    // Neither label covers the other marker's circle.
    for (const [k, other] of [["low", "high"], ["high", "low"]] as const) {
      const o = items.find((i) => i.key === other)!;
      expect(boxesOverlap(box(k), { x: o.x - 7, y: o.y - 7, w: 14, h: 14 })).toBe(false);
    }
  });

  it("labels only the best of markers drawn over each other", () => {
    const out = declutter([glyph({ key: "best", priority: 90, swellRot: null, windRot: null }), glyph({ key: "under", x: 4, priority: 20, swellRot: null, windRot: null })], 6);
    expect(out.get("best")!.label).not.toBeNull();
    expect(out.get("under")!.label).toBeNull();
  });

  it("leaves a label out when every side is taken", () => {
    // A ring of markers 16 px around the centre blocks all four sides.
    const ring = [0, 90, 180, 270].map((deg, i) => {
      const t = (deg * Math.PI) / 180;
      return glyph({ key: `r${i}`, x: 16 * Math.sin(t), y: -16 * Math.cos(t), priority: 99, label: null, swellRot: null, windRot: null });
    });
    const out = declutter([glyph({ key: "c", priority: 1, swellRot: null, windRot: null }), ...ring], 6);
    expect(out.get("c")!.label).toBeNull();
  });

  it("de-clutters the 50 real spot positions at the one-world zoom: no two labels overlap", () => {
    // Positions projected like Leaflet's spherical Mercator at zoom 3 (256 px tiles).
    const scale = 256 * 2 ** 3;
    const items = SPOTS.map((s, i) => {
      const x = ((s.longitude + 180) / 360) * scale;
      const lat = (s.latitude * Math.PI) / 180;
      const y = ((1 - Math.log(Math.tan(lat) + 1 / Math.cos(lat)) / Math.PI) / 2) * scale;
      return glyph({ key: s.slug, x, y, priority: i, swellRot: (i * 37) % 360, windRot: (i * 71) % 360 });
    });
    const out = declutter(items, 3);
    const placed = items
      .filter((g) => out.get(g.key)!.label)
      .map((g) => {
        const b = labelBox(out.get(g.key)!.label!, g.radius, labelWidth(g.label!));
        return { ...b, x: b.x + g.x, y: b.y + g.y };
      });
    expect(placed.length).toBeGreaterThan(10);
    for (let i = 0; i < placed.length; i++) for (let j = i + 1; j < placed.length; j++) expect(boxesOverlap(placed[i]!, placed[j]!)).toBe(false);
    expect(placed.length).toBeLessThan(items.length); // the Portugal and Bali clusters lose some
  });

  it("detects a segment through a box", () => {
    expect(segmentHitsBox({ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 8, y: -2, w: 4, h: 4 })).toBe(true);
    expect(segmentHitsBox({ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 8, y: 3, w: 4, h: 4 })).toBe(false);
  });
});

describe("glyph HTML", () => {
  it("rotates each arrow by FROM + 180 and carries the values for tests and tooling", () => {
    const html = glyphHtml({ slug: "synthetic", radius: 6, swellRot: glyphRotation(225), windRot: glyphRotation(300), label: "4–6 ft", side: "right", stale: false });
    expect(html).toContain('data-swell-rot="45"');
    expect(html).toContain('data-wind-rot="120"');
    expect(html).toContain('transform="rotate(45)"');
    expect(html).toContain('transform="rotate(120)"');
    expect(html).toContain('data-label-side="right"');
    expect(html).toContain(">4–6 ft</span>");
    expect(html).not.toContain("sg-stale");
  });

  it("escapes the label and marks a stale forecast", () => {
    const html = glyphHtml({ slug: "x", radius: 6, swellRot: null, windRot: null, label: '<b>"1"</b>', side: "left", stale: true });
    expect(html).not.toContain("<b>");
    expect(html).toContain("&#60;b&#62;");
    expect(html).toContain('class="sg-stale"');
    expect(html).toContain("data-stale");
    expect(html).toContain("sg-label sg-label-stale");
  });

  it("has no SVG when there is nothing to draw", () => {
    expect(glyphHtml({ slug: "x", radius: 6, swellRot: null, windRot: null, label: null, side: null, stale: false })).not.toContain("<svg");
  });

  it("matches the circle sizes of SpotMarker", () => {
    expect([markerRadius(false, false), markerRadius(false, true), markerRadius(true, false)]).toEqual([6, 8, 11]);
  });
});

// jsdom has no layout: give Leaflet's container a size, and ResizeObserver a no-op.
const SIZE = { x: 1440, y: 800 };
const clientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
const clientHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => SIZE.x });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => SIZE.y });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});
afterAll(() => {
  if (clientWidth) Object.defineProperty(HTMLElement.prototype, "clientWidth", clientWidth);
  if (clientHeight) Object.defineProperty(HTMLElement.prototype, "clientHeight", clientHeight);
  vi.unstubAllGlobals();
});

describe("SpotGlyphs on a map", () => {
  function draw(zoom: number, markers: Record<string, MarkerState>, spots = SPOTS) {
    let map: LeafletMap | null = null;
    const view = render(
      <MapContainer
        ref={(m) => {
          map = m;
        }}
        center={[15, 10]}
        zoom={zoom}
        style={{ width: SIZE.x, height: SIZE.y }}
      >
        <SpotGlyphs spots={spots} markers={markers} selected={null} centreLng={10} />
      </MapContainer>,
    );
    return { view, map: () => map! };
  }
  const state = (over: Partial<MarkerState> = {}): MarkerState => ({ score: 70, tooltip: "x", swellFromDeg: 225, windFromDeg: 45, label: "4–6 ft", ...over });

  it("draws arrows and labels from the marker state, and nothing for a spot without data", () => {
    const one = SPOTS.find((s) => s.slug === "cloudbreak")!;
    const { view } = draw(5, { cloudbreak: state() }, [one]);
    const g = view.container.querySelector('[data-testid="spot-glyph"][data-slug="cloudbreak"]')!;
    expect(g).not.toBeNull();
    expect(g.getAttribute("data-swell-rot")).toBe("45");
    expect(g.getAttribute("data-wind-rot")).toBe("225");
    expect(g.getAttribute("data-label-side")).not.toBeNull();
    expect(g.textContent).toBe("4–6 ft");
    view.unmount();
    const empty = draw(5, { cloudbreak: state({ score: null }) }, [one]);
    expect(empty.view.container.querySelector('[data-testid="spot-glyph"]')).toBeNull();
  });

  it("keeps the glyphs out of the way of clicks and the keyboard", () => {
    const one = SPOTS.find((s) => s.slug === "cloudbreak")!;
    const { view } = draw(5, { cloudbreak: state() }, [one]);
    const icon = view.container.querySelector(".spot-glyph-icon")!;
    expect(icon.classList.contains("leaflet-interactive")).toBe(false);
    expect(icon.getAttribute("tabindex")).toBeNull();
  });

  it("marks a stale spot even where its arrows and label are hidden", () => {
    const one = SPOTS.find((s) => s.slug === "cloudbreak")!;
    const { view } = draw(1, { cloudbreak: state({ stale: true, swellFromDeg: null, windFromDeg: null }) }, [one]);
    expect(view.container.querySelector('[data-testid="spot-glyph"]')!.hasAttribute("data-stale")).toBe(true);
  });
});
