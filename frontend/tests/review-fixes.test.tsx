/**
 * Integration review fixes on the spot page and cards (UX-01..13, PHYS-2/5/6/7, SEC-1..3).
 * Every payload here is the recorded J-Bay fixture or a SYNTHETIC variant of it.
 */
import { act, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AttributionLinks, mergeAttributionLinks } from "@/components/forecast-provenance";
import { SpotForecastTimeline } from "@/components/forecast/forecast-timeline";
import { SelectedTimeSummary } from "@/components/forecast/selected-summary";
import { TideSourceNote } from "@/components/forecast/tide-row";
import { SelectionStore, TimelineSelectionProvider } from "@/components/forecast/timeline-selection";
import { QUALITY_COLORS } from "@/lib/quality";
import { datedTime, interpolationText, relationRuns, shortTime, stepExtents, timelineModel, zoneChanges } from "@/lib/timeline";
import { explanationText } from "@/lib/units";
import type { SpotTimeline } from "@/types/api";

import { jbay, lisbonFallBack, patchStep } from "./timeline-helpers";

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 0, right: 1000, top: 0, bottom: 600, width: 1000, height: 600, x: 0, y: 0, toJSON: () => ({}) });
});
afterEach(() => vi.restoreAllMocks());

function renderTimeline(tl: SpotTimeline, select = 3) {
  const store = new SelectionStore({ selectedT: Date.parse(tl.steps[select]!.t) });
  render(
    <TimelineSelectionProvider initialT={null} store={store}>
      <SpotForecastTimeline timeline={tl} units="ft" />
    </TimelineSelectionProvider>,
  );
  return store;
}

function renderSummary(tl: SpotTimeline, i: number) {
  const store = new SelectionStore({ selectedT: Date.parse(tl.steps[i]!.t) });
  render(
    <TimelineSelectionProvider initialT={null} store={store}>
      <SelectedTimeSummary model={timelineModel(tl)} timeline={tl} units="ft" windUnit="km/h" />
    </TimelineSelectionProvider>,
  );
}

/** WCAG relative luminance of a #rrggbb colour. */
function luminance(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0]! + 0.7152 * c[1]! + 0.0722 * c[2]!;
}

describe("UX-01 night steps keep the quality ramp's order", () => {
  it("draws night steps at full colour as a shorter inset bar, never faded", () => {
    const tl = jbay();
    renderTimeline(tl);
    const rects = [...document.querySelectorAll<SVGRectElement>('[data-testid="quality-strip"] rect[data-step]')];
    const night = rects.filter((r) => r.hasAttribute("data-night"));
    const day = rects.filter((r) => !r.hasAttribute("data-night"));
    expect(night.length).toBeGreaterThan(0);
    expect(day.length).toBeGreaterThan(0);
    for (const r of rects) {
      expect(r.getAttribute("fill-opacity") ?? r.getAttribute("fillOpacity")).toBeNull();
      expect(r.getAttribute("fill")).toBe(QUALITY_COLORS[r.getAttribute("data-label") as keyof typeof QUALITY_COLORS]);
    }
    expect(Number(night[0]!.getAttribute("height"))).toBeLessThan(Number(day[0]!.getAttribute("height")));
    // The ramp itself: a better class is always darker, so a night Exceptional can never read
    // lighter than a daylight Poor (it did at 45 % opacity: 0.305 against 0.273).
    expect(luminance(QUALITY_COLORS.exceptional)).toBeLessThan(luminance(QUALITY_COLORS.poor));
    expect(screen.getByTestId("quality-chart-legend")).toHaveTextContent("thin bar = night");
  });
});

describe("UX-02 the wind relation is labelled in every day", () => {
  it("cuts relation runs at local midnights", () => {
    const tl = jbay();
    const model = timelineModel(tl);
    const whole = relationRuns(model.steps);
    const daily = relationRuns(model.steps, model.days.map((d) => d.start));
    expect(daily.length).toBeGreaterThan(whole.length);
    for (const d of model.days) for (const r of daily) expect(r.from < d.start && r.to > d.start).toBe(false);
    // Pieces of one run keep its relation and together cover it exactly.
    const covered = daily.reduce((n, r) => n + (r.to - r.from), 0);
    expect(covered).toBe(whole.reduce((n, r) => n + (r.to - r.from), 0));
  });
});

