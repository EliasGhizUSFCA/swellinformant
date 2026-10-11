import { expect, test, type Locator, type Page } from "@playwright/test";

import type { SpotDetail } from "../types/api";

import {
  MATCH_ID,
  matchDetail,
  matchSummary,
  notifications,
  SCENARIOS,
  SPOT_SLUG,
  spotDetail,
  spotForecast,
  swellEvent,
  user,
  type Scenario,
} from "./confidence.mock";
import { apiGet } from "./helpers";

/**
 * Forecast confidence and spot research confidence, shown from mocked API responses.
 *
 * Every response here is SYNTHETIC (see confidence.mock.ts): the tests check that the labels
 * on screen are exactly the classes the (mocked) backend sent, that the two assessments stay
 * separate, that details open and close with mouse and touch, and that no confidence is ever
 * shown as a percentage. One test uses the real demo backend instead and checks the page
 * against whatever classes it returns (null renders as "Not assessed yet"). Set
 * UI_SCREENS_DIR to also save screenshots.
 */
const SCREENS = process.env.UI_SCREENS_DIR;
const DESKTOP = { width: 1440, height: 900 };
const MOBILE = { width: 390, height: 844 };
const LABEL: Record<string, string> = { high: "High", moderate: "Moderate", low: "Low" };
const label = (v: string | null) => (v && LABEL[v] ? LABEL[v] : "Not assessed yet");

async function shot(page: Page, name: string) {
  if (SCREENS) await page.screenshot({ path: `${SCREENS}/${name}.png`, fullPage: true });
}

async function blockTiles(page: Page) {
  await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (r) => r.fulfill({ status: 204 }));
}

async function mockSpot(page: Page, sc: Scenario) {
  await blockTiles(page);
  await page.route(new RegExp(`/api/spots/${SPOT_SLUG}$`), (r) => r.fulfill({ json: spotDetail(sc) }));
  await page.route(new RegExp(`/api/spots/${SPOT_SLUG}/forecast`), (r) => r.fulfill({ json: spotForecast(sc) }));
  await page.route(new RegExp(`/api/spots/${SPOT_SLUG}/events$`), (r) =>
    r.fulfill({ json: [swellEvent(sc, { id: "00000000-0000-4000-8000-0000000000e1", name: "Synthetic Point (test fixture)", startH: 150, score: 89 })] }),
  );
}

/** Signed-in pages: a synthetic session cookie gets past the route guard, the user is mocked. */
async function mockSignedIn(page: Page, sc: Scenario, baseURL: string | undefined) {
  await blockTiles(page);
  await page.context().addCookies([{ name: "sta_session", value: "synthetic-test-session", url: baseURL ?? "http://localhost:3000" }]);
  await page.route(/\/api\/auth\/me$/, (r) => r.fulfill({ json: user() }));
  await page.route(/\/api\/searches$/, (r) => r.fulfill({ json: [] }));
  await page.route(/\/api\/opportunities\?/, (r) => r.fulfill({ json: [matchSummary(sc)] }));
  await page.route(new RegExp(`/api/opportunities/${MATCH_ID}$`), (r) => r.fulfill({ json: matchDetail(sc) }));
  await page.route(/\/api\/notifications\?/, (r) => r.fulfill({ json: notifications(sc) }));
  await page.route(/\/api\/events\?/, (r) =>
    r.fulfill({
      json: [
        swellEvent(sc, { id: "00000000-0000-4000-8000-0000000000e1", name: "Synthetic Point (test fixture)", startH: 150, score: 89 }),
        swellEvent(sc, { id: "00000000-0000-4000-8000-0000000000e2", name: "Test Reef (synthetic)", startH: 60, score: 74 }),
        swellEvent(sc, { id: "00000000-0000-4000-8000-0000000000e3", name: "Fixture Beach (synthetic)", startH: 200, score: 61 }),
      ],
    }),
  );
}

/** No confidence element shows a percentage, and no line pairs "confidence" with one. */
async function expectNoConfidencePercentage(page: Page) {
  const texts = await page.locator('[data-testid$="confidence"], [data-testid$="confidence-badge"]').allTextContents();
  expect(texts.length).toBeGreaterThan(0);
  for (const t of texts) expect(t).not.toMatch(/\d\s*%/);
  const body = await page.locator("body").innerText();
  for (const line of body.split("\n")) expect(line).not.toMatch(/confiden[^\n]*\d+\s*%/i);
}

async function toggleOpensAndCloses(toggle: Locator, panelText: Locator, how: "click" | "tap") {
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(panelText).toBeHidden();
  await (how === "tap" ? toggle.tap() : toggle.click());
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(panelText).toBeVisible();
  await (how === "tap" ? toggle.tap() : toggle.click());
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(panelText).toBeHidden();
}

