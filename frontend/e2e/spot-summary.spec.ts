import { expect, test, type Page, type Route } from "@playwright/test";

import { apiGet } from "./helpers";

/**
 * The spot page's always-visible summary and "Why this rating?" (spec 25), the ft / m toggle
 * for visitors who are not signed in (spec 24), the stale label (spec 14), the provider credit
 * (design D3) and the chart annotations (best windows, tide suitability) against the demo
 * backend. Tests named "(synthetic)" patch the demo's own responses with route mocks to reach
 * states the demo cannot produce (a stale spot, Windy data, a rated tide); the patched values
 * are SYNTHETIC and labelled so.
 */

const SPOT = "jeffreys-bay";
const HOUR = 3_600_000;

async function blockTiles(page: Page) {
  await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (r) => r.fulfill({ status: 204 }));
}

async function openSpot(page: Page, slug = SPOT) {
  await blockTiles(page);
  await page.goto(`/spots/${slug}`);
  await expect(page.getByTestId("surf-outlook")).toBeVisible();
}

/** Answers `pattern` with the demo's own response, changed by `patch` (SYNTHETIC change). */
async function patchJson(page: Page, pattern: RegExp, patch: (body: Record<string, unknown>) => void) {
  await page.route(pattern, async (route: Route) => {
    const res = await route.fetch();
    const body = (await res.json()) as Record<string, unknown>;
    patch(body);
    await route.fulfill({ response: res, json: body });
  });
}

interface Best {
  label: string;
  score: number;
  valid_time: string;
}

test.describe("spot summary (spec 25)", () => {
  test("always shows the rating as one sentence, the waves, wind, swell, best time and forecast confidence", async ({ page }) => {
    const spot = (await apiGet(page.request, `/api/spots/${SPOT}`)) as { best_upcoming: Best | null; research: unknown };
    test.skip(!spot.best_upcoming, "the demo has no usable forecast for this spot");
    await openSpot(page);
    const outlook = page.getByTestId("surf-outlook");
    const b = spot.best_upcoming!;
    const title = `${{ poor: "Poor", fair: "Fair", good: "Good", very_good: "Very Good", excellent: "Excellent", exceptional: "Exceptional" }[b.label]} Surf — ${(Math.round(b.score) / 10).toFixed(1)}/10`;
    await expect(outlook.getByTestId("rating-title")).toHaveText(title);
    // One sentence: "<class> Surf — x/10 — <reason>."
    const reason = (await outlook.getByTestId("rating-reason").textContent())!;
    expect(reason).toMatch(/^[A-Z].+\.$/);
    await expect(outlook.getByTestId("rating-headline")).toHaveText(`${title} — ${reason}`);
    await expect(outlook.getByTestId("best-height")).toHaveText(/\d.* ft$/);
    await expect(outlook.getByTestId("best-swell")).toContainText(/@ \d+ s from [NESW]{1,3}/);
    await expect(outlook.getByTestId("best-wind")).toContainText(/km\/h from [NESW]{1,3}/);
    await expect(outlook.getByTestId("best-time")).toBeVisible();
    await expect(outlook.getByTestId("forecast-confidence-badge")).toBeVisible();
    // Research confidence is shown apart; no combined class (spec 20).
    await expect(outlook.getByTestId("research-confidence-badge")).toBeVisible();
    await expect(page.getByTestId("local-confidence")).toHaveCount(0);
  });

  test("'Why this rating?' opens the breakdown, every variable, tide, models and research", async ({ page }) => {
    await openSpot(page);
    const outlook = page.getByTestId("surf-outlook");
    const details = outlook.getByTestId("rating-details");
    await expect(details).toBeHidden();
    await outlook.getByRole("button", { name: "Why this rating?" }).click();
    await expect(outlook.getByTestId("rating-fits")).toBeVisible();
    await expect(details.getByTestId("rating-variables")).toContainText("Breaking faces (our estimate)");
    await expect(details.getByTestId("rating-variables")).toContainText("Primary swell");
    await expect(details.getByTestId("rating-tide")).toContainText("Preferred tide here:");
    await expect(details.getByTestId("rating-models")).toContainText("Wind:");
    await expect(details.getByTestId("rating-models")).toContainText("Waves:");
    await expect(details.getByTestId("rating-research").getByTestId("rating-forecast-confidence")).toBeVisible();
    await expect(details.getByTestId("rating-research").getByTestId("rating-research-confidence")).toBeVisible();
  });

  test("ft / m works signed out, is remembered, and the map follows it", async ({ page }) => {
    await openSpot(page);
    const toggle = page.getByTestId("height-unit-toggle");
    await expect(toggle.getByRole("button", { name: "ft" })).toHaveAttribute("aria-pressed", "true");
    await toggle.getByRole("button", { name: "m" }).click();
    await expect(page.getByTestId("best-height")).toHaveText(/\d m$/);
    await expect(page.getByTestId("summary-breaking")).toContainText(" m");
    await page.reload();
    await expect(page.getByTestId("height-unit-toggle").getByRole("button", { name: "m" })).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("best-height")).toHaveText(/\d m$/);
    // The map uses the same choice.
    await page.goto("/map");
    await expect(page.locator("path.spot-marker")).toHaveCount(50);
    await page.locator(`path.spot-${SPOT}`).focus();
    await expect(page.locator(".leaflet-tooltip")).toContainText(/ m surf \(our prediction\)|No usable forecast/);
  });

  test("ft / m on a phone", { tag: "@mobile" }, async ({ page, hasTouch }) => {
    await openSpot(page);
    const m = page.getByTestId("height-unit-toggle").getByRole("button", { name: "m" });
    await (hasTouch ? m.tap() : m.click());
    await expect(page.getByTestId("best-height")).toHaveText(/\d m$/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  });
});

