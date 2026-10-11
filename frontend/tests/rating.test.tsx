import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RatingHeadline, WhyThisRating } from "@/components/rating";
import { componentFits, limitingGates, ratingFactors, ratingHeadline, ratingReason, ratingTitle, score10 } from "@/lib/rating";

// Factor values below are SYNTHETIC inputs shaped like the API's fields.

describe("score out of ten", () => {
  it("divides the 0–100 score by ten with one decimal", () => {
    expect(score10(89)).toBe("8.9");
    expect(score10(90)).toBe("9.0");
    expect(score10(57)).toBe("5.7");
    expect(score10(0)).toBe("0.0");
    expect(score10(100)).toBe("10.0");
    expect(score10(88.6)).toBe("8.9");
    expect(score10(140)).toBe("10.0");
    expect(score10(-5)).toBe("0.0");
  });

  it("builds the headline from the API label", () => {
    expect(ratingTitle("excellent")).toBe("Excellent Surf");
    expect(ratingTitle("very_good")).toBe("Very Good Surf");
    expect(ratingHeadline("excellent", 89)).toBe("Excellent Surf — 8.9/10");
    expect(ratingHeadline("poor", 12)).toBe("Poor Surf — 1.2/10");
  });
});

describe("one-line reason", () => {
  it("names period, direction fit and wind from the components", () => {
    expect(ratingReason({ periodS: 16, windRelation: "offshore", windSpeedKmh: 8, components: { swell_direction: 0.92 } })).toBe(
      "Long-period groundswell, favorable direction, and light offshore winds.",
    );
  });

  it("reads direction fit and size from the explanation text when components are absent", () => {
    const explanation =
      "1.8 m @ 15 s SW swell, well aligned with the break; offshore wind 14 km/h from NE; approx. 5–8 ft faces vs. a working range of 3–12 ft; favourable tide.";
    expect(ratingReason({ periodS: 15, windRelation: "offshore", windSpeedKmh: 14, explanation })).toBe(
      "Long-period groundswell, favorable direction, and offshore winds.",
    );
  });

  it("names limiting size and tide only when they hold the rating back", () => {
    const explanation =
      "0.6 m @ 7 s W swell, at the edge of the break's swell window; onshore wind 30 km/h from W; approx. 1–2 ft faces vs. a working range of 3–10 ft; unfavourable tide.";
    expect(ratingReason({ periodS: 7, windRelation: "onshore", windSpeedKmh: 30, explanation })).toBe(
      "Short-period swell, direction at the edge of the swell window, onshore winds, and waves below the break's usual size range.",
    );
    const factors = ratingFactors({ periodS: 7, windRelation: "onshore", windSpeedKmh: 30, explanation });
    expect(factors.find((f) => f.key === "tide")).toEqual({ key: "tide", phrase: "unfavorable tide", tone: "bad" });
  });

  it("uses the spot's working range when given", () => {
    expect(
      ratingReason({ periodS: 11, windRelation: "glassy", windSpeedKmh: 3, breakingMinFt: 14, breakingMaxFt: 18, spotMinFt: 4, spotMaxFt: 12 }),
    ).toBe("Mid-period swell, glassy, near-calm conditions, and waves above the break's usual size range.");
  });

  it("says strong wind and skips unknown factors", () => {
    expect(ratingReason({ periodS: null, windRelation: "cross-shore", windSpeedKmh: 40 })).toBe("Strong cross-shore winds.");
    expect(ratingReason({ windRelation: "unknown" })).toBeNull();
    expect(ratingReason({})).toBeNull();
  });
});

describe("rating components", () => {
  it("renders the headline and reason", () => {
    render(<RatingHeadline label="excellent" score={89} reason="Long-period groundswell, favorable direction, and light offshore winds." />);
    expect(screen.getByTestId("rating-title")).toHaveTextContent("Excellent Surf — 8.9/10");
    expect(screen.getByTestId("rating-reason")).toHaveTextContent("Long-period groundswell, favorable direction, and light offshore winds.");
  });

  it("omits the reason line when there is none", () => {
    render(<RatingHeadline label="fair" score={44} reason={null} />);
    expect(screen.queryByTestId("rating-reason")).toBeNull();
  });

  it("puts factor details and the 0–100 score behind “Why this rating?”", () => {
    render(
      <WhyThisRating
        score={62}
        explanation="Synthetic explanation."
        components={{ swell_direction: 0.9, swell_period: 0.5, wave_size: 0.2, wind: 1, size_gate: 0.33, period_gate: 1, wind_gate: 1 }}
      />,
    );
    const toggle = screen.getByRole("button", { name: "Why this rating?" });
    expect(screen.getByText("Synthetic explanation.")).not.toBeVisible();
    fireEvent.click(toggle);
    expect(screen.getByText("Synthetic explanation.")).toBeVisible();
    expect(screen.getByText(/Quality score 62 \/ 100/)).toBeVisible();
    expect(screen.getByText("Held back by: wave size.")).toBeVisible();
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Swell directionFavorable",
      "Swell periodWorkable",
      "Wave sizeLimiting",
      "WindFavorable",
    ]);
  });

  it("lists the factors read from the response when the API sends no components", () => {
    const factors = ratingFactors({
      periodS: 15,
      windRelation: "offshore",
      windSpeedKmh: 8,
      explanation: "Synthetic: swell well aligned with the break; unfavourable tide.",
    });
    render(<WhyThisRating score={81} explanation="Synthetic explanation." factors={factors} />);
    fireEvent.click(screen.getByRole("button", { name: "Why this rating?" }));
    const items = within(screen.getByTestId("rating-factors")).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      "Swell periodlong-period groundswellFavorable",
      "Swell directionfavorable directionFavorable",
      "Windlight offshore windsFavorable",
      "Tideunfavorable tideLimiting",
    ]);
    // Technical term, explained on demand.
    expect(within(items[0]!).getByRole("button", { name: "Learn more: Groundswell" })).toBeInTheDocument();
  });

  it("prefers the API's component fits over derived factors", () => {
    render(<WhyThisRating score={50} components={{ wind: 0.5 }} factors={ratingFactors({ periodS: 15 })} />);
    expect(screen.queryByTestId("rating-factors")).toBeNull();
  });

  it("extracts component fits and gates", () => {
    expect(componentFits(null)).toEqual([]);
    expect(componentFits({ tide: 0.3, unrelated: 5 }).map((f) => [f.key, f.tone])).toEqual([["tide", "bad"]]);
    expect(limitingGates({ size_gate: 1, period_gate: 0.7, wind_gate: 0.5 })).toEqual(["swell period", "wind"]);
  });
});