for (const [key, sc] of Object.entries(SCENARIOS) as Array<[keyof typeof SCENARIOS, Scenario]>) {
  test.describe(`spot detail · ${sc.name} (synthetic)`, () => {
    test.use({ viewport: DESKTOP });

    test("shows both assessments separately with the mocked classes", async ({ page }) => {
      await mockSpot(page, sc);
      await page.goto(`/spots/${SPOT_SLUG}`);
      const outlook = page.getByTestId("surf-outlook");
      await expect(outlook.getByTestId("rating-title")).toHaveText("Excellent Surf — 8.9/10");
      await expect(outlook.getByTestId("rating-reason")).toHaveText("Long-period groundswell, favorable direction, and light offshore winds.");

      const fc = outlook.getByTestId("forecast-confidence");
      await expect(fc.getByTestId("forecast-confidence-badge")).toHaveText(`Forecast confidence: ${label(sc.forecastClass)}`);
      await expect(fc).toHaveAttribute("data-confidence", sc.forecastClass ?? "none");
      if (sc.reasons[0]) await expect(fc.getByTestId("forecast-confidence-reason")).toHaveText(sc.reasons[0]);
      else await expect(fc.getByTestId("forecast-confidence-reason")).toHaveCount(0);

      const rc = outlook.getByTestId("research-confidence");
      await expect(rc.getByTestId("research-confidence-badge")).toHaveText(`Spot research confidence: ${label(sc.research?.research_class ?? null)}`);
      if (sc.research) await expect(rc.getByTestId("research-summary")).toHaveText(sc.research.summary);
      if (sc.research && sc.research.gaps.length) {
        // Low research confidence: the API's own gap sentences, visible without expanding.
        await expect(rc.getByTestId("research-gaps").getByRole("listitem")).toHaveText(sc.research.gaps);
      }

      const local = outlook.getByTestId("local-confidence");
      if (sc.localClass !== sc.forecastClass) {
        await expect(local.getByTestId("local-confidence-badge")).toHaveText(`Confidence at the break: ${label(sc.localClass)}`);
        await expect(local.getByTestId("local-confidence-reason")).toHaveText(sc.reasons[1]!);
      } else {
        await expect(local).toHaveCount(0);
      }

      // The hour-by-hour breakdown carries the same per-hour class.
      await expect(page.getByTestId("our-prediction").getByTestId("forecast-confidence-badge")).toHaveText(`Forecast confidence: ${label(sc.forecastClass)}`);
      await expectNoConfidencePercentage(page);
      await shot(page, `spot-${key}-desktop-collapsed`);
    });

    test("details open and close with the mouse", async ({ page }) => {
      await mockSpot(page, sc);
      await page.goto(`/spots/${SPOT_SLUG}`);
      const outlook = page.getByTestId("surf-outlook");
      const meaning = outlook.getByText("A rating of how much the forecast may still change", { exact: false });
      await toggleOpensAndCloses(outlook.getByRole("button", { name: "Confidence details" }), meaning, "click");

      await outlook.getByRole("button", { name: "Confidence details" }).click();
      if (sc.reasons.length) await expect(outlook.getByTestId("forecast-confidence-reasons").getByRole("listitem")).toHaveText(sc.reasons);
      else await expect(outlook.getByText("Forecast confidence has not been assessed for this forecast yet.")).toBeVisible();
      // The earlier 0–100 index only appears inside details, as an index.
      await expect(outlook.getByText("Confidence index 61 / 100 (not a probability).")).toBeVisible();

      if (sc.research) {
        const toggle = outlook.getByRole("button", { name: "Research details" });
        await toggle.click();
        await expect(outlook.getByTestId("research-variables").getByRole("listitem")).toHaveCount(sc.research.variables.length);
        const links = outlook.getByTestId("research-sources").getByRole("link");
        await expect(links).toHaveCount(sc.research.sources.length);
        for (let i = 0; i < sc.research.sources.length; i++) {
          await expect(links.nth(i)).toHaveAttribute("href", sc.research.sources[i]!.url!);
          await expect(links.nth(i)).toHaveAttribute("target", "_blank");
          await expect(links.nth(i)).toHaveAttribute("rel", "noopener noreferrer");
        }
        await expect(outlook.getByTestId("research-method")).toContainText(`Methodology ${sc.research.methodology_version}`);
      }
      // "Why this rating?" opens the factor details behind the headline.
      const why = outlook.getByRole("button", { name: "Why this rating?" });
      await toggleOpensAndCloses(why, outlook.getByTestId("rating-factors"), "click");
      await why.click();
      await expect(outlook.getByText("Quality score 89 / 100", { exact: false })).toBeVisible();
      await expectNoConfidencePercentage(page);
      await shot(page, `spot-${key}-desktop-expanded`);
    });
  });
}

