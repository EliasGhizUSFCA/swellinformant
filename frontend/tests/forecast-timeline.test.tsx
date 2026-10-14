import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ProfilerOnRenderCallback } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { SpotForecastTimeline } from "@/components/forecast/forecast-timeline";
import { SelectionStore, TimelineSelectionProvider } from "@/components/forecast/timeline-selection";
import { ARROW_CONVENTION_TEXT, arrowRotationDeg } from "@/lib/directions";
import { WIND_RELATION_LABEL } from "@/lib/quality";
import { directionText, hourTicks, makeScale, pxPerHourFor, selectedTimeTitle, stepLengthText, timelineModel } from "@/lib/timeline";
import { formatHeightRange, formatWind } from "@/lib/units";
import type { SpotTimeline } from "@/types/api";

import { jbay, mixed3h6h, patchStep, withSyntheticTide } from "./timeline-helpers";

// jsdom has no layout: the component then lays out for a 1000 px frame with a 92 px gutter.
const GUTTER = 92;
const scaleFor = (tl: SpotTimeline) => makeScale(timelineModel(tl).start, pxPerHourFor(1000 - GUTTER, 72));

beforeAll(() => {
  // React listens for pointer events; jsdom does not construct PointerEvent with pointerType.
  if (!("PointerEvent" in window) || !("pointerType" in new window.PointerEvent("pointermove"))) {
    class PE extends MouseEvent {
      pointerType: string;
      pointerId: number;
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init);
        this.pointerType = init.pointerType ?? "mouse";
        this.pointerId = init.pointerId ?? 1;
      }
    }
    (window as unknown as { PointerEvent: typeof PE }).PointerEvent = PE;
  }
});

beforeEach(() => {
  // The frame occupies x 0..1000 on screen; the plot content starts at its left edge.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 0, right: 1000, top: 0, bottom: 600, width: 1000, height: 600, x: 0, y: 0, toJSON: () => ({}) });
});
afterEach(() => vi.restoreAllMocks());

function renderTimeline(tl: SpotTimeline, opts: { select?: number; onRowRender?: ProfilerOnRenderCallback; units?: "ft" | "m" } = {}) {
  const store = new SelectionStore({ selectedT: Date.parse(tl.steps[opts.select ?? 3]!.t) });
  const utils = render(
    <TimelineSelectionProvider initialT={null} store={store}>
      <SpotForecastTimeline timeline={tl} units={opts.units ?? "ft"} onRowRender={opts.onRowRender} />
    </TimelineSelectionProvider>,
  );
  return { store, ...utils };
}

const qualityRect = (i: number) => document.querySelector<SVGRectElement>(`[data-testid="quality-strip"] rect[data-step="${i}"]`)!;
/** Screen x of step i's centre (the bar's valid time). */
function stepClientX(i: number): number {
  const r = qualityRect(i);
  return GUTTER + Number(r.getAttribute("x")) + Number(r.getAttribute("width")) / 2;
}
const scroller = () => screen.getByTestId("timeline-scroller");
const summaryTime = () => screen.getByTestId("summary-time").textContent;

describe("1. every row loads", () => {
  it("renders the header, quality, waves, swell, wind and tide rows", () => {
    renderTimeline(jbay());
    for (const id of ["timeline-header", "quality-strip", "wave-bars", "swell-partitions", "wind-row", "tide-row"]) expect(screen.getByTestId(id)).toBeInTheDocument();
    expect(screen.getByTestId("tide-row")).toHaveTextContent("Tide data unavailable");
    expect(screen.getByTestId("quality-chart")).toBeInTheDocument();
    expect(screen.getByTestId("quality-chart-legend")).toHaveTextContent("likely range");
  });
});

