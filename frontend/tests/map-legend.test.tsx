import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { leadText, LegendDrawer } from "@/components/map/legend-drawer";
import { publishMapFlowStatus } from "@/hooks/map-fields";
import { colorAtValue, LAYER_SCALES, rgbToHex, WAVE_HEIGHT_RAMP } from "@/lib/map-colors";
import type { FieldIndex } from "@/lib/map-fields";
import { DEFAULT_SETTINGS } from "@/lib/map-settings";
import { BLOB_PROFILE, dashAlpha, dashTint } from "@/lib/map-waves";

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
  act(() => publishMapFlowStatus({ systems: [], validTime: null, zoom: 2, centreLat: 15 }));
});

describe("map legend", () => {
  it("keys the crest systems drawn, primary first, with the stated scales and conventions", () => {
    act(() => publishMapFlowStatus({ systems: ["secondary", "primary"], validTime: "2026-10-14T12:00:00Z", zoom: 2, centreLat: 0 }));
    legend(INDEX);
    const items = screen.getByRole("list", { name: "Wave systems drawn" }).querySelectorAll("li");
    expect([...items].map((li) => li.textContent)).toEqual(["Primary swell (light tinted blobs)", "Secondary swell (split amber bars)"]);
    // Each system's swatch is drawn in its own dash shape on a patch of a typical (2.5 m) sea:
    // two soft blobs with the map's falloff for the primary, two split bars for the secondary.
    const swatch = (system: string) => screen.getByTestId("legend-crests").querySelector(`svg[data-swatch="${system}"]`)!;
    expect(swatch("primary").querySelectorAll("rect[data-blob]").length).toBe(2);
    expect(swatch("primary").querySelectorAll("rect[data-bar]").length).toBe(0);
    expect(swatch("secondary").querySelectorAll("rect[data-bar]").length).toBe(2 * 2); // each dash split in two
    expect(swatch("secondary").querySelectorAll("rect[data-blob]").length).toBe(0);
    const stops = [...swatch("primary").querySelectorAll("linearGradient stop")];
    expect(stops.map((st) => Number(st.getAttribute("offset")))).toEqual(BLOB_PROFILE.map((p) => p.at));
    // As on the map (and in the reference, about 5 px along the crest by 7 along the travel), a
    // blob reaches further along its travel (the swatch's height) than along its crest, and it
    // is drawn at the opacity of the 2.5 m sea under it.
    for (const blob of swatch("primary").querySelectorAll("rect[data-blob]")) {
      expect(Number(blob.getAttribute("height"))).toBeGreaterThan(Number(blob.getAttribute("width")));
    }
    stops.forEach((st, k) => expect(Number(st.getAttribute("stop-opacity"))).toBeCloseTo(dashAlpha(2.5) * BLOB_PROFILE[k]!.alpha, 9));
    // The blobs are a lighter tint of the sea patch under them, not white.
    const sea = swatch("primary").querySelector("rect:not([data-blob])")!.getAttribute("fill");
    expect(sea).toBe(rgbToHex(colorAtValue(WAVE_HEIGHT_RAMP, 2.5)));
    expect(stops[0]!.getAttribute("stop-color")).toBe(`rgb(${dashTint(colorAtValue(WAVE_HEIGHT_RAMP, 2.5), "primary").join(", ")})`);
    expect(stops[0]!.getAttribute("stop-color")).not.toBe("rgb(255, 255, 255)");
    expect(screen.getByTestId("legend-crests").textContent).toContain("the bright edge is the crest, the fading smear behind it where it just was");
    expect(screen.getByTestId("legend-crests").textContent).toContain("each is a lighter tint of the colour under it");
    expect(screen.getByTestId("legend-crests").textContent).toContain("Short dashes are wave crests moving with the waves");
    expect(screen.getByTestId("legend-scales").textContent).toBe("Crest spacing ×4,700 at the map centre; time runs 4× faster than real.");
    expect(screen.getByTestId("legend-crests").textContent).toContain("Stylised crests from the forecast model, not individual waves.");
    expect(screen.getByTestId("legend-convention").textContent).toContain("Arrows point where the wind or waves are going.");
    expect(screen.getByTestId("legend-valid").textContent).toMatch(/^Valid .+ · \+6 h \(forecast, not observed\)$/);
    expect(screen.queryByTestId("legend-wind")).toBeNull();
    expect(screen.queryByTestId("legend-stale")).toBeNull();
  });

  it("explains the wind streaks on the Wind layer and with the overlay, in the chosen unit", () => {
    act(() => publishMapFlowStatus({ systems: [], validTime: "2026-10-14T06:00:00Z" }));
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