test.describe("spot detail against the demo backend (no mocks)", () => {
  test.use({ viewport: DESKTOP });

  test("labels match the classes the API returns (null → Not assessed yet)", async ({ page }) => {
    await blockTiles(page);
    const api = (await apiGet(page.request, "/api/spots/pipeline")) as SpotDetail;
    await page.goto("/spots/pipeline");
    const outlook = page.getByTestId("surf-outlook");
    await expect(outlook.getByTestId("research-confidence-badge")).toHaveText(`Spot research confidence: ${label(api.research?.research_class ?? null)}`);
    if (api.best_upcoming) {
      const fc = (api.best_upcoming.forecast_confidence_class ?? null) as string | null;
      await expect(outlook.getByTestId("forecast-confidence-badge")).toHaveText(`Forecast confidence: ${label(fc)}`);
    }
    await expectNoConfidencePercentage(page);
  });
});

test.describe("spot detail on a phone (synthetic)", () => {
  test.use({ viewport: MOBILE, hasTouch: true, isMobile: true });

  test("details open and close with a tap, without horizontal scrolling", async ({ page }) => {
    const sc = SCENARIOS.lowResearchHighForecast;
    await mockSpot(page, sc);
    await page.goto(`/spots/${SPOT_SLUG}`);
    const outlook = page.getByTestId("surf-outlook");
    await expect(outlook.getByTestId("forecast-confidence-badge")).toHaveText("Forecast confidence: High");
    await expect(outlook.getByTestId("research-confidence-badge")).toHaveText("Spot research confidence: Low");
    await shot(page, "spot-mobile-collapsed");
    await toggleOpensAndCloses(
      outlook.getByRole("button", { name: "Confidence details" }),
      outlook.getByText("A rating of how much the forecast may still change", { exact: false }),
      "tap",
    );
    await toggleOpensAndCloses(outlook.getByRole("button", { name: "Research details" }), outlook.getByTestId("research-variables"), "tap");
    // "Learn more" popovers open on tap too.
    await outlook.getByRole("button", { name: "Learn more: Spot research confidence" }).tap();
    await expect(page.getByRole("dialog")).toContainText("How well this break's ideal conditions");
    await page.keyboard.press("Escape");
    await outlook.getByRole("button", { name: "Confidence details" }).tap();
    await outlook.getByRole("button", { name: "Research details" }).tap();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
    await expectNoConfidencePercentage(page);
    await shot(page, "spot-mobile-expanded");
  });
});

for (const [device, viewport] of [
  ["desktop", DESKTOP],
  ["mobile", MOBILE],
] as const) {
  test.describe(`signed-in pages · ${device} (synthetic)`, () => {
    test.use({ viewport, hasTouch: device === "mobile", isMobile: device === "mobile" });

    test("dashboard shows forecast confidence next to every predicted surf quality", async ({ page, baseURL }) => {
      const sc = SCENARIOS.highResearchLowForecast;
      await mockSignedIn(page, sc, baseURL);
      await page.goto("/dashboard");
      const cards = page.getByTestId("event-card");
      await expect(cards).toHaveCount(3);
      for (let i = 0; i < 3; i++) {
        await expect(cards.nth(i).getByTestId("rating-title")).toBeVisible();
        await expect(cards.nth(i).getByTestId("forecast-confidence-badge")).toHaveText("Forecast confidence: Low");
      }
      await expect(cards.first()).toContainText("Best upcoming surf");
      await expect(cards.first().getByTestId("rating-title")).toHaveText("Excellent Surf — 8.9/10");
      const opp = page.getByTestId("opportunity-card");
      await expect(opp.getByTestId("forecast-confidence-badge")).toHaveText("Forecast confidence: Low");
      await expectNoConfidencePercentage(page);
      await shot(page, `dashboard-${device}`);
    });

    test("opportunity detail shows the swell's forecast confidence, the index only in details", async ({ page, baseURL }) => {
      const sc = SCENARIOS.lowResearchHighForecast;
      await mockSignedIn(page, sc, baseURL);
      await page.goto(`/opportunities/${MATCH_ID}`);
      const overview = page.getByTestId("swell-overview");
      await expect(overview.getByTestId("forecast-confidence-badge")).toHaveText("Forecast confidence: High");
      await expect(overview.getByTestId("forecast-confidence-reason")).toHaveText(sc.reasons[0]!);
      const scoring = overview.getByRole("button", { name: "How this trip is scored" });
      await toggleOpensAndCloses(scoring, page.getByTestId("match-scoring").getByText("index 61 / 100 (not a probability)"), device === "mobile" ? "tap" : "click");
      await (device === "mobile" ? scoring.tap() : scoring.click());
      await expectNoConfidencePercentage(page);
      await shot(page, `opportunity-${device}`);
    });

    test("notification history shows the class the API sends", async ({ page, baseURL }) => {
      const sc = SCENARIOS.notAssessed;
      await mockSignedIn(page, sc, baseURL);
      await page.goto("/notifications");
      const table = page.getByTestId("notification-table");
      await expect(table.getByRole("row")).toHaveCount(2); // header + 1 alert
      await expect(table.getByTestId("forecast-confidence-badge")).toHaveText("Forecast confidence: Not assessed yet");
      await expectNoConfidencePercentage(page);
      await shot(page, `notifications-${device}`);
    });
  });
}