describe("2. hour labels", () => {
  it("are the axis ticks in the spot's zone (12 AM / 6 AM / 12 PM / 6 PM)", () => {
    const tl = jbay();
    renderTimeline(tl);
    const m = timelineModel(tl);
    const expected = hourTicks(m.start, m.end, tl.timezone, scaleFor(tl).pxPerHour).filter((t) => !t.midnight);
    const drawn = [...document.querySelectorAll("[data-testid='timeline-header'] [data-tick]")];
    expect(drawn.map((g) => Number(g.getAttribute("data-t")))).toEqual(expected.map((t) => t.t));
    expect(new Set(drawn.map((g) => g.getAttribute("data-tick")))).toEqual(new Set(["6 AM", "12 PM", "6 PM"]));
    // 6 AM SAST is 04:00 UTC.
    expect(drawn.find((g) => g.getAttribute("data-tick") === "6 AM")!.getAttribute("data-t")).toBe(String(Date.parse("2026-10-12T04:00:00Z")));
  });
});

describe("3. day boundaries line up", () => {
  it("draws each local midnight at the same x in every row", () => {
    renderTimeline(jbay());
    for (const date of ["2026-10-13", "2026-10-14"]) {
      const lines = [...document.querySelectorAll(`[data-day-boundary="${date}"]`)];
      expect(lines).toHaveLength(6);
      expect(new Set(lines.map((l) => l.getAttribute("x1"))).size).toBe(1);
      const rows = new Set(lines.map((l) => l.closest("[data-row]")!.getAttribute("data-testid")));
      expect(rows.size).toBe(6);
    }
  });
});

describe("4. selecting a time updates every row and the summary", () => {
  it("click, then the arrow keys, Page Down and Home / End", () => {
    const tl = withSyntheticTide(jbay());
    renderTimeline(tl);
    fireEvent.click(qualityRect(8), { clientX: stepClientX(8) });
    expect(summaryTime()).toBe(selectedTimeTitle(Date.parse(tl.steps[8]!.t), tl.timezone));
    expect(scroller()).toHaveAttribute("aria-valuenow", "8");
    const marked = [...document.querySelectorAll("[data-selected]")];
    expect(new Set(marked.map((el) => el.getAttribute("data-step")))).toEqual(new Set(["8"]));
    for (const row of ["quality-strip", "wave-bars", "swell-partitions", "wind-row"]) expect(within(screen.getByTestId(row)).getAllByText((_, el) => el?.hasAttribute("data-selected") ?? false).length).toBeGreaterThan(0);
    const cursor = document.querySelector("[data-cursor='selected']")!;
    const x = Number(cursor.getAttribute("data-x"));
    expect(Math.abs(x - (stepClientX(8) - GUTTER))).toBeLessThan(0.01);
    const marker = document.querySelector("[data-tide-marker]")!;
    expect(Number(marker.getAttribute("cx"))).toBeCloseTo(x, 6);
    expect(Number(marker.getAttribute("data-height"))).toBe(tl.steps[8]!.tide!.height_m);

    fireEvent.keyDown(scroller(), { key: "ArrowRight" });
    expect(scroller()).toHaveAttribute("aria-valuenow", "9");
    expect(summaryTime()).toBe(selectedTimeTitle(Date.parse(tl.steps[9]!.t), tl.timezone));
    fireEvent.keyDown(scroller(), { key: "ArrowLeft" });
    fireEvent.keyDown(scroller(), { key: "PageDown" });
    expect(scroller()).toHaveAttribute("aria-valuenow", "16"); // 24 h later on a 3-hourly run
    expect(tl.steps[16]!.t).toBe("2026-10-14T03:00:00Z"); // 5:00 AM SAST, Wednesday the 14th
    fireEvent.keyDown(scroller(), { key: "Home" });
    // The first step of that local day is 2:00 AM SAST (00:00 UTC), not 21:00 UTC (11 PM the day before).
    expect(tl.steps[Number(scroller().getAttribute("aria-valuenow"))]!.t).toBe("2026-10-14T00:00:00Z");
    fireEvent.keyDown(scroller(), { key: "End" });
    expect(tl.steps[Number(scroller().getAttribute("aria-valuenow"))]!.t).toBe("2026-10-14T21:00:00Z");
    expect(scroller().getAttribute("aria-valuetext")).toMatch(/^Wed 11:00 PM, \w[\w ]+, [\d.–]+ ft$/);
  });

  it("follows a step chosen elsewhere (another reader of the same selection)", () => {
    const tl = jbay();
    const { store } = renderTimeline(tl);
    act(() => store.select(Date.parse(tl.steps[12]!.t)));
    expect(scroller()).toHaveAttribute("aria-valuenow", "12");
    expect(document.querySelector("[data-testid='quality-strip'] [data-selected]")!.getAttribute("data-step")).toBe("12");
  });

  it("day buttons select the day's best daylight step and are pressed for the selected day", () => {
    const tl = jbay();
    renderTimeline(tl);
    const day = screen.getByTestId("day-2026-10-13");
    fireEvent.click(day);
    expect(day).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("day-2026-10-12")).toHaveAttribute("aria-pressed", "false");
    expect(tl.steps[Number(scroller().getAttribute("aria-valuenow"))]!.t).toBe(tl.days[1]!.best_time);
    fireEvent.click(screen.getByRole("button", { name: "Next day" }));
    expect(screen.getByTestId("day-2026-10-14")).toHaveAttribute("aria-pressed", "true");
  });
});

