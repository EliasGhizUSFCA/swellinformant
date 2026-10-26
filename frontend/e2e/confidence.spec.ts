import { expect, test, type Locator, type Page } from "@playwright/test";

import type { MatchDetail, MatchSummary, NotificationPage, SpotDetail, SpotForecast } from "../types/api";

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
import { apiGet, apiPost, verifyEmailFromOutbox } from "./helpers";

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
const label = (v: string | null | undefined) => (v && LABEL[v] ? LABEL[v] : "Not assessed yet");
/** Sentences the backend writes that are never forecast-confidence reasons (word for word:
 * forecast_confidence.RESEARCH_REASONS, WIND_DISPUTE_REASON and NOT_VERIFIED_REASON). */
const NOT_FORECAST_REASONS = [
  "How this break responds to swell is not well documented yet.",
  "How this break responds to swell is only partly documented.",
  "How this break responds to swell is well documented.",
  "Sources disagree about which wind is offshore here.",
  "Not yet checked against buoys near this spot.",
];

/** The forecast-confidence block never lists research, local or wind-dispute text. */
async function expectForecastReasonsOnly(fc: Locator, forbidden: Array<string | null | undefined>) {
  const list = fc.getByTestId("forecast-confidence-reasons");
  const reason = fc.getByTestId("forecast-confidence-reason");
  for (const text of [...NOT_FORECAST_REASONS, ...forbidden]) {
    if (!text) continue;
    if (await list.count()) await expect(list).not.toContainText(text);
    if (await reason.count()) await expect(reason).not.toContainText(text);
  }
}

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

async function expectNoHorizontalScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
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
      // The spot page uses the compact block (integration): the summary is in "Research details".
      if (sc.research) {
        await expect(rc.getByTestId("research-summary")).toHaveCount(0);
        await expect(rc.getByTestId("research-details")).toContainText(sc.research.summary);
      }
      if (sc.research && sc.research.gaps.length) {
        // Low research confidence: the API's first gap and how many more there are stay
        // visible without expanding; every gap is listed in the details.
        const [first, ...more] = sc.research.gaps;
        await expect(rc.getByTestId("research-first-gap")).toBeVisible();
        await expect(rc.getByTestId("research-first-gap")).toContainText(first!);
        if (more.length) await expect(rc.getByTestId("research-more-gaps")).toHaveText(`+${more.length} more`);
        await expect(rc.getByTestId("research-gaps")).toBeHidden();
        await expect(rc.getByTestId("research-gaps").getByRole("listitem", { includeHidden: true })).toHaveText(sc.research.gaps);
      }

      const local = outlook.getByTestId("local-confidence");
      if (sc.localClass !== sc.forecastClass) {
        await expect(local.getByTestId("local-confidence-badge")).toHaveText(`Confidence at the break: ${label(sc.localClass)}`);
        await expect(local.getByTestId("local-confidence-reason")).toHaveText(sc.localReason!);
      } else {
        await expect(local).toHaveCount(0);
      }
      // The reason about the break belongs to confidence at the break, never to forecast confidence.
      await expectForecastReasonsOnly(fc, [sc.localReason]);

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
      // Forecast-only reasons (the API's forecast_confidence_reasons), never the reason about
      // the break that the deprecated mixed list also carries.
      if (sc.reasons.length) await expect(outlook.getByTestId("forecast-confidence-reasons").getByRole("listitem")).toHaveText(sc.reasons);
      else await expect(outlook.getByText("Forecast confidence has not been assessed for this forecast yet.")).toBeVisible();
      await expectForecastReasonsOnly(outlook.getByTestId("forecast-confidence"), [sc.localReason]);
      // The earlier 0–100 index only appears inside details, named for what it is.
      await expect(outlook.getByText("Offshore spread index 61 / 100 (not a probability).")).toBeVisible();
      await expect(outlook.getByText(/Confidence index \d+/)).toHaveCount(0);

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
      // "Why this rating?" opens the factor details behind the headline: the API's own
      // component fits for that hour (from the forecast), mocked here as all favourable.
      const why = outlook.getByRole("button", { name: "Why this rating?" });
      await toggleOpensAndCloses(why, outlook.getByTestId("rating-fits"), "click");
      await why.click();
      await expect(outlook.getByTestId("rating-fits").getByRole("listitem")).toHaveText([
        "Swell directionFavorable",
        "Swell periodFavorable",
        "Wave sizeFavorable",
        "WindFavorable",
        "ConsistencyFavorable",
      ]);
      await expect(outlook.getByText("Quality score 89 / 100", { exact: false })).toBeVisible();
      await expectNoConfidencePercentage(page);
      await shot(page, `spot-${key}-desktop-expanded`);
    });
  });
}

