import { expect, test } from "@playwright/test";

import { apiGet } from "./helpers";

interface ForecastPoint {
  valid_time: string;
  breaking_height_min_ft: number;
  breaking_p10_ft: number | null;
  breaking_p90_ft: number | null;
  uncertainty_kind: string | null;
}
interface Forecast {
  breaking_height_method: string | null;
  algorithm_version: string | null;
  points: ForecastPoint[];
  daily: Array<{ date: string; best_time: string | null }>;
  models: Array<{ source_code: string; role: string }>;
}

test.describe("spot page forecast", () => {
  test("shows our prediction with its uncertainty next to the raw model data", async ({ page, request }) => {
    const fc = (await apiGet(request, "/api/spots/jeffreys-bay/forecast?days=10")) as Forecast;
    expect(fc.breaking_height_method).toBe("linear_wave_v2");
    expect(fc.points[0]?.uncertainty_kind).toMatch(/heuristic/);
    expect(fc.models.some((m) => m.role === "authoritative")).toBe(true);

    await page.goto("/spots/jeffreys-bay");
    // The timeline's legend names the whisker band honestly: an unverified, heuristic range.
    await expect(page.getByTestId("quality-chart-legend")).toContainText("likely range (p10–p90 of an assumed error model, not yet verified)");
    const hour = page.getByTestId("forecast-hour");
    await expect(hour.getByTestId("raw-model")).toContainText("Significant wave height (offshore)");
    await expect(hour.getByTestId("our-prediction")).toContainText("typical–sets");
    // Final copy (confidence-text): an unverified range is a "Likely range … (unverified)" on
    // screen; "80% range" is only its intent, kept for screen readers with the basis.
    const range = hour.getByTestId("uncertainty-range");
    await expect(range).toContainText(/Likely range .+ \(unverified\)/);
    await expect(range).toContainText("meant as an 80% range");
    await expect(range).toContainText("not yet verified");
    // Directions are where wind and swell come FROM.
    await expect(hour.getByTestId("prediction-inputs")).toContainText(/km\/h from [NESW]{1,3} · /);
    await expect(hour.getByTestId("prediction-inputs")).toContainText(/swell from [NESW]{1,3}/);
    await expect(page.getByTestId("method-note")).toContainText("Linear wave theory");
    await expect(page.getByTestId("method-note")).not.toContainText("Komar–Gaughan approximation");

    // The best-week summary: forecast reasons only (no research or not-verified sentence), the
    // break's research kept apart, and the time named with its zone.
    const outlook = page.getByTestId("surf-outlook");
    await expect(outlook.getByTestId("forecast-confidence")).not.toContainText("How this break responds to swell");
    await expect(outlook.getByTestId("research-confidence")).toBeVisible();
    // Compact research block: its summary sits in "Research details", not on the first screen.
    await expect(outlook.getByTestId("research-summary")).toHaveCount(0);
    await expect(outlook.getByRole("button", { name: "Research details" })).toHaveAttribute("aria-expanded", "false");
    await expect(outlook.getByTestId("best-likely-range")).toContainText(/Likely range .+ \(unverified\)/);
    // Provenance next to the timeline: the model run with its cycle (demo data has no links).
    await expect(page.getByTestId("timeline-provenance").getByTestId("provenance-members")).toContainText(/Model runs?: /);

    // Choosing a day moves the breakdown to that day's best hour.
    const day = fc.daily[3]!;
    const before = await hour.getByTestId("forecast-breakdown").innerText();
    await page.getByTestId(`day-${day.date}`).click();
    await expect(page.getByTestId(`day-${day.date}`)).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => hour.getByTestId("forecast-breakdown").innerText()).not.toBe(before);

    // Step forward one forecast step (not "three hours": steps are 1, 3 or 6 hours long).
    const atDay = await hour.getByTestId("forecast-breakdown").innerText();
    await page.getByRole("button", { name: "Next forecast step" }).click();
    await expect.poll(() => hour.getByTestId("forecast-breakdown").innerText()).not.toBe(atDay);
  });

  test("model comparison lists every model with its role and switches variables", async ({ page }) => {
    await page.goto("/spots/pipeline");
    // Model comparison is secondary detail: collapsed until asked for.
    await page.getByRole("button", { name: /Compare forecast models/ }).click();
    const cmp = page.getByTestId("model-comparison");
    await expect(cmp).toBeVisible();
    await expect(cmp.getByTestId("model-roles")).toContainText("used for our prediction");
    await expect(cmp).toContainText("not surf heights");
    for (const tab of ["Peak period", "Wind speed", "Offshore wave height"]) {
      await cmp.getByRole("tab", { name: tab }).click();
      await expect(cmp.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
    }
  });
});
