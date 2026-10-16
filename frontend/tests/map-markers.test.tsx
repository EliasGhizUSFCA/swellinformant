import { act, render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { CircleMarker as LeafletCircleMarker, Evented, Map as LeafletMap } from "leaflet";
import { Profiler, useMemo } from "react";
import { MapContainer } from "react-leaflet";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { SpotMarker } from "@/components/map/forecast-map";
import { CentreWatcher, FlyToSpot, MapBounds, useWrapCentre, type MapCover, type MarkerState } from "@/components/map/surf-map";
import { nearestCopy, wrapCentre } from "@/lib/map-bounds";
import type { SpotSummary } from "@/types/api";

// The 50 monitored spots from the seed data (vitest runs in frontend/): positions only, and
// every score below is SYNTHETIC, not a forecast.
const SPOTS = (
  JSON.parse(readFileSync(resolve(process.cwd(), "../data/surf_spots/spots.json"), "utf8")) as Array<{
    slug: string;
    name: string;
    latitude: number;
    longitude: number;
  }>
).map((s) => ({ slug: s.slug, name: s.name, latitude: s.latitude, longitude: s.longitude }) as SpotSummary);
const SIZE = { x: 1440, y: 800 };

// jsdom has no layout: give Leaflet's container a size, and ResizeObserver a no-op.
const clientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
const clientHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("leaflet-container") ? SIZE.x : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("leaflet-container") ? SIZE.y : 0;
    },
  });
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

// Counts the Leaflet calls react-leaflet makes on the markers.
const calls = { setLatLng: 0, setStyle: 0, on: 0, off: 0 };
beforeEach(() => {
  for (const k of Object.keys(calls) as Array<keyof typeof calls>) calls[k] = 0;
  const proto = LeafletCircleMarker.prototype;
  for (const k of ["setLatLng", "setStyle"] as const) {
    const orig = proto[k] as (...a: unknown[]) => unknown;
    vi.spyOn(proto, k).mockImplementation(function (this: LeafletCircleMarker, ...a: unknown[]) {
      calls[k] += 1;
      return orig.apply(this, a) as never;
    });
  }
  for (const k of ["on", "off"] as const) {
    const orig = Evented.prototype[k] as (...a: unknown[]) => unknown;
    vi.spyOn(Evented.prototype, k).mockImplementation(function (this: Evented, ...a: unknown[]) {
      if (this instanceof LeafletCircleMarker) calls[k] += 1;
      return orig.apply(this, a) as never;
    });
  }
});
afterEach(() => vi.restoreAllMocks());
const reset = () => {
  for (const k of Object.keys(calls) as Array<keyof typeof calls>) calls[k] = 0;
};

interface HarnessProps {
  spots: SpotSummary[];
  markers: Record<string, MarkerState>;
  selected?: string | null;
  cover?: (size: { x: number; y: number }) => MapCover;
  onCommit?: () => void;
}

const noop = () => undefined;

/** The forecast map's marker layer without its canvas layers. */
function Markers({ spots, markers, selected = null, cover, onCommit }: HarnessProps) {
  const lons = useMemo(() => spots.map((s) => s.longitude), [spots]);
  const { centreLng, onMove, onRecentre } = useWrapCentre(lons, 10);
  const sel = spots.find((s) => s.slug === selected);
  return (
    <>
      <Profiler id="markers" onRender={() => onCommit?.()}>
        {spots.map((s) => (
          <SpotMarker
            key={s.slug}
            spot={s}
            lng={nearestCopy(s.longitude, centreLng)}
            score={markers[s.slug]?.score ?? null}
            tooltip={markers[s.slug]?.tooltip ?? "No usable forecast"}
            selected={s.slug === selected}
            highlight={!!markers[s.slug]?.highlight}
            onSelect={noop}
          />
        ))}
      </Profiler>
      <FlyToSpot slug={selected} lat={sel?.latitude} lon={sel?.longitude} cover={cover} />
      <MapBounds onRecentre={onRecentre} />
      <CentreWatcher onCentre={onMove} />
    </>
  );
}

function setup(props: HarnessProps) {
  let map: LeafletMap | null = null;
  const tree = (p: HarnessProps) => (
    <MapContainer
      ref={(m) => {
        map = m;
      }}
      center={[15, 10]}
      zoom={3}
      minZoom={1}
      worldCopyJump={false}
      style={{ width: SIZE.x, height: SIZE.y }}
    >
      <Markers {...p} />
    </MapContainer>
  );
  const view = render(tree(props));
  return { map: () => map!, rerender: (p: HarnessProps) => view.rerender(tree(p)) };
}