const QUALITY: Record<string, string> = {
  exceptional: "Exceptional",
  excellent: "Excellent",
  very_good: "Very Good",
  good: "Good",
  fair: "Fair",
  poor: "Poor",
};
const FIT_ORDER: Array<[string, string]> = [
  ["swell_direction", "Swell direction"],
  ["swell_period", "Swell period"],
  ["wave_size", "Wave size"],
  ["wind", "Wind"],
  ["tide", "Tide"],
  ["consistency", "Consistency"],
];
const fitWord = (v: number) => (v >= 0.75 ? "Favorable" : v >= 0.4 ? "Workable" : "Limiting");

test.describe("spot detail against the demo backend (no mocks)", () => {
  test.use({ viewport: DESKTOP });

  test("labels match the classes the API returns (null → Not assessed yet)", async ({ page }) => {
    await blockTiles(page);
    const api = (await apiGet(page.request, "/api/spots/pipeline")) as SpotDetail;
    const forecast = (await apiGet(page.request, "/api/spots/pipeline/forecast?days=10")) as SpotForecast;
    await page.goto("/spots/pipeline");
    const outlook = page.getByTestId("surf-outlook");
    await expect(outlook.getByTestId("research-confidence-badge")).toHaveText(`Spot research confidence: ${label(api.research?.research_class ?? null)}`);
    const best = api.best_upcoming;
    if (best) {
      const fc = (best.forecast_confidence_class ?? null) as string | null;
      await expect(outlook.getByTestId("forecast-confidence-badge")).toHaveText(`Forecast confidence: ${label(fc)}`);
      // The reasons under forecast confidence are the API's forecast-only list, word for word,
      // and never the research sentence the deprecated mixed list carries.
      expect(best.confidence_reasons.some((r) => NOT_FORECAST_REASONS.includes(r))).toBe(true);
      expect(best.forecast_confidence_reasons.filter((r) => NOT_FORECAST_REASONS.includes(r))).toEqual([]);
      const block = outlook.getByTestId("forecast-confidence").first();
      await block.getByRole("button", { name: "Confidence details" }).click();
      if (best.forecast_confidence_reasons.length) await expect(block.getByTestId("forecast-confidence-reasons").getByRole("listitem")).toHaveText(best.forecast_confidence_reasons);
      await expectForecastReasonsOnly(block, [best.local_confidence_reason]);
      await block.getByRole("button", { name: "Confidence details" }).click();
      await expect(outlook.getByTestId("rating-title")).toHaveText(`${QUALITY[best.label]} Surf — ${(best.score / 10).toFixed(1)}/10`);
      // "Why this rating?" shows the API's own component fits for that hour, word for word.
      const point = forecast.points.find((p) => Date.parse(p.valid_time) === Date.parse(best.valid_time));
      if (point) {
        await outlook.getByRole("button", { name: "Why this rating?" }).click();
        const expected = FIT_ORDER.filter(([k]) => typeof point.components[k] === "number").map(
          ([k, name]) => `${name}${fitWord(point.components[k] as number)}`,
        );
        await expect(outlook.getByTestId("rating-fits").getByRole("listitem")).toHaveText(expected);
        await expect(outlook.getByText(`Quality score ${best.score} / 100`, { exact: false })).toBeVisible();
      }
    }
    await expectNoConfidencePercentage(page);
    await shot(page, "spot-real-desktop-why-open");
  });
});

test.describe("phone layout against the demo backend (no mocks)", () => {
  test.use({ viewport: MOBILE, hasTouch: true, isMobile: true });

  test("the spot list and a spot page fit the screen", { tag: "@mobile" }, async ({ page }) => {
    await blockTiles(page);
    await page.goto("/spots");
    await expect(page.getByTestId("spot-card")).toHaveCount(50);
    await expectNoHorizontalScroll(page);
    await shot(page, "spots-real-mobile");
    await page.goto("/spots/pipeline");
    await expect(page.getByTestId("surf-outlook")).toBeVisible();
    await expectNoHorizontalScroll(page);
    await shot(page, "spot-real-mobile");
  });
});

