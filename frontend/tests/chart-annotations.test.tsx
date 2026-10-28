import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SpotForecastTimeline } from "@/components/forecast/forecast-timeline";
import { SelectionStore, TimelineSelectionProvider } from "@/components/forecast/timeline-selection";
import { bestWindows, tideFit, tideFitRuns } from "@/lib/best-windows";
import { timelineModel } from "@/lib/timeline";
import { HEIGHT_UNIT_KEY, resetHeightUnitMemory, useHeightUnit } from "@/lib/units";
import type { SpotTimeline } from "@/types/api";

import { jbay, withSyntheticTide } from "./timeline-helpers";

// jbay() is the real 3-hourly J-Bay fixture; the tide fits added below are SYNTHETIC values
// that exist only to exercise the tide shading.

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 0, right: 1000, top: 0, bottom: 600, width: 1000, height: 600, x: 0, y: 0, toJSON: () => ({}) });
});
afterEach(() => {
  vi.restoreAllMocks();
  resetHeightUnitMemory();
  window.localStorage.removeItem(HEIGHT_UNIT_KEY);
});

/** SYNTHETIC: the tide counts in the rating, with these fits per step (null = not scored). */
function withTideFits(tl: SpotTimeline, fits: Array<number | null>): SpotTimeline {
  const out = withSyntheticTide(tl);
  out.steps.forEach((s, i) => {
    const f = fits[i % fits.length]!;
    s.why = { ...s.why, tide_included: f != null, fits: f == null ? { ...s.why.fits } : { ...s.why.fits, tide: f } };
  });
  return out;
}

/** The page's wiring: the height toggle writes the site-wide choice, which the page reads. */
function Page({ tl }: { tl: SpotTimeline }) {
  const [units] = useHeightUnit("ft");
  return <SpotForecastTimeline timeline={tl} units={units} />;
}

function renderTimeline(tl: SpotTimeline) {
  const store = new SelectionStore({ selectedT: Date.parse(tl.steps[3]!.t) });
  return render(
    <TimelineSelectionProvider initialT={null} store={store}>
      <Page tl={tl} />
    </TimelineSelectionProvider>,
  );
}

describe("best windows (spec 24)", () => {
  it("are the runs of daylight steps the API rated Good or better", () => {
    const model = timelineModel(jbay());
    const w = bestWindows(model.steps);
    // Fixture: step 1 very good (daylight), steps 3-4 good (daylight), step 9 good (daylight);
    // the very good night steps 0 and 22 and the good night steps 5-8 are not windows.
    expect(w.map((x) => [x.first, x.last])).toEqual([
      [1, 1],
      [3, 4],
      [9, 9],
    ]);
    expect(w[1]!.peak).toBe("good");
    expect(w[1]!.peakScore).toBe(63);
    expect(w[1]!.from).toBe(model.steps[3]!.x0);
    expect(w[1]!.to).toBe(model.steps[4]!.x1);
  });

  it("are bracketed under the quality strip, one bracket per window", () => {
    renderTimeline(jbay());
    const brackets = within(screen.getByTestId("quality-strip")).getAllByTestId("best-window");
    expect(brackets.map((b) => [b.getAttribute("data-first"), b.getAttribute("data-last")])).toEqual([
      ["1", "1"],
      ["3", "4"],
      ["9", "9"],
    ]);
    expect(screen.getByTestId("best-window-key")).toHaveTextContent("best window: daylight rated Good or better");
  });

  it("draws no bracket and no key when nothing is rated Good or better in daylight", () => {
    const tl = jbay();
    for (const s of tl.steps) s.quality = { ...s.quality, label: "fair", score: 45 };
    renderTimeline(tl);
    expect(screen.queryAllByTestId("best-window")).toHaveLength(0);
    expect(screen.queryByTestId("best-window-key")).toBeNull();
  });
});

describe("tide suitability (spec 24)", () => {
  it("classes each step by the score's own tide fit, only where the tide is rated", () => {
    const tl = withTideFits(jbay(), [0.9, 0.9, 0.5, 0.1, null]);
    const steps = timelineModel(tl).steps;
    expect(steps.slice(0, 5).map((s) => tideFit(s.step))).toEqual(["favourable", "favourable", "workable", "unfavourable", null]);
    const runs = tideFitRuns(steps);
    expect(runs.slice(0, 3).map((r) => [r.fit, r.first, r.last])).toEqual([
      ["favourable", 0, 1],
      ["workable", 2, 2],
      ["unfavourable", 3, 3],
    ]);
  });

  it("shades the tide row and keys the classes; nothing is shaded without a rated tide", () => {
    const { unmount } = renderTimeline(withTideFits(jbay(), [0.9, 0.5, 0.1]));
    const row = screen.getByTestId("tide-row");
    expect(row.querySelectorAll('[data-tide-fit="favourable"]').length).toBeGreaterThan(0);
    expect(row.querySelectorAll('[data-tide-fit="workable"]').length).toBeGreaterThan(0);
    expect(row.querySelectorAll('[data-tide-fit="unfavourable"]').length).toBeGreaterThan(0);
    expect(screen.getByTestId("tide-fit-key")).toHaveTextContent("Favorable tide");
    unmount();
    // The real fixture's tide is not part of its rating.
    renderTimeline(jbay());
    expect(screen.getByTestId("tide-row").querySelectorAll("[data-tide-fit]")).toHaveLength(0);
    expect(screen.getByTestId("tide-fit-key")).toHaveTextContent("Tide is not part of this break's rating");
  });
});

describe("ft / m toggle for everyone (spec 24)", () => {
  it("switches every height and is remembered in the browser", () => {
    renderTimeline(jbay());
    const group = screen.getByTestId("height-unit-toggle");
    expect(within(group).getByRole("button", { name: "ft" })).toHaveAttribute("aria-pressed", "true");
    const before = screen.getByTestId("summary-breaking").textContent;
    expect(before).toContain("ft");
    act(() => {
      fireEvent.click(within(group).getByRole("button", { name: "m" }));
    });
    expect(within(group).getByRole("button", { name: "m" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("summary-breaking").textContent).toContain(" m");
    expect(window.localStorage.getItem(HEIGHT_UNIT_KEY)).toBe("m");
  });
});