const states = (score: number | null): Record<string, MarkerState> =>
  Object.fromEntries(SPOTS.map((s) => [s.slug, { score, tooltip: score == null ? "No usable forecast" : `Synthetic ${score}` }]));
const markerLng = (map: LeafletMap, slug: string) => {
  let lng: number | null = null;
  map.eachLayer((l) => {
    if (l instanceof LeafletCircleMarker && (l.options.className ?? "").split(" ").includes(`spot-${slug}`)) lng = l.getLatLng().lng;
  });
  return lng;
};

describe("spot markers", () => {
  it("draws every marker in the world copy nearest the view, re-placing only those that change copy", () => {
    const { map } = setup({ spots: SPOTS, markers: states(60) });
    for (const s of SPOTS) expect(markerLng(map(), s.slug)).toBeCloseTo(nearestCopy(s.longitude, 10), 9);
    reset();
    // A small pan crosses no spot's centre ± 180: nothing moves.
    act(() => map().setView([15, 15], 3, { animate: false }));
    expect(calls.setLatLng).toBe(0);
    // Centring on 100E moves the spots between 170W and 80W to the copy east of the date line.
    act(() => map().setView([15, 100], 3, { animate: false }));
    const crossed = SPOTS.filter((s) => nearestCopy(s.longitude, 100) !== nearestCopy(s.longitude, 15));
    expect(crossed.length).toBeGreaterThan(5);
    expect(calls.setLatLng).toBe(crossed.length);
    for (const s of SPOTS) {
      const lng = markerLng(map(), s.slug)!;
      expect(Math.abs(lng - 100)).toBeLessThanOrEqual(180);
      expect(lng).toBeCloseTo(nearestCopy(s.longitude, 100), 9);
    }
  });

  it("re-renders the marker layer only when some spot crosses the centre ± 180", () => {
    let commits = 0;
    const { map } = setup({ spots: SPOTS, markers: states(60), onCommit: () => (commits += 1) });
    commits = 0;
    // Panning east from 10E, the first spot to change copy is the one whose antipodal
    // meridian (longitude + 180) comes first: a Hawaiian break near 158W, at about 21.8E.
    const first = SPOTS.map((s) => ({ s, at: wrapCentre(s.longitude + 180) }))
      .filter((c) => c.at > 10)
      .sort((x, y) => x.at - y.at)[0]!;
    expect(first.at).toBeGreaterThan(20);
    // Zoom 8: Leaflet keeps the centre to the nearest pixel, 0.005 degrees there.
    for (const lng of [12, 16, 20, first.at - 0.05]) act(() => map().setView([15, lng], 8, { animate: false }));
    expect(commits).toBe(0);
    act(() => map().setView([15, first.at + 0.05], 8, { animate: false }));
    expect(commits).toBe(1);
    expect(markerLng(map(), first.s.slug)).toBeCloseTo(first.s.longitude + 360, 9);
  });

  it("is memoised: a parent render with equal states touches no marker; a new colour restyles only that marker", () => {
    const { rerender } = setup({ spots: SPOTS, markers: states(60) });
    reset();
    rerender({ spots: SPOTS, markers: states(60) }); // new objects, same values
    expect(calls).toEqual({ setLatLng: 0, setStyle: 0, on: 0, off: 0 });
    // A new score in the same quality class (Good, 57-71) changes the tooltip only.
    rerender({ spots: SPOTS, markers: { ...states(60), pipeline: { score: 65, tooltip: "Synthetic 65" } } });
    expect(calls).toEqual({ setLatLng: 0, setStyle: 0, on: 0, off: 0 });
    // A new class (Excellent) restyles that one marker.
    rerender({ spots: SPOTS, markers: { ...states(60), pipeline: { score: 90, tooltip: "Synthetic 90" } } });
    expect(calls.setStyle).toBe(1);
    expect(calls.setLatLng).toBe(0);
    expect(calls.on + calls.off).toBe(0);
  });

  it("re-centres a runaway view on the same meridian with the markers already in their new copy", () => {
    const { map } = setup({ spots: SPOTS, markers: states(60) });
    act(() => map().setView([15, 600], 3, { animate: false }));
    expect(map().getCenter().lng).toBeCloseTo(wrapCentre(600), 6);
    for (const s of SPOTS) expect(Math.abs(markerLng(map(), s.slug)! - wrapCentre(600))).toBeLessThanOrEqual(180);
    // Below 540 degrees the view is left alone.
    act(() => map().setView([15, 500], 3, { animate: false }));
    expect(map().getCenter().lng).toBeCloseTo(500, 6);
  });
});