test.describe("staleness and provider credit (synthetic)", () => {
  test("a stale spot says when it was last updated and is never shown as current (synthetic freshness)", async ({ page }) => {
    const updated = new Date(Date.now() - 7 * HOUR - 10 * 60_000).toISOString();
    const freshness = { version_id: 7, last_updated_at: updated, stale: true, reason: "refresh_failed" };
    for (const path of [`/api/spots/${SPOT}/forecast`, `/api/spots/${SPOT}/timeline`]) {
      await patchJson(page, new RegExp(`${path.replace(/\//g, "\\/")}(\\?|$)`), (b) => {
        b.freshness = freshness;
      });
    }
    await openSpot(page);
    const banner = page.getByTestId("stale-banner");
    await expect(banner.getByTestId("stale-last-updated")).toHaveText("Forecast last updated 7 hours ago.");
    await expect(banner).toContainText("The latest refresh failed for this spot");
    await expect(page.getByTestId("outlook-stale")).toContainText("Forecast last updated 7 hours ago.");
    await expect(page.getByTestId("outlook-stale")).toContainText("Not a current forecast.");
    await expect(page.getByTestId("timeline-stale")).toHaveText("Forecast last updated 7 hours ago");
  });

  test("a fresh spot shows no stale label", async ({ page }) => {
    await openSpot(page);
    await expect(page.getByTestId("outlook-stale")).toHaveCount(0);
    await expect(page.getByTestId("timeline-stale")).toHaveCount(0);
  });

  test("Windy data carries the Windy credit and estimated run times (synthetic source)", async ({ page }) => {
    await patchJson(page, new RegExp(`/api/spots/${SPOT}/forecast(\\?|$)`), (b) => {
      const source = b.source as Record<string, unknown>;
      source.code = "windy_gfswave";
      source.name = "Windy (synthetic e2e source)";
      source.attribution_links = [{ label: "Windy.com", href: "https://www.windy.com/" }];
    });
    await openSpot(page);
    await page.getByTestId("surf-outlook").getByRole("button", { name: "Why this rating?" }).click();
    const models = page.getByTestId("rating-models");
    await expect(models).toContainText("(estimated)");
    await expect(models.getByRole("link", { name: "Contains data from the Windy database" })).toHaveAttribute("href", "https://www.windy.com/");
    await page.getByRole("button", { name: /About this forecast/ }).click();
    await expect(page.getByTestId("forecast-source").getByRole("link", { name: "Contains data from the Windy database" })).toBeVisible();
  });
});

test.describe("chart annotations (synthetic)", () => {
  test("best windows bracket the daylight steps rated Good or better; the tide row shades the rated tide fit", async ({ page }) => {
    let windows = 0;
    await patchJson(page, new RegExp(`/api/spots/${SPOT}/timeline(\\?|$)`), (b) => {
      const steps = b.steps as Array<{ is_daylight: boolean; quality: { label: string; score: number }; why: { tide_included: boolean; fits: Record<string, number> } }>;
      // SYNTHETIC: rate the first two daylight runs Good, everything else Fair; the tide counts,
      // with fits cycling favourable / workable / unfavourable.
      let runs = 0;
      let inRun = false;
      steps.forEach((s, i) => {
        s.quality = { ...s.quality, label: "fair", score: 45 };
        if (s.is_daylight && runs < 2) {
          s.quality = { ...s.quality, label: "good", score: 60 };
          inRun = true;
        } else if (inRun) {
          inRun = false;
          runs += 1;
        }
        s.why = { ...s.why, tide_included: true, fits: { ...s.why.fits, tide: [0.9, 0.5, 0.1][i % 3]! } };
      });
      windows = Math.min(2, runs + (inRun ? 1 : 0));
    });
    await openSpot(page);
    await expect(page.getByTestId("timeline-scroller")).toBeVisible();
    await expect(page.locator('[data-testid="quality-strip"] [data-testid="best-window"]')).toHaveCount(windows);
    await expect(page.getByTestId("best-window-key")).toBeVisible();
    for (const fit of ["favourable", "workable", "unfavourable"]) expect(await page.locator(`[data-testid="tide-row"] [data-tide-fit="${fit}"]`).count()).toBeGreaterThan(0);
    await expect(page.getByTestId("tide-fit-key")).toContainText("Favorable tide");
  });
});