describe("5 and 6. bars and wind equal the API values", () => {
  it("wave bars carry the central value, range and sets of each step", () => {
    const tl = jbay();
    renderTimeline(tl, { select: 4 });
    const bars = document.querySelectorAll("[data-testid='wave-bars'] g[data-step]");
    expect(bars).toHaveLength(tl.steps.length);
    bars.forEach((g, i) => {
      const b = tl.steps[i]!.breaking;
      expect(g.getAttribute("data-central")).toBe(String(b.central_ft));
      expect(g.getAttribute("data-p10")).toBe(String(b.p10_ft));
      expect(g.getAttribute("data-p90")).toBe(String(b.p90_ft));
      expect(g.getAttribute("data-sets")).toBe(String(b.sets_ft));
    });
    const b = tl.steps[4]!.breaking;
    expect(screen.getByTestId("summary-breaking")).toHaveTextContent(formatHeightRange(b.typical_ft, b.sets_ft, "ft"));
    expect(screen.getByTestId("summary-breaking")).toHaveAttribute("data-central", String(b.central_ft));
  });

  it("wind bars carry speed and gust; the summary says speed, direction and relation; no gust, no tick", () => {
    const tl = patchStep(jbay(), 5, (s) => {
      s.wind!.gust_kmh = null; // SYNTHETIC: a source without gusts
    });
    renderTimeline(tl, { select: 4 });
    const groups = document.querySelectorAll("[data-testid='wind-row'] g[data-step]");
    groups.forEach((g) => {
      const w = tl.steps[Number(g.getAttribute("data-step"))]!.wind!;
      expect(g.getAttribute("data-speed")).toBe(String(w.speed_kmh));
      expect(g.getAttribute("data-gust")).toBe(w.gust_kmh == null ? null : String(w.gust_kmh));
      expect(g.querySelectorAll("[data-gust-tick]")).toHaveLength(w.gust_kmh == null ? 0 : 1);
    });
    const w = tl.steps[4]!.wind!;
    expect(screen.getByTestId("summary-wind")).toHaveTextContent(`${formatWind(w.speed_kmh, "km/h")} from ${directionText(w.dir_from_deg)}`);
    expect(screen.getByTestId("summary-wind-relation")).toHaveTextContent(WIND_RELATION_LABEL[w.relation]!);
    expect(screen.getByTestId("summary-gust")).toHaveTextContent(`gust ${formatWind(w.gust_kmh, "km/h")}`);
    fireEvent.keyDown(scroller(), { key: "ArrowRight" });
    expect(screen.getByTestId("summary-gust")).toHaveTextContent("no gust data");
  });

  it("converts wind to the shared unit", () => {
    const tl = jbay();
    renderTimeline(tl, { select: 4 });
    fireEvent.click(screen.getByRole("button", { name: "kn" }));
    expect(screen.getByTestId("summary-wind")).toHaveTextContent(formatWind(tl.steps[4]!.wind!.speed_kmh, "kn"));
    expect(window.localStorage.getItem("swell.windUnit")).toBe("kn");
    window.localStorage.clear();
  });
});