describe("flying to the selected spot", () => {
  const without = (slug: string) => SPOTS.filter((s) => s.slug !== slug);

  it("takes the short way across the date line, only when the selection changes", () => {
    const { map, rerender } = setup({ spots: SPOTS, markers: states(60), selected: "cloudbreak" });
    const fiji = SPOTS.find((s) => s.slug === "cloudbreak")!;
    expect(map().getCenter().lng).toBeCloseTo(fiji.longitude, 6);
    expect(map().getZoom()).toBe(5);
    rerender({ spots: SPOTS, markers: states(60), selected: "pipeline" });
    const lng = map().getCenter().lng;
    expect(Math.abs(lng - fiji.longitude)).toBeLessThan(40); // not 335 degrees the long way
    const pipeline = SPOTS.find((s) => s.slug === "pipeline")!;
    expect(lng).toBeCloseTo(nearestCopy(pipeline.longitude, fiji.longitude), 6);
    // The user pans away; a filter hides and re-shows Pipeline; new forecast states arrive.
    act(() => map().setView([-30, 140], 4, { animate: false }));
    rerender({ spots: without("pipeline"), markers: states(60), selected: "pipeline" });
    rerender({ spots: SPOTS, markers: states(80), selected: "pipeline" });
    expect(map().getCenter().lat).toBeCloseTo(-30, 6);
    expect(map().getCenter().lng).toBeCloseTo(140, 6);
    expect(map().getZoom()).toBe(4);
  });

  it("jumps instead of flying when the user prefers reduced motion", () => {
    // jsdom has no matchMedia: answer the reduced-motion query only.
    const original = window.matchMedia;
    window.matchMedia = ((q: string) => ({ matches: q === "(prefers-reduced-motion: reduce)", media: q })) as unknown as typeof window.matchMedia;
    const fly = vi.spyOn(LeafletMap.prototype, "flyTo");
    const setView = vi.spyOn(LeafletMap.prototype, "setView");
    try {
      const { map, rerender } = setup({ spots: SPOTS, markers: states(60), selected: null });
      setView.mockClear();
      rerender({ spots: SPOTS, markers: states(60), selected: "pipeline" });
      expect(fly).not.toHaveBeenCalled();
      expect(setView).toHaveBeenCalledWith(expect.anything(), 5, expect.objectContaining({ animate: false }));
      const pipeline = SPOTS.find((s) => s.slug === "pipeline")!;
      expect(map().getCenter().lng).toBeCloseTo(nearestCopy(pipeline.longitude, 10), 6);
    } finally {
      window.matchMedia = original;
    }
  });

  it("lands the spot above a bottom sheet and below the top controls", () => {
    // A phone's bottom sheet: up to 72 % of the map's height.
    const cover = (size: { y: number }) => ({ top: 64, right: 0, bottom: size.y * 0.72, left: 0 });
    const { map } = setup({ spots: SPOTS, markers: states(60), selected: "nazare", cover });
    const nazare = SPOTS.find((s) => s.slug === "nazare")!;
    const p = map().latLngToContainerPoint([nazare.latitude, nazare.longitude]);
    const sheetTop = SIZE.y * (1 - 0.72);
    expect(p.y).toBeGreaterThan(64);
    expect(p.y).toBeLessThan(sheetTop);
    expect(p.y).toBeCloseTo((64 + sheetTop) / 2, 0);
    expect(p.x).toBeCloseTo(SIZE.x / 2, 0);
  });

  it("lands the spot left of a side sheet and above the timeline", () => {
    // Landscape: a 360 px side sheet on the right, the timeline stack (120 px) at the bottom.
    const cover = () => ({ top: 64, right: 360, bottom: 120, left: 0 });
    const { map } = setup({ spots: SPOTS, markers: states(60), selected: "nazare", cover });
    const nazare = SPOTS.find((s) => s.slug === "nazare")!;
    const p = map().latLngToContainerPoint([nazare.latitude, nazare.longitude]);
    expect(p.x).toBeCloseTo((SIZE.x - 360) / 2, 0);
    expect(p.y).toBeCloseTo((64 + SIZE.y - 120) / 2, 0);
  });
});
