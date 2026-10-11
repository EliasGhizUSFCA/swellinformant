import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConfidenceBadge, ForecastConfidence, LocalConfidence, ResearchConfidence } from "@/components/confidence";
import { ConfidenceBadge as LegacyConfidenceBadge } from "@/components/quality";
import { asConfidenceClass, confidenceLabel, FORECAST_CONFIDENCE_MEANING, leadTimeText, legacyIndexText, safeExternalUrl } from "@/lib/confidence";
import type { Research } from "@/types/api";

// SYNTHETIC test data: invented reasons, sources and URLs that exist only to exercise the
// components. Nothing here describes a real forecast or a real surf spot.

describe("confidence classes", () => {
  it("maps known classes and treats anything else as not assessed (never High)", () => {
    expect(asConfidenceClass("high")).toBe("high");
    expect(asConfidenceClass(" Moderate ")).toBe("moderate");
    expect(asConfidenceClass("LOW")).toBe("low");
    for (const v of [null, undefined, "", "certain", "very_high", "1"]) {
      expect(asConfidenceClass(v)).toBeNull();
      expect(confidenceLabel(v)).toBe("Not assessed yet");
    }
  });

  it("formats the legacy index only as an index, never a percentage", () => {
    expect(legacyIndexText(72)).toBe("index 72 / 100 (not a probability)");
    expect(legacyIndexText(71.6)).toBe("index 72 / 100 (not a probability)");
    expect(legacyIndexText(null)).toBeNull();
    expect(legacyIndexText(72)).not.toContain("%");
  });

  it("describes lead time in plain words", () => {
    expect(leadTimeText(168)).toBe("about 7 days ahead");
    expect(leadTimeText(18)).toBe("about 18 hours ahead");
    expect(leadTimeText(0.4)).toBe("under an hour ahead");
    expect(leadTimeText(-3)).toBeNull();
    expect(leadTimeText(null)).toBeNull();
  });

  it("only turns http(s) URLs into links", () => {
    expect(safeExternalUrl("https://example.org/a")).toBe("https://example.org/a");
    expect(safeExternalUrl("http://example.org/")).toBe("http://example.org/");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("data:text/html,hi")).toBeNull();
    expect(safeExternalUrl("/relative")).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
  });
});

describe("ConfidenceBadge", () => {
  it.each([
    ["high", "High"],
    ["moderate", "Moderate"],
    ["low", "Low"],
    [null, "Not assessed yet"],
    [undefined, "Not assessed yet"],
  ])("renders %s as text, not colour alone", (value, text) => {
    render(<ConfidenceBadge value={value} />);
    const badge = screen.getByTestId("forecast-confidence-badge");
    expect(badge).toHaveTextContent(`Forecast confidence: ${text}`);
    expect(badge).toHaveAttribute("data-confidence", value ?? "none");
  });

  it("labels research confidence separately", () => {
    render(<ConfidenceBadge value="low" kind="research" />);
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: Low");
  });

  it("compact mode keeps the prefix for screen readers and as a tooltip", () => {
    render(<ConfidenceBadge value="moderate" compact />);
    const badge = screen.getByTestId("forecast-confidence-badge");
    expect(badge).toHaveTextContent("Forecast confidence: Moderate");
    expect(badge).toHaveAttribute("title", "Forecast confidence: Moderate");
    expect(screen.getByText("Forecast confidence:", { exact: false })).toHaveClass("sr-only");
  });

  it("keeps the deprecated legacy badge labelled as an index, with no number on screen", () => {
    render(<LegacyConfidenceBadge label="high" value={72} />);
    const badge = screen.getByTestId("confidence-index-badge");
    expect(badge).toHaveTextContent("Confidence index: High");
    expect(badge.textContent).not.toMatch(/\d/);
    expect(badge.parentElement).toHaveAttribute("title", "Confidence index 72 / 100 (not a probability)");
  });
});