describe("7. arrows point where the wind and waves go", () => {
  it("rotates each wind arrow by FROM + 180 and states the convention", () => {
    const tl = jbay();
    renderTimeline(tl);
    const arrows = document.querySelectorAll<SVGPathElement>("[data-testid='wind-row'] .tl-wind-arrow");
    expect(arrows.length).toBe(tl.steps.length); // 3-hourly steps are wide enough for one each
    arrows.forEach((a) => {
      const i = Number(a.closest("[data-step]")!.getAttribute("data-step"));
      const rot = arrowRotationDeg(tl.steps[i]!.wind!.dir_from_deg!);
      expect(a.getAttribute("transform")).toContain(`rotate(${rot})`);
    });
    const swell = document.querySelectorAll("[data-testid='swell-partitions'] [data-partition]");
    swell.forEach((p) => expect(p.getAttribute("transform")).toContain(`rotate(${arrowRotationDeg(Number(p.getAttribute("data-dir-from")))})`));
    expect(screen.getByTestId("quality-chart-legend")).toHaveTextContent(ARROW_CONVENTION_TEXT);
  });

  it("emphasises the partition that drives the surf, from the API", () => {
    const tl = jbay();
    renderTimeline(tl);
    const drives = [...document.querySelectorAll("[data-testid='swell-partitions'] [data-drives='true']")];
    expect(drives.length).toBe(tl.steps.filter((s) => s.partitions.some((p) => p.drives_surf)).length);
    for (const d of drives) {
      const s = tl.steps[Number(d.closest("[data-step]")!.getAttribute("data-step"))]!;
      expect(s.partitions.find((p) => p.drives_surf)!.kind).toBe(d.getAttribute("data-partition"));
    }
  });
});

describe("8. tide", () => {
  it("draws the curve and highs/lows at their times, labelled in the spot zone with the datum", () => {
    const tl = withSyntheticTide(jbay());
    renderTimeline(tl);
    const scale = scaleFor(tl);
    const m = timelineModel(tl);
    expect(document.querySelector("[data-tide-curve]")).not.toBeNull();
    const events = [...document.querySelectorAll("[data-tide-event]")];
    expect(events).toHaveLength(m.tide!.events.length);
    events.forEach((g, k) => {
      const e = m.tide!.events[k]!;
      expect(Number(g.querySelector("circle")!.getAttribute("cx"))).toBeCloseTo(scale(e.t), 6);
      const clock = new Intl.DateTimeFormat("en-US", { timeZone: tl.timezone, hour: "numeric", minute: "2-digit", hourCycle: "h12" }).format(e.t);
      expect(g.textContent).toBe(`${e.kind === "high" ? "H" : "L"} ${clock}`);
    });
    expect(screen.getByTestId("tide-caption")).toHaveTextContent("model mean sea level");
    expect(screen.getByTestId("tide-preference")).toHaveTextContent("Mid tide (synthetic test preference).");
    expect(screen.getByTestId("summary-tide")).toHaveTextContent(/ft · (rising|falling)/);
  });

  it("says so, and draws nothing, without a tide source", () => {
    renderTimeline(jbay());
    expect(screen.getByTestId("tide-unavailable")).toHaveTextContent("Tide data unavailable · No tide source configured for this spot.");
    expect(document.querySelector("[data-tide-curve]")).toBeNull();
    expect(screen.getByTestId("summary-tide")).toHaveTextContent("Tide data unavailable");
    expect(screen.getByTestId("tide-preference")).toHaveTextContent("Tide preference not well established.");
  });
});

describe("9. quality comes from the API's rating", () => {
  it("does not change when only the wave heights change", () => {
    const tl = jbay();
    const { unmount } = renderTimeline(tl);
    const before = [...document.querySelectorAll("[data-testid='quality-strip'] rect[data-step]")].map((r) => [r.getAttribute("data-label"), r.getAttribute("fill")]);
    unmount();
    const bigger = structuredClone(tl);
    for (const s of bigger.steps) {
      // SYNTHETIC: three times the breaking heights.
      s.breaking.central_ft *= 3;
      s.breaking.sets_ft *= 3;
    }
    renderTimeline(bigger);
    const after = [...document.querySelectorAll("[data-testid='quality-strip'] rect[data-step]")].map((r) => [r.getAttribute("data-label"), r.getAttribute("fill")]);
    expect(after).toEqual(before);
    expect(before.map(([l]) => l)).toEqual(tl.steps.map((s) => s.quality.label));
  });
});