describe("UX-03 compact readout", () => {
  it("shows the selected step's time, rating, breaking range and wind in one line and follows the selection", () => {
    const tl = jbay();
    const store = renderTimeline(tl, 3);
    const readout = screen.getByTestId("compact-readout");
    expect(readout).toHaveAttribute("aria-hidden", "true");
    expect(readout.className).toMatch(/sticky/);
    expect(readout.className).toMatch(/lg:hidden/);
    expect(readout).toHaveTextContent(shortTime(Date.parse(tl.steps[3]!.t), tl.timezone));
    act(() => store.select(Date.parse(tl.steps[9]!.t)));
    expect(screen.getByTestId("compact-readout")).toHaveTextContent(shortTime(Date.parse(tl.steps[9]!.t), tl.timezone));
  });
});

describe("UX-04 and UX-13 dates and short forms", () => {
  it("names the date in step times and spells out the chart's short forms", () => {
    expect(datedTime(Date.parse("2026-10-23T09:00:00Z"), "Africa/Johannesburg")).toBe("Fri Oct 23, 11:00 AM");
    renderTimeline(jbay());
    const legend = screen.getByTestId("quality-chart-legend");
    expect(legend).toHaveTextContent("Excellent (Ex)");
    expect(legend).toHaveTextContent("Exceptional (Ex+)");
    expect(screen.getByTestId("wind-relation-key")).toHaveTextContent("X-off = cross-offshore");
  });
});

describe("PHYS-7 a clock change inside the window", () => {
  it("names both zones in the caption", () => {
    const tl = lisbonFallBack(jbay());
    const model = timelineModel(tl);
    const changes = zoneChanges(model.times, tl.timezone);
    expect(changes).toHaveLength(1);
    expect(changes[0]!.t).toBe(Date.parse("2026-10-25T01:00:00Z"));
    renderTimeline(tl, 0);
    expect(screen.getByTestId("timeline-zone").textContent).toContain(`, ${changes[0]!.label} from `);
  });
});

describe("PHYS-2 interpolation is named", () => {
  it("says which blend member was aligned in time, and never 'not interpolated'", () => {
    const tl = patchStep(jbay(), 4, (s) => {
      s.interpolated = true;
      s.interpolated_members = ["open_meteo_ecmwf"];
    });
    tl.provenance = { ...tl.provenance!, members: [{ code: "open_meteo_ecmwf", name: "Open-Meteo ECMWF (SYNTHETIC)", family: "ecmwf_ifs", run_key: "k", init_time: null, wave_model: null, atmosphere_model: null }] };
    expect(interpolationText(tl.steps[4]!, tl.provenance.members)).toBe("ECMWF value aligned in time");
    expect(interpolationText(tl.steps[3]!, tl.provenance.members)).toBeNull();
    renderSummary(tl, 4);
    expect(screen.getByTestId("summary-interpolated")).toHaveTextContent("ECMWF value aligned in time");
  });
});

describe("PHYS-1 and PHYS-6 wind notes", () => {
  it("names a model disagreement on the wind and a gust the blend put below the mean", () => {
    const tl = patchStep(jbay(), 3, (s) => {
      s.wind = { ...s.wind!, speed_kmh: 14.5, gust_kmh: 10.8, gust_below_mean: true, gust_below_mean_cause: "blend", models_disagree: ["direction"] };
    });
    renderSummary(tl, 3);
    expect(screen.getByTestId("summary-gust")).toHaveTextContent("gust 11 km/h (from fewer models than the mean wind)");
    expect(screen.getByTestId("summary-gust")).not.toHaveTextContent("in the source");
    expect(screen.getByTestId("summary-wind-disagree")).toHaveTextContent("Models disagree on the wind direction");
  });
});

