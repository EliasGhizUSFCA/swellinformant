import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SelectionStore, TimelineSelectionProvider, useTimelineSelection } from "@/components/forecast/timeline-selection";
import { useMapSettings } from "@/hooks/map-settings";
import { DEFAULT_SETTINGS, writeSettings } from "@/lib/map-settings";
import { DEFAULT_WIND_UNIT, readWindUnit, useWindUnit, WIND_UNIT_EVENT, WIND_UNIT_KEY, WIND_UNITS, writeWindUnit } from "@/lib/units";

describe("SelectionStore", () => {
  it("notifies only when the state changes", () => {
    const store = new SelectionStore();
    const seen: number[] = [];
    store.subscribe(() => seen.push(store.getState().selectedT ?? -1));
    store.select(10);
    store.select(10);
    store.hover(20, 5);
    store.hover(20, 5);
    store.select(30);
    expect(seen).toEqual([10, 10, 30]);
    expect(store.getState()).toEqual({ selectedT: 30, hoverT: 20, hoverX: 5 });
    store.hover(null, 99);
    expect(store.getState().hoverX).toBeNull();
  });

  it("seeds a default without overriding a choice", () => {
    const store = new SelectionStore();
    store.initialise(5);
    expect(store.getState().selectedT).toBe(5);
    store.initialise(9);
    expect(store.getState().selectedT).toBe(5);
  });

  describe("deep link", () => {
    beforeEach(() => {
      vi.useFakeTimers();
      window.history.replaceState(null, "", "/spots/jeffreys-bay");
    });
    afterEach(() => vi.useRealTimers());

    it("writes ?t= for a chosen step (debounced, no navigation), not for the default", () => {
      const store = new SelectionStore({}, true);
      store.initialise(Date.parse("2026-10-15T05:00:00Z"));
      vi.advanceTimersByTime(500);
      expect(window.location.search).toBe("");
      store.select(Date.parse("2026-10-15T08:00:00Z"));
      store.select(Date.parse("2026-10-15T11:00:00Z"));
      expect(window.location.search).toBe("");
      vi.advanceTimersByTime(200);
      expect(new URLSearchParams(window.location.search).get("t")).toBe("2026-10-15T11:00:00Z");
      expect(window.location.pathname).toBe("/spots/jeffreys-bay");
      store.dispose();
    });
  });
});

describe("useTimelineSelection", () => {
  it("re-renders a reader only when its slice changes", () => {
    const store = new SelectionStore({ selectedT: 1 });
    let selectedRenders = 0;
    let hoverRenders = 0;
    function Selected() {
      const t = useTimelineSelection((s) => s.selectedT);
      selectedRenders++;
      return <span data-testid="sel">{t}</span>;
    }
    function Hovered() {
      const t = useTimelineSelection((s) => s.hoverT);
      hoverRenders++;
      return <span data-testid="hov">{t ?? "none"}</span>;
    }
    render(
      <TimelineSelectionProvider initialT={null} store={store}>
        <Selected />
        <Hovered />
      </TimelineSelectionProvider>,
    );
    expect(selectedRenders).toBe(1);
    act(() => store.hover(7, 3));
    act(() => store.hover(8, 4));
    expect(screen.getByTestId("hov")).toHaveTextContent("8");
    expect(selectedRenders).toBe(1); // hovering never re-renders a selection reader
    act(() => store.select(2));
    expect(screen.getByTestId("sel")).toHaveTextContent("2");
    expect(selectedRenders).toBe(2);
    expect(hoverRenders).toBe(3);
  });

  it("throws outside a provider", () => {
    function Lonely() {
      useTimelineSelection((s) => s.selectedT);
      return null;
    }
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Lonely />)).toThrow(/TimelineSelectionProvider/);
    vi.restoreAllMocks();
  });
});

describe("the shared wind unit (swell.windUnit)", () => {
  afterEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  function Probe() {
    const [unit, setUnit] = useWindUnit();
    return (
      <button type="button" onClick={() => setUnit("kn")}>
        {unit}
      </button>
    );
  }

  it("defaults to km/h, reads the map's choice and writes the same key", () => {
    expect(readWindUnit()).toBe(DEFAULT_WIND_UNIT);
    window.localStorage.setItem(WIND_UNIT_KEY, "mph");
    render(<Probe />);
    expect(screen.getByRole("button")).toHaveTextContent("mph");
    act(() => screen.getByRole("button").click());
    expect(window.localStorage.getItem(WIND_UNIT_KEY)).toBe("kn");
    expect(screen.getByRole("button")).toHaveTextContent("kn");
  });

  it("is one preference for the charts and the map settings on the same page", () => {
    function MapProbe() {
      const [settings, update] = useMapSettings();
      return (
        <button type="button" data-testid="map-unit" onClick={() => update({ windUnits: "m/s" })}>
          {settings.windUnits}
        </button>
      );
    }
    render(
      <>
        <Probe />
        <MapProbe />
      </>,
    );
    const charts = screen.getAllByRole("button").find((b) => !b.dataset.testid)!;
    expect(charts).toHaveTextContent("km/h");
    expect(screen.getByTestId("map-unit")).toHaveTextContent("km/h");
    // The map settings change it: the charts follow without a reload (same-tab event).
    act(() => screen.getByTestId("map-unit").click());
    expect(window.localStorage.getItem(WIND_UNIT_KEY)).toBe("m/s");
    expect(charts).toHaveTextContent("m/s");
    // The charts change it: the map settings follow.
    act(() => charts.click());
    expect(screen.getByTestId("map-unit")).toHaveTextContent("kn");
  });

  it("tells the page once per change, and offers one list of units", () => {
    const seen: string[] = [];
    const listen = () => seen.push(readWindUnit());
    window.addEventListener(WIND_UNIT_EVENT, listen);
    try {
      writeWindUnit("mph");
      writeWindUnit("mph"); // unchanged: no event
      writeSettings({ ...DEFAULT_SETTINGS, windUnits: "kn" });
      expect(seen).toEqual(["mph", "kn"]);
    } finally {
      window.removeEventListener(WIND_UNIT_EVENT, listen);
    }
    expect(WIND_UNITS).toEqual(["km/h", "mph", "kn", "m/s"]);
  });

  it("ignores unknown values and survives blocked storage", () => {
    window.localStorage.setItem(WIND_UNIT_KEY, "furlongs");
    expect(readWindUnit()).toBe("km/h");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readWindUnit()).toBe("km/h");
    render(<Probe />);
    act(() => screen.getByRole("button").click());
    expect(screen.getByRole("button")).toHaveTextContent("kn");
  });
});