describe("10. tooltips", () => {
  it("hover shows the step's local time, values, gust and step length; the keyboard summary agrees", () => {
    const tl = jbay();
    renderTimeline(tl, { select: 2 });
    const content = scroller().firstElementChild!;
    fireEvent.pointerMove(content, { clientX: stepClientX(6), pointerType: "mouse" });
    const tip = screen.getByTestId("timeline-tooltip");
    const s = tl.steps[6]!;
    const time = selectedTimeTitle(Date.parse(s.t), tl.timezone);
    expect(tip).toHaveAttribute("data-step", "6");
    expect(tip).toHaveTextContent(time);
    expect(tip).toHaveTextContent(stepLengthText(s.step_hours));
    expect(tip).toHaveTextContent(`gust ${formatWind(s.wind!.gust_kmh, "km/h")}`);
    expect(tip).toHaveTextContent(formatHeightRange(s.breaking.typical_ft, s.breaking.sets_ft, "ft"));
    expect(document.querySelector("[data-cursor='hover']")!.getAttribute("data-t")).toBe(String(Date.parse(s.t)));
    fireEvent.pointerLeave(content);
    expect(screen.queryByTestId("timeline-tooltip")).toBeNull();

    // The keyboard path: the slider's step and the summary carry the same facts.
    for (let k = 0; k < 4; k++) fireEvent.keyDown(scroller(), { key: "ArrowRight" });
    expect(summaryTime()).toBe(time);
    expect(screen.getByTestId("summary-step-length")).toHaveTextContent("3-hour step");
    expect(screen.getByTestId("summary-gust")).toHaveTextContent(`gust ${formatWind(s.wind!.gust_kmh, "km/h")}`);
  });
});

describe("12. day and night shading", () => {
  it("spans exactly last light to the next first light", () => {
    const tl = jbay();
    renderTimeline(tl);
    const scale = scaleFor(tl);
    const bands = [...document.querySelectorAll("[data-testid='quality-strip'] rect[data-night]")];
    const between = bands.find((b) => Math.abs(Number(b.getAttribute("x")) - scale(Date.parse(tl.days[0]!.last_light!))) < 1e-6)!;
    expect(between).toBeDefined();
    expect(Number(between.getAttribute("width"))).toBeCloseTo(scale(Date.parse(tl.days[1]!.first_light!)) - scale(Date.parse(tl.days[0]!.last_light!)), 6);
  });
});

describe("13. local time zone", () => {
  const before = process.env.TZ;
  afterEach(() => {
    process.env.TZ = before;
  });
  it("labels everything in the spot's zone whatever the browser's zone", () => {
    process.env.TZ = "America/New_York";
    const tl = jbay();
    renderTimeline(tl, { select: 1 });
    expect(summaryTime()).toBe("Monday, Oct 12 · 8:00 AM SAST");
    expect(screen.getByTestId("timeline-zone")).toHaveTextContent("Times in SAST (GMT+2)");
    expect(screen.getByTestId("timeline-header")).toHaveTextContent("Mon");
  });
});

describe("14. missing data does not break the timeline", () => {
  it("renders gaps and dashes for a step without wind, swells or gusts", () => {
    let tl = patchStep(jbay(), 3, (s) => {
      s.wind = null; // SYNTHETIC
    });
    tl = patchStep(tl, 4, (s) => {
      s.partitions = s.partitions.filter((p) => p.kind === "primary_swell"); // SYNTHETIC
      s.wind!.gust_kmh = null;
    });
    renderTimeline(tl, { select: 3 });
    expect(document.querySelector("[data-testid='wind-row'] g[data-step='3']")).toBeNull();
    expect(screen.getByTestId("summary-wind")).toHaveTextContent("No wind data for this step");
    fireEvent.keyDown(scroller(), { key: "ArrowRight" });
    expect(screen.getByTestId("summary-partitions").querySelectorAll("li")).toHaveLength(1);
    expect(document.querySelectorAll("[data-testid='swell-partitions'] g[data-step='4'] [data-partition]")).toHaveLength(1);
  });

  it("copes with a run that has no partitions at all", () => {
    const tl = structuredClone(jbay());
    for (const s of tl.steps) s.partitions = []; // SYNTHETIC
    renderTimeline(tl);
    expect(screen.getByTestId("swell-partitions")).toHaveTextContent("The wave model sent no swell partitions for this run.");
    expect(screen.getByTestId("summary-swell")).toHaveTextContent("Total sea");
  });
});