describe("PHYS-5 and UX-09 the explanation in the viewer's units", () => {
  it("converts the engine's metres, km/h and spelling, and nothing else", () => {
    const text = "2.4 m @ 13 s SW swell, light offshore wind 17 km/h from E, favourable tide; approx. 4–6 ft faces vs. a working range of 3–8 ft";
    expect(explanationText(text, "ft", "mph")).toBe("7.9 ft @ 13 s SW swell, light offshore wind 11 mph from E, favorable tide; approx. 4–6 ft faces vs. a working range of 3–8 ft");
    expect(explanationText(text, "m", "km/h")).toBe("2.4 m @ 13 s SW swell, light offshore wind 17 km/h from E, favorable tide; approx. 1.2–1.8 m faces vs. a working range of 0.9–2.4 m");
    expect(explanationText("Unfavourable wind", "ft", "km/h")).toBe("Unfavorable wind");
  });
});

describe("SEC-1 to SEC-3 credits next to the data", () => {
  it("merges the events' links once and links a tide's licence and dataset", () => {
    const om = { label: "Weather data by Open-Meteo.com", href: "https://open-meteo.com/" };
    const cc = { label: "CC BY 4.0", href: "https://creativecommons.org/licenses/by/4.0/" };
    expect(mergeAttributionLinks([{ attribution_links: [om, cc] }, { attribution_links: [om] }, {}])).toEqual([om, cc]);
    render(<AttributionLinks links={mergeAttributionLinks([{ attribution_links: [om] }])} />);
    expect(screen.getByRole("link", { name: om.label })).toHaveAttribute("href", om.href);

    const tide = { ...jbay().tide, status: "available" as const, attribution: "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE. Modified: … (CC BY 4.0)", attribution_links: [{ label: "EOT20 dataset (SEANOE)", href: "https://doi.org/10.17882/79489" }, cc] };
    render(<TideSourceNote tide={tide} />);
    const note = screen.getByTestId("tide-source");
    expect(within(note).getByRole("link", { name: "EOT20 dataset (SEANOE)" })).toHaveAttribute("href", "https://doi.org/10.17882/79489");
    expect(within(note).getByRole("link", { name: "CC BY 4.0" })).toHaveAttribute("href", cc.href);
  });

  it("credits a buoy reading by provider name with its licence link (IMOS / AODN, CC BY 4.0)", () => {
    const tl = structuredClone(jbay());
    tl.observations = {
      status: "available",
      reason: null,
      items: [
        {
          t: tl.steps[2]!.t,
          kind: "instrument",
          station_code: "aodn:TEST",
          station_name: "Synthetic AODN buoy",
          provider: "aodn",
          relation: "offshore_reference",
          distance_km: 12,
          hs_m: 1.5,
          hs_kind: "spectral_hm0",
          tp_s: 11,
          dir_from_deg: 200,
          wind_kmh: null,
          qc_flag: null,
          stale: false,
          licence: "CC BY 4.0",
          attribution: "Data was sourced from Australia's Integrated Marine Observing System (IMOS) (SYNTHETIC test text)",
          provider_name: "IMOS / AODN",
          licence_url: "https://creativecommons.org/licenses/by/4.0/",
        },
      ],
    };
    renderTimeline(tl);
    const credit = screen.getByTestId("observation-credit");
    expect(credit).toHaveTextContent("Data: IMOS / AODN · CC BY 4.0");
    expect(within(credit).getByRole("link", { name: "CC BY 4.0" })).toHaveAttribute("href", "https://creativecommons.org/licenses/by/4.0/");
    expect(credit).toHaveAttribute("title", expect.stringContaining("Integrated Marine Observing System"));
    expect(screen.getByTestId("observations-note")).not.toHaveTextContent("aodn)");
  });
});

describe("step extents (unchanged by the review)", () => {
  it("still centre every mark on its valid time", () => {
    const steps = stepExtents(jbay().steps);
    for (const s of steps.slice(1, -1)) expect((s.x0 + s.x1) / 2).toBe(s.t);
  });
});
