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
    await expect(page.getByTestId("quality-chart-legend")).toContainText("80% forecast range");
    const hour = page.getByTestId("forecast-hour");
    await expect(hour.getByTestId("raw-model")).toContainText("Significant wave height (offshore)");
    await expect(hour.getByTestId("our-prediction")).toContainText("typical–sets");
    await expect(hour.getByTestId("uncertainty-range")).toContainText("80% range");
    await expect(hour.getByTestId("uncertainty-range")).toContainText("not yet verified");
    await expect(page.getByTestId("method-note")).toContainText("Linear wave theory");
    await expect(page.getByTestId("method-note")).not.toContainText("Komar–Gaughan approximation");

    // Choosing a day moves the breakdown to that day's best hour.
    const day = fc.daily[3]!;
    const before = await hour.getByTestId("forecast-breakdown").innerText();
    await page.getByTestId(`day-${day.date}`).click();
    await expect(page.getByTestId(`day-${day.date}`)).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => hour.getByTestId("forecast-breakdown").innerText()).not.toBe(before);

    // Step forward three hours with the arrow control.
    const atDay = await hour.getByTestId("forecast-breakdown").innerText();
    await hour.getByRole("button", { name: "Three hours later" }).click();
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
