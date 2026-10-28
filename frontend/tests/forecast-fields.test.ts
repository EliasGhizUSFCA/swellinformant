import { describe, expect, it } from "vitest";

import { combinedFreshness, forecastConfidenceOf, freshnessOf, hourOutputs, researchConfidenceOf } from "@/lib/forecast-fields";
import { updatedAgo } from "@/lib/format";
import { creditLinks, isWindySource, runTimeEstimated, WINDY_CREDIT, WINDY_URL } from "@/lib/provider";
import { conditionsSentence, hourReason } from "@/lib/rating-sentence";

// Every payload here is SYNTHETIC, shaped like the API's fields; none is a real forecast.

describe("hour outputs (algorithm 2.2.0), read defensively", () => {
  it("reads the lists, the offshore class and the model version", () => {
    const o = hourOutputs({ favourable: ["well-aligned swell", "light offshore wind"], unfavourable: [], offshore_class: "light offshore", model_version: "2.2.0" });
    expect(o).toEqual({ favourable: ["well-aligned swell", "light offshore wind"], unfavourable: [], offshoreClass: "light offshore", modelVersion: "2.2.0" });
  });

  it("reads absent, null or malformed fields as null, never a guess", () => {
    expect(hourOutputs(null)).toEqual({ favourable: null, unfavourable: null, offshoreClass: null, modelVersion: null });
    expect(hourOutputs({})).toEqual({ favourable: null, unfavourable: null, offshoreClass: null, modelVersion: null });
    expect(hourOutputs({ favourable: "not a list", unfavourable: null, offshore_class: 5, model_version: "" }).favourable).toBeNull();
    // Non-strings and duplicates are dropped from a list.
    expect(hourOutputs({ favourable: ["a", 3, "a", " ", null, "b"] }).favourable).toEqual(["a", "b"]);
  });
});

describe("separate confidences (spec 20)", () => {
  it("reads the forecast confidence object, else the older class string", () => {
    expect(forecastConfidenceOf({ forecast_confidence: { level: "moderate", kind: "heuristic", method: "forecast-v1.2" } })).toEqual({ level: "moderate", kind: "heuristic", method: "forecast-v1.2" });
    expect(forecastConfidenceOf({ forecast_confidence_class: "low" })).toEqual({ level: "low", kind: "heuristic", method: null });
    expect(forecastConfidenceOf({ forecast_confidence: { level: "certain" } })).toBeNull();
    expect(forecastConfidenceOf(undefined)).toBeNull();
  });

  it("reads research confidence on its own and never from the combined class", () => {
    expect(researchConfidenceOf({ research_confidence: "high" })).toBe("high");
    expect(researchConfidenceOf({ local_confidence_class: "low" })).toBeNull();
    expect(researchConfidenceOf({ research_confidence: "unknown" })).toBeNull();
  });
});

describe("freshness (design D10)", () => {
  it("reads the block and validates its values", () => {
    expect(freshnessOf({ freshness: { version_id: 12, last_updated_at: "2026-10-28T01:00:00Z", stale: true, reason: "refresh_failed" } })).toEqual({
      versionId: 12,
      lastUpdatedAt: "2026-10-28T01:00:00Z",
      stale: true,
      reason: "refresh_failed",
    });
    expect(freshnessOf({ freshness: { version_id: 0, last_updated_at: "yesterday", stale: "yes" } })).toEqual({ versionId: null, lastUpdatedAt: null, stale: false, reason: null });
    expect(freshnessOf({})).toBeNull();
    expect(freshnessOf(null)).toBeNull();
  });

  it("is stale when any payload says so", () => {
    const fresh = { freshness: { stale: false, last_updated_at: "2026-10-28T01:00:00Z" } };
    const stale = { freshness: { stale: true, last_updated_at: "2026-10-27T18:00:00Z" } };
    expect(combinedFreshness(fresh, stale)!.stale).toBe(true);
    expect(combinedFreshness(fresh, {})!.stale).toBe(false);
    expect(combinedFreshness(undefined, {})).toBeNull();
  });

  it('says "Forecast last updated 7 hours ago" in whole units', () => {
    const now = Date.parse("2026-10-28T08:30:00Z");
    expect(updatedAgo("2026-10-28T01:00:00Z", now)).toBe("7 hours ago");
    expect(updatedAgo("2026-10-28T07:30:00Z", now)).toBe("1 hour ago");
    expect(updatedAgo("2026-10-28T08:18:00Z", now)).toBe("12 minutes ago");
    expect(updatedAgo("2026-10-28T08:29:50Z", now)).toBe("just now");
    expect(updatedAgo("2026-10-28T09:00:00Z", now)).toBe("just now"); // a clock behind the server's
    expect(updatedAgo("2026-10-25T08:00:00Z", now)).toBe("3 days ago");
    expect(updatedAgo("2026-10-26T10:00:00Z", now)).toBe("46 hours ago");
    expect(updatedAgo("not a time", now)).toBe("just now");
  });
});