test.describe("keyboard and pointer access (synthetic)", () => {
  test.use({ viewport: DESKTOP });

  test("details and Learn more popovers work from the keyboard and with the mouse", async ({ page }) => {
    await mockSpot(page, SCENARIOS.lowResearchHighForecast);
    await page.goto(`/spots/${SPOT_SLUG}`);
    const outlook = page.getByTestId("surf-outlook");

    // Disclosure: a focusable button; Enter opens, Space closes.
    const toggle = outlook.getByRole("button", { name: "Confidence details" });
    await toggle.focus();
    await expect(toggle).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(outlook.getByText("A rating of how much the forecast may still change", { exact: false })).toBeVisible();
    await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    // Tab moves on from the toggle to the next control (it is in the tab order).
    await toggle.focus();
    await page.keyboard.press("Tab");
    await expect(toggle).not.toBeFocused();

    // Popover from the keyboard: Enter opens it, Escape closes it and focus returns.
    const info = outlook.getByRole("button", { name: "Learn more: Spot research confidence" });
    await info.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toContainText("How well this break's ideal conditions");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(info).toBeFocused();

    // Popover with the mouse: click opens, a click elsewhere closes.
    // The range's final copy (confidence-text): "Likely range (unverified)", term likelyRange.
    await outlook.getByRole("button", { name: "Learn more: Likely range (unverified)" }).click();
    await expect(page.getByRole("dialog")).toContainText("not been checked against surf observations");
    await outlook.getByText("Best surf, next 7 days").click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });
});

