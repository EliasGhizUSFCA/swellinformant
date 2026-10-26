import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SelectedTimeSummary } from "@/components/forecast/selected-summary";
import { SelectionStore, TimelineSelectionProvider } from "@/components/forecast/timeline-selection";
import { QUALITY_LABELS } from "@/lib/quality";
import { clockTime, localDate, partitionText, selectedTimeTitle, shortTime, tideHeightText, timelineModel } from "@/lib/timeline";
import { explanationText, formatHeightRange, formatWind } from "@/lib/units";
import type { SpotTimeline } from "@/types/api";

import { jbay, patchStep, withSyntheticTide } from "./timeline-helpers";

function renderSummary(tl: SpotTimeline, i: number, windUnit: "km/h" | "mph" = "km/h", units: "ft" | "m" = "ft") {
  const store = new SelectionStore({ selectedT: Date.parse(tl.steps[i]!.t) });
  render(
    <TimelineSelectionProvider initialT={null} store={store}>
      <SelectedTimeSummary model={timelineModel(tl)} timeline={tl} units={units} windUnit={windUnit} />
    </TimelineSelectionProvider>,
  );
  return store;
}

describe("selected-time summary", () => {
  it("shows the selected step in the spot's zone with its rating, waves, swell, wind, tide and confidence", () => {
    const tl = jbay();
    const s = tl.steps[3]!; // 2026-10-12T12:00Z, 2:00 PM SAST
    renderSummary(tl, 3);
    expect(screen.getByTestId("summary-time")).toHaveTextContent("Monday, Oct 12 · 2:00 PM SAST");
    expect(screen.getByTestId("summary-step-length")).toHaveTextContent("3-hour step");
    expect(screen.getByTestId("summary-quality")).toHaveTextContent(`${QUALITY_LABELS.good}${(s.quality.score / 10).toFixed(1)}/10`);
    expect(screen.getByTestId("summary-breaking")).toHaveTextContent("7.5–9 ft");
    // The surf-driving system first, marked, then the others; heights in the viewer's unit.
    const rows = screen.getByTestId("summary-partitions").querySelectorAll("li");
    expect(rows[0]).toHaveAttribute("data-partition", "primary_swell");
    expect(rows[0]).toHaveTextContent(`Primary swell${partitionText(s.partitions[0]!, "ft")}drives the surf`);
    expect(rows[0]).toHaveTextContent("5.2 ft · 12 s · SW 225°");
    expect(rows).toHaveLength(4);
    expect(screen.getByTestId("summary-wind")).toHaveTextContent("22 km/h from SW 232°");
    expect(screen.getByTestId("summary-wind-relation")).toHaveTextContent("Cross-offshore");
    expect(screen.getByTestId("summary-gust")).toHaveTextContent("gust 39 km/h");
    expect(screen.getByTestId("summary-tide")).toHaveTextContent("Tide data unavailable");
    // Forecast confidence and confidence at the break, separately; research is not merged in.
    expect(screen.getByTestId("summary-forecast-confidence")).toHaveTextContent("Forecast confidence: High");
    expect(screen.getByTestId("summary-local-confidence")).toHaveTextContent("Confidence at the break: Moderate");
    expect(screen.getByTestId("summary-confidence")).not.toHaveTextContent(/research/i);
    expect(screen.getByTestId("summary-confidence")).toHaveTextContent("A 1-day forecast.");
  });

  it("puts a wind sea that drives the surf first, as the API marks it", () => {
    const tl = patchStep(jbay(), 3, (s) => {
      // SYNTHETIC: the wind sea marked as the surf-driving system.
      for (const p of s.partitions) p.drives_surf = p.kind === "wind_sea";
    });
    renderSummary(tl, 3);
    const rows = screen.getByTestId("summary-partitions").querySelectorAll("li");
    expect(rows[0]).toHaveAttribute("data-partition", "wind_sea");
    expect(rows[0]).toHaveAttribute("data-drives", "true");
  });

  it("follows the selection and the units", () => {
    const tl = withSyntheticTide(jbay());
    const store = renderSummary(tl, 3, "mph", "m");
    act(() => store.select(Date.parse(tl.steps[6]!.t)));
    expect(screen.getByTestId("summary-time")).toHaveTextContent(selectedTimeTitle(Date.parse(tl.steps[6]!.t), tl.timezone));
    expect(screen.getByTestId("summary-wind")).toHaveTextContent(formatWind(tl.steps[6]!.wind!.speed_kmh, "mph"));
    const b = tl.steps[6]!.breaking;
    expect(screen.getByTestId("summary-breaking")).toHaveTextContent(`${formatHeightRange(b.typical_ft, b.sets_ft, "m")}typical–sets`);
    expect(screen.getByTestId("summary-tide")).toHaveTextContent(/^Tide-?\d\.\d m · (rising|falling)above model mean sea level · demo tide \(synthetic\)Next (high|low) -?\d\.\d m at .+$/);
    // The next high or low water after the selected step, from the block's events.
    const next = tl.tide.events.find((e) => Date.parse(e.t) > Date.parse(tl.steps[6]!.t))!;
    // (with the weekday when it falls on the next local day)
    const when = localDate(Date.parse(next.t), tl.timezone) === localDate(Date.parse(tl.steps[6]!.t), tl.timezone) ? clockTime(Date.parse(next.t), tl.timezone) : shortTime(Date.parse(next.t), tl.timezone);
    expect(screen.getByTestId("summary-next-tide")).toHaveTextContent(`Next ${next.kind} ${tideHeightText(next.height_m, "m")} at ${when}`);
    expect(screen.getByTestId("summary-tide")).toHaveTextContent(next.kind === "high" ? "rising" : "falling");
    fireEvent.click(screen.getByRole("button", { name: "Previous forecast step" }));
    expect(screen.getByTestId("summary-time")).toHaveTextContent(selectedTimeTitle(Date.parse(tl.steps[5]!.t), tl.timezone));
    fireEvent.click(screen.getByRole("button", { name: "Next forecast step" }));
    fireEvent.click(screen.getByRole("button", { name: "Next forecast step" }));
    expect(screen.getByTestId("summary-time")).toHaveTextContent(selectedTimeTitle(Date.parse(tl.steps[7]!.t), tl.timezone));
  });

  it("marks night steps", () => {
    const tl = jbay();
    renderSummary(tl, 0); // 03:00Z, before first light
    expect(screen.getByTestId("selected-summary")).toHaveTextContent("Night");
  });

  it("explains the rating only with what the scoring engine stored", () => {
    const tl = patchStep(jbay(), 3, (s) => {
      s.why.contributions = { wind: 6.2, swell_period: 14.5 }; // SYNTHETIC points lost
    });
    renderSummary(tl, 3);
    fireEvent.click(screen.getByRole("button", { name: "Why this forecast?" }));
    const why = tl.steps[3]!.why;
    // The engine's sentence, with its numbers in the viewer's units (ft and km/h here).
    expect(screen.getByTestId("why-explanation")).toHaveTextContent(explanationText(why.explanation, "ft", "km/h"));
    expect(screen.getByTestId("why-explanation").textContent).not.toMatch(/\d m @/);
    const fits = screen.getByTestId("why-fits").querySelectorAll("li");
    expect([...fits].map((li) => [li.getAttribute("data-fit"), Number(li.getAttribute("data-value"))])).toEqual(
      ["swell_direction", "swell_period", "wave_size", "wind", "consistency"].map((k) => [k, why.fits[k]]),
    );
    // Only the gates below 1 held the score back (period 0.851 for this step).
    expect(screen.getByTestId("why-gates")).toHaveTextContent("Held back by swell period (score × 0.85).");
    expect(screen.getByTestId("why-contributions")).toHaveTextContent("Points lost: swell period −14.5, wind −6.2.");
    expect(screen.getByTestId("why-this-forecast")).toHaveTextContent("Tide is not part of this rating.");
  });
});