describe("ForecastConfidence", () => {
  const reasons = ["Synthetic reason one: the swell is about seven days away, so conditions may change.", "Synthetic reason two: the models agree."];

  it("shows the class and first reason, with every reason and the meaning behind a toggle", () => {
    render(<ForecastConfidence value="moderate" reasons={reasons} leadHours={170} legacyIndex={61} />);
    expect(screen.getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: Moderate");
    expect(screen.getByTestId("forecast-confidence-reason")).toHaveTextContent(reasons[0]!);
    const toggle = screen.getByRole("button", { name: "Confidence details" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const panel = document.getElementById(toggle.getAttribute("aria-controls")!)!;
    expect(panel).not.toBeVisible();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(panel).toBeVisible();
    const items = within(screen.getByTestId("forecast-confidence-reasons")).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(reasons);
    expect(panel).toHaveTextContent(FORECAST_CONFIDENCE_MEANING);
    expect(panel).toHaveTextContent("It is not a probability.");
    expect(panel).toHaveTextContent("about 7 days ahead");
    expect(panel).toHaveTextContent("index 61 / 100 (not a probability)");
    expect(document.body.textContent).not.toMatch(/\d\s*%/);
  });

  it.each(["high", "low"])("renders the %s class from the API value", (value) => {
    render(<ForecastConfidence value={value} reasons={[]} />);
    expect(screen.getByTestId("forecast-confidence")).toHaveAttribute("data-confidence", value);
  });

  it("renders a null class as Not assessed yet without a default", () => {
    render(<ForecastConfidence value={null} reasons={[]} />);
    expect(screen.getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: Not assessed yet");
    expect(screen.queryByTestId("forecast-confidence-reason")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confidence details" }));
    expect(screen.getByText("Forecast confidence has not been assessed for this forecast yet.")).toBeVisible();
    expect(document.body.textContent).not.toContain("High");
  });

  it("keeps long reasons whole", () => {
    const long = `Synthetic long reason: ${"the ensemble members spread widely and the latest run moved the peak by a day; ".repeat(6)}`.trim();
    render(<ForecastConfidence value="low" reasons={[long, "Second synthetic reason."]} />);
    expect(screen.getByTestId("forecast-confidence-reason").textContent).toBe(long);
    fireEvent.click(screen.getByRole("button", { name: "Confidence details" }));
    expect(within(screen.getByTestId("forecast-confidence-reasons")).getAllByRole("listitem")).toHaveLength(2);
  });
});

describe("LocalConfidence", () => {
  it("is hidden when it matches the forecast class", () => {
    const { container } = render(<LocalConfidence value="moderate" forecastValue="moderate" reasons={["x"]} />);
    expect(container).toBeEmptyDOMElement();
    const both = render(<LocalConfidence value={null} forecastValue={null} />);
    expect(both.container).toBeEmptyDOMElement();
  });

  it("shows the at-the-break class and the API's reason about the break when it differs", () => {
    render(
      <LocalConfidence
        value="low"
        forecastValue="high"
        reasons={["Synthetic: the models agree.", "Synthetic: this break's preferred tide is not documented in the research."]}
      />,
    );
    expect(screen.getByTestId("local-confidence-badge")).toHaveTextContent("Confidence at the break: Low");
    expect(screen.getByTestId("local-confidence-reason")).toHaveTextContent("Synthetic: this break's preferred tide is not documented in the research.");
  });

  it("does not invent a reason when none is about the break", () => {
    render(<LocalConfidence value="moderate" forecastValue="high" reasons={["Synthetic: models agree."]} />);
    expect(screen.queryByTestId("local-confidence-reason")).toBeNull();
  });
});

function research(overrides: Partial<Research> = {}): Research {
  return {
    research_class: "high",
    summary: "Synthetic summary: preferred swell direction, tide and wind are supported by several sources.",
    gaps: [],
    variables: [
      { variable: "swell_direction", label: "Swell direction", research_class: "high", summary: "Synthetic: three sources agree.", gaps: [], source_count: 3 },
      { variable: "tide", label: "Tide", research_class: "low", summary: "Synthetic: one source only.", gaps: ["Synthetic gap: only one source mentions tide."], source_count: 1 },
    ],
    sources: [
      { title: "Synthetic Guide (test fixture)", url: "https://example.org/synthetic-guide", publisher: "Example Press", source_type: "guidebook", published_year: 2020, accessed_at: "2026-09-01T00:00:00Z" },
      { title: "Unsafe synthetic link", url: "javascript:alert(1)", publisher: "Nobody", source_type: "community", published_year: null, accessed_at: null },
      { title: "Offline synthetic report", url: null, publisher: "Example Agency", source_type: "official", published_year: 2018, accessed_at: null },
    ],
    methodology_version: "research-v1-test",
    assessed_at: "2026-10-01T12:00:00Z",
    research_updated_at: "2026-09-30T12:00:00Z",
    ...overrides,
  };
}

describe("ResearchConfidence", () => {
  it("shows class, summary, details, safe source links, method and update date", () => {
    render(<ResearchConfidence research={research()} />);
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: High");
    expect(screen.getByTestId("research-summary")).toHaveTextContent("Synthetic summary");
    expect(screen.getByTestId("research-updated")).toHaveTextContent("Research updated Sep 30, 2026");
    const toggle = screen.getByRole("button", { name: "Research details" });
    fireEvent.click(toggle);
    const vars = within(screen.getByTestId("research-variables")).getAllByRole("listitem");
    expect(vars).toHaveLength(2);
    expect(vars[1]).toHaveTextContent("Gap: Synthetic gap: only one source mentions tide.");
    expect(vars[1]).toHaveTextContent("1 source");
    const links = within(screen.getByTestId("research-sources")).getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "https://example.org/synthetic-guide");
    expect(links[0]).toHaveAttribute("target", "_blank");
    expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText("Unsafe synthetic link").closest("a")).toBeNull();
    expect(screen.getByText("Offline synthetic report").closest("a")).toBeNull();
    expect(screen.getByTestId("research-method")).toHaveTextContent("Methodology research-v1-test");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the API's gap sentences when research confidence is low", () => {
    const gaps = ["Synthetic gap: no source describes the preferred tide.", "Synthetic gap: sources disagree on the best swell direction."];
    render(<ResearchConfidence research={research({ research_class: "low", gaps })} />);
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: Low");
    const items = within(screen.getByTestId("research-gaps")).getAllByRole("listitem");
    expect(items.map((i) => i.textContent)).toEqual(gaps);
    expect(items[0]).toBeVisible();
  });

  it("renders a missing assessment as Not assessed yet", () => {
    render(<ResearchConfidence research={null} />);
    expect(screen.getByTestId("research-confidence")).toHaveAttribute("data-confidence", "none");
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: Not assessed yet");
  });

  it("keeps research and forecast confidence as separate labels", () => {
    render(
      <>
        <ForecastConfidence value="low" reasons={[]} />
        <ResearchConfidence research={research({ research_class: "high" })} />
      </>,
    );
    expect(screen.getByTestId("forecast-confidence-badge")).toHaveTextContent("Forecast confidence: Low");
    expect(screen.getByTestId("research-confidence-badge")).toHaveTextContent("Spot research confidence: High");
  });
});