describe("observations stay apart from the forecast", () => {
  it("shows a measured buoy height as measured, with station, distance and time, only with offshore Hs", () => {
    const tl = structuredClone(jbay());
    // SYNTHETIC measurement at the time of step 2.
    tl.observations = {
      status: "available",
      reason: null,
      items: [
        {
          t: tl.steps[2]!.t,
          kind: "instrument",
          station_code: "TEST1",
          station_name: "Synthetic test buoy",
          provider: "test",
          relation: "offshore_reference",
          distance_km: 41.6,
          hs_m: 2.3,
          hs_kind: "spectral_hm0",
          tp_s: 12,
          dir_from_deg: 230,
          wind_kmh: null,
          qc_flag: null,
          stale: false,
          licence: null,
          attribution: null,
        },
      ],
    };
    renderTimeline(tl);
    expect(screen.getByTestId("observations-note")).toHaveTextContent("Synthetic test buoy (42 km away, test): Hs 2.3 m at Mon 11:00 AM");
    expect(document.querySelector("[data-observation]")).toBeNull();
    fireEvent.click(screen.getByTestId("toggle-offshore"));
    expect(document.querySelector("[data-observation='TEST1']")).not.toBeNull();
    expect(document.querySelectorAll("[data-offshore-hs]")).toHaveLength(tl.steps.length);
  });

  it("shows nothing while the observations block is unavailable", () => {
    renderTimeline(jbay());
    expect(screen.queryByTestId("observations-note")).toBeNull();
  });
});

describe("15. no time is invented", () => {
  it("draws one mark per API step, only at API times; 6-hour steps are twice as wide as 3-hour ones", () => {
    const tl = mixed3h6h(jbay());
    renderTimeline(tl);
    const times = new Set(tl.steps.map((s) => String(Date.parse(s.t))));
    const rects = [...document.querySelectorAll<SVGRectElement>("[data-testid='quality-strip'] rect[data-step]")];
    expect(rects).toHaveLength(tl.steps.length);
    // Every mark in the data rows sits at a valid time of the API (the header's ticks are axis times).
    const marks = document.querySelectorAll(["quality-strip", "wave-bars", "swell-partitions", "wind-row"].map((id) => `[data-testid='${id}'] [data-t]`).join(", "));
    expect(marks.length).toBe(4 * tl.steps.length);
    for (const el of marks) expect(times.has(el.getAttribute("data-t")!)).toBe(true);
    const w = (i: number) => Number(rects[i]!.getAttribute("width")) + 1;
    expect(w(14) / w(3)).toBeCloseTo(2, 6);
    expect(screen.getByTestId("timeline-zone")).toHaveTextContent(/3-hour steps, from .* 6-hour steps · not interpolated/);
  });
});

describe("performance: hovering and selecting re-render no row", () => {
  it("renders the rows once; hover, click, keys and day buttons only move the cursor and the summary", () => {
    const tl = jbay();
    const renders: string[] = [];
    renderTimeline(tl, { onRowRender: (id) => renders.push(id) });
    expect(new Set(renders)).toEqual(new Set(["timeline-header", "quality-strip", "wave-bars", "swell-partitions", "wind-row", "tide-row"]));
    renders.length = 0;
    const content = scroller().firstElementChild!;
    for (const i of [2, 5, 9, 14]) fireEvent.pointerMove(content, { clientX: stepClientX(i), pointerType: "mouse" });
    fireEvent.click(qualityRect(10), { clientX: stepClientX(10) });
    fireEvent.keyDown(scroller(), { key: "ArrowRight" });
    fireEvent.keyDown(scroller(), { key: "PageDown" });
    fireEvent.click(screen.getByTestId("day-2026-10-12"));
    fireEvent.pointerLeave(content);
    expect(renders).toEqual([]);
    expect(document.querySelector("[data-cursor='selected']")).not.toBeNull();
  });
});