describe("the rating sentence", () => {
  it("joins the favourable conditions, then the unfavourable ones after 'but'", () => {
    expect(conditionsSentence(["well-aligned swell", "light offshore wind"], [], "ft", "km/h")).toBe("Well-aligned swell and light offshore wind.");
    expect(conditionsSentence(["well-aligned swell", "good swell period (14 s)", "light offshore wind"], ["unfavourable tide stage"], "ft", "km/h")).toBe(
      "Well-aligned swell, good swell period (14 s) and light offshore wind, but unfavorable tide stage.",
    );
    expect(conditionsSentence([], ["onshore wind 25 km/h"], "ft", "km/h")).toBe("Onshore wind 25 km/h.");
    expect(conditionsSentence([], [], "ft", "km/h")).toBeNull();
    expect(conditionsSentence(null, undefined, "ft", "km/h")).toBeNull();
  });

  it("shows the backend's fixed units in the viewer's", () => {
    expect(conditionsSentence(["good size for this break (4–6 ft)"], ["onshore wind 25 km/h"], "m", "mph")).toBe("Good size for this break (1.2–1.8 m), but onshore wind 16 mph.");
  });

  it("falls back to today's factor wording for hours scored before the lists existed", () => {
    const fallback = { periodS: 15, windRelation: "offshore", windSpeedKmh: 8 };
    expect(hourReason({}, fallback, "ft", "km/h")).toBe("Long-period groundswell and light offshore winds.");
    expect(hourReason({ favourable: [], unfavourable: [] }, fallback, "ft", "km/h")).toBe("Long-period groundswell and light offshore winds.");
    expect(hourReason({ favourable: ["glassy, near-calm wind"] }, fallback, "ft", "km/h")).toBe("Glassy, near-calm wind.");
    expect(hourReason(null, null, "ft", "km/h")).toBeNull();
  });
});

describe("provider credit and run times (design D3)", () => {
  it("names Windy's link by the credit its terms ask for and adds it for Windy data", () => {
    const om = { label: "Weather data by Open-Meteo.com", href: "https://open-meteo.com/" };
    const cc = { label: "CC BY 4.0", href: "https://creativecommons.org/licenses/by/4.0/" };
    expect(creditLinks([om, cc])).toEqual([om, cc]);
    expect(creditLinks([{ label: "Windy.com", href: "https://www.windy.com/" }])).toEqual([{ label: WINDY_CREDIT, href: "https://www.windy.com/" }]);
    expect(creditLinks([], "windy_gfswave")).toEqual([{ label: WINDY_CREDIT, href: WINDY_URL }]);
    expect(creditLinks([{ label: "Windy.com", href: "https://www.windy.com/" }], "windy_gfswave")).toHaveLength(1);
    expect(creditLinks(null, "open_meteo_noaa")).toEqual([]);
    // A look-alike host is not Windy.
    expect(creditLinks([{ label: "x", href: "https://notwindy.com.example/" }])[0]!.label).toBe("x");
  });

  it("marks Windy run times estimated unless the version says otherwise", () => {
    expect(isWindySource("windy_gfswave")).toBe(true);
    expect(isWindySource("open_meteo_noaa")).toBe(false);
    expect(runTimeEstimated("windy_gfswave")).toBe(true);
    expect(runTimeEstimated("open_meteo_noaa")).toBe(false);
    expect(runTimeEstimated("open_meteo_noaa", true)).toBe(true);
    expect(runTimeEstimated("windy_gfswave", false)).toBe(false);
  });
});