test.describe("spot detail on a phone (synthetic)", () => {
  test.use({ viewport: MOBILE, hasTouch: true, isMobile: true });

  test("details open and close with a tap, without horizontal scrolling", { tag: "@mobile" }, async ({ page }) => {
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
    // "Learn more" popovers open on tap too, and a tap elsewhere closes them.
    await outlook.getByRole("button", { name: "Learn more: Spot research confidence" }).tap();
    await expect(page.getByRole("dialog")).toContainText("How well this break's ideal conditions");
    await page.getByRole("heading", { level: 1 }).tap();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await outlook.getByRole("button", { name: "Learn more: Breaking wave height (our estimate)" }).tap();
    await expect(page.getByRole("dialog")).toContainText("significant breaking height");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await toggleOpensAndCloses(outlook.getByRole("button", { name: "Why this rating?" }), outlook.getByTestId("rating-fits"), "tap");
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

    test("dashboard shows forecast confidence next to every predicted surf quality", device === "mobile" ? { tag: "@mobile" } : {}, async ({ page, baseURL }) => {
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
      // Forecast and local classes are equal here: one badge per card, and nothing is stale.
      await expect(page.getByTestId("local-confidence")).toHaveCount(0);
      await expect(page.getByTestId("stale-badge")).toHaveCount(0);
      await expect(page.getByTestId("stale-banner")).toHaveCount(0);
      const opp = page.getByTestId("opportunity-card");
      await expect(opp.getByTestId("forecast-confidence-badge")).toHaveText("Forecast confidence: Low");
      await expectNoConfidencePercentage(page);
      await expectNoHorizontalScroll(page);
      await shot(page, `dashboard-${device}`);
    });

    test("opportunity detail shows the swell's forecast confidence, the index only in details", device === "mobile" ? { tag: "@mobile" } : {}, async ({ page, baseURL }) => {
      const sc = SCENARIOS.lowResearchHighForecast;
      await mockSignedIn(page, sc, baseURL);
      await page.goto(`/opportunities/${MATCH_ID}`);
      const overview = page.getByTestId("swell-overview");
      await expect(overview.getByTestId("forecast-confidence-badge")).toHaveText("Forecast confidence: High");
      await expect(overview.getByTestId("forecast-confidence-reason")).toHaveText(sc.reasons[0]!);
      // Lower at the break (Moderate): its own badge and the API's reason, apart from forecast confidence.
      await expect(overview.getByTestId("local-confidence-badge")).toHaveText("Confidence at the break: Moderate");
      await expect(overview.getByTestId("local-confidence-reason")).toHaveText(sc.localReason!);
      await expectForecastReasonsOnly(overview.getByTestId("forecast-confidence"), [sc.localReason]);
      const scoring = overview.getByRole("button", { name: "How this trip is scored" });
      // The match's "confidence" component is the legacy index, named for what it is.
      const index = page.getByTestId("match-scoring").getByTestId("match-index-value");
      await toggleOpensAndCloses(scoring, index, device === "mobile" ? "tap" : "click");
      await (device === "mobile" ? scoring.tap() : scoring.click());
      await expect(index).toHaveText("61 / 100 (not a probability)");
      // Named for what it is; "(not a probability)" is said once, in the value.
      await expect(page.getByTestId("match-scoring").getByTestId("match-index-label")).toHaveText("Offshore spread index");
      await expect(page.getByTestId("match-scoring")).not.toContainText("Forecast confidence");
      await expectNoConfidencePercentage(page);
      await expectNoHorizontalScroll(page);
      await shot(page, `opportunity-${device}`);
    });

    test("notification history shows the class the API sends", device === "mobile" ? { tag: "@mobile" } : {}, async ({ page, baseURL }) => {
      const sc = SCENARIOS.notAssessed;
      await mockSignedIn(page, sc, baseURL);
      await page.goto("/notifications");
      const table = page.getByTestId("notification-table");
      await expect(table.getByRole("row")).toHaveCount(2); // header + 1 alert
      await expect(table.getByTestId("forecast-confidence-badge")).toHaveText("Forecast confidence: Not assessed yet");
      await expectNoConfidencePercentage(page);
      await expectNoHorizontalScroll(page);
      await shot(page, `notifications-${device}`);
    });
  });
}

test.describe("opportunity, alert and match badges against the demo backend (no mocks)", () => {
  test.use({ viewport: DESKTOP });

  test("the cards show the forecast and local confidence the real API sends", async ({ page }) => {
    // Needs a simulated swell (about 30 s through the whole pipeline). A spot no other spec
    // simulates, so this swell never merges with another test's event.
    test.setTimeout(240_000);
    const SPOT = "raglan-manu-bay";
    await blockTiles(page);
    const email = `confidence-real-${Date.now()}@example.com`;
    await page.goto("/register");
    await page.fill("#full_name", "Confidence Tester");
    await page.fill("#email", email);
    await page.fill("#password", "Confidence-tester-9!");
    await page.fill("#confirm_password", "Confidence-tester-9!");
    await page.getByRole("button", { name: "Create account" }).click();
    await page.waitForURL("**/dashboard**");
    await verifyEmailFromOutbox(page, email);
    const search = (await apiPost(page, "/api/searches", {
      name: "Confidence badges (e2e)",
      date_mode: "flexible",
      horizon_days: 30,
      units: "ft",
      wave_min: 3,
      wave_max: 20,
      min_quality: "fair",
      destination_mode: "spots",
      destinations: { spot_slugs: [SPOT] },
      origins: ["SFO"],
      notification_channel: "email",
      priority: "balanced",
      min_window_hours: 3,
      travel: { max_price: 5000, currency: "USD", max_layovers: 2, max_flight_hours: 45, min_days_at_destination: 3, max_trip_days: 14, travelers: 1, arrival_buffer_days: 2, departure_buffer_days: 1 },
    })) as { id: string };
    await apiPost(page, "/api/dev/simulate-swell", { spot_slug: SPOT, days_ahead: 6, duration_hours: 48, target_breaking_ft: 8, period_s: 16, deliver: true });

    const opps = ((await apiGet(page.request, "/api/opportunities?limit=50")) as MatchSummary[]).filter((m) => m.search_id === search.id);
    expect(opps.length, "the simulated swell produced an opportunity").toBeGreaterThan(0);
    const top = opps[0]!;
    const detail = (await apiGet(page.request, `/api/opportunities/${top.id}`)) as MatchDetail;
    // The API's own contract: the opportunity reads exactly like its swell event.
    expect(top.forecast_confidence_class ?? null).toBe(detail.event.forecast_confidence_class ?? null);
    expect(top.local_confidence_class ?? null).toBe(detail.event.local_confidence_class ?? null);

    await page.goto("/dashboard");
    const card = page.locator(`[data-testid="opportunity-card"][href="/opportunities/${top.id}"]`);
    await expect(card.getByTestId("forecast-confidence-badge")).toHaveText(`Forecast confidence: ${label(top.forecast_confidence_class)}`);
    const lower = top.local_confidence_class && top.local_confidence_class !== top.forecast_confidence_class;
    if (lower) await expect(card.getByTestId("local-confidence-badge")).toHaveText(`At the break: ${label(top.local_confidence_class)}`);
    else await expect(card.getByTestId("local-confidence")).toHaveCount(0);
    await expect(card.getByTestId("stale-badge")).toHaveCount(top.stale ? 1 : 0);

    await page.goto(`/opportunities/${top.id}`);
    const overview = page.getByTestId("swell-overview");
    await expect(overview.getByTestId("forecast-confidence-badge")).toHaveText(`Forecast confidence: ${label(detail.event.forecast_confidence_class)}`);
    await expectForecastReasonsOnly(overview.getByTestId("forecast-confidence"), [detail.event.local_confidence_reason]);

    // The search's matches use the same card.
    await page.goto(`/searches/${search.id}`);
    await expect(card.getByTestId("forecast-confidence-badge")).toHaveText(`Forecast confidence: ${label(top.forecast_confidence_class)}`);

    const alerts = (await apiGet(page.request, "/api/notifications?limit=25")) as NotificationPage;
    const alert = alerts.items.find((n) => n.match_id === top.id);
    expect(alert, "the simulated swell produced an alert").toBeTruthy();
    expect(alert!.forecast_confidence_class ?? null).toBe(top.forecast_confidence_class ?? null);
    await page.goto("/notifications");
    await expect(page.getByTestId("notification-table").getByTestId("forecast-confidence-badge").first()).toHaveText(`Forecast confidence: ${label(alert!.forecast_confidence_class)}`);
    await expectNoConfidencePercentage(page);
  });
});
