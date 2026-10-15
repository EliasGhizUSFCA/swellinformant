import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { leadText, LegendDrawer } from "@/components/map/legend-drawer";
import { publishMapFlowStatus } from "@/hooks/map-fields";
import { LAYER_SCALES } from "@/lib/map-colors";
import type { FieldIndex } from "@/lib/map-fields";
import { DEFAULT_SETTINGS } from "@/lib/map-settings";

// The index below is SYNTHETIC test data.
const INDEX: FieldIndex = {
  available: true,
  message: null,
  source: { code: "synthetic", name: "Synthetic test fields", attribution: "Synthetic test data, not a forecast", is_demo: true },
  run_key: "run-1",
  cycle_time: "2026-10-14T06:00:00Z",
  valid_times: ["2026-10-14T12:00:00Z"],
  variables: [],
  updated_at: "2026-10-14T09:00:00Z",
  age_hours: 4,
  stale_after_hours: 12,
  stale: false,
};

const legend = (index: FieldIndex, layer: "hs" | "wind" = "hs", windOverlay = false) =>
  render(
    <LegendDrawer
      settings={{ ...DEFAULT_SETTINGS, layer, windOverlay }}
      scale={LAYER_SCALES[layer]}
      units={{ height: "m", wind: "mph" }}
      index={index}
      particles="animated"
      onClose={() => undefined}
    />,
  );

afterEach(() => {
  act(() => publishMapFlowStatus({ systems: [], waveMode: "off", validTime: null, zoom: 2, centreLat: 15 }));
});

describe("map legend", () => {
  it("keys the crest systems drawn, primary first, with the stated scales and conventions", () => {
    act(() => publishMapFlowStatus({ systems: ["secondary", "primary"], waveMode: "animated", validTime: "2026-10-14T12:00:00Z", zoom: 2, centreLat: 0 }));
    legend(INDEX);
    const items = screen.getByRole("list", { name: "Wave systems drawn" }).querySelectorAll("li");
    expect([...items].map((li) => li.textContent)).toEqual(["Primary swell (solid)", "Secondary swell (dashed)"]);
    expect(screen.getByTestId("legend-scales").textContent).toBe("Crest spacing ×4,700 at the map centre; time runs 4× faster than real.");
    expect(screen.getByTestId("legend-crests").textContent).toContain("Stylised crests from the forecast model, not individual waves.");
    expect(screen.getByTestId("legend-convention").textContent).toContain("Arrows point where the wind or waves are going.");
    expect(screen.getByTestId("legend-valid").textContent).toMatch(/^Valid .+ · \+6 h \(forecast, not observed\)$/);
    expect(screen.queryByTestId("legend-wind")).toBeNull();
    expect(screen.queryByTestId("legend-stale")).toBeNull();
  });

  it("explains the wind streaks on the Wind layer and with the overlay, in the chosen unit", () => {
    act(() => publishMapFlowStatus({ systems: [], waveMode: "off", validTime: "2026-10-14T06:00:00Z" }));
    legend(INDEX, "wind");
    expect(screen.getByTestId("legend-title").textContent).toBe("Wind speed (mph)");
    expect(screen.getByTestId("legend-wind").textContent).toContain("visual speed (not ground speed)");
    expect(screen.queryByTestId("legend-crests")).toBeNull();
    // At the cycle time itself the field is the model's analysis: not a forecast, not observed.
    expect(screen.getByTestId("legend-valid").textContent).toMatch(/ · model analysis \(not observed\)$/);
    expect(screen.getByTestId("legend-valid").textContent).not.toContain("forecast");
  });

  it("says when the model run is out of date", () => {
    legend({ ...INDEX, stale: true, age_hours: 30.4 });
    expect(screen.getByTestId("legend-stale").textContent).toBe("Out of date: this model run is 30 h old.");
  });

  it("states the lead time from the model cycle", () => {
    expect(leadText("2026-10-15T12:00:00Z", "2026-10-14T06:00:00Z")).toBe("+30 h");
    expect(leadText("2026-10-14T06:00:00Z", "2026-10-14T06:00:00Z")).toBe("analysis");
    expect(leadText(null, "2026-10-14T06:00:00Z")).toBeNull();
  });
});
