import { expect, test, type Page } from "@playwright/test";

import { stubTiles } from "./map-helpers";

/**
 * The "How to use" tab against the demo backend: reached from the map's nav on a desktop and
 * from the menu on a phone; the guide renders its headings, and its table of contents scrolls
 * to each section below the sticky header.
 */

const SECTIONS = [
  "What the app does",
  "Read the surf map",
  "Read a spot page",
  "Ratings and confidence",
  "How forecasts stay current",
  "Create an account",
  "Save a search",
  "How matching works",
  "Trips and flights",
  "Alerts and emails",
  "Demo mode and test sites",
  "Status page",
  "Tips and questions",
];

const SCREENS = process.env.HOWTO_SCREENS_DIR;

async function shot(page: Page, name: string) {
  if (!SCREENS) return;
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({ path: `${SCREENS}/${name}.png`, fullPage: true });
}

async function expectGuide(page: Page) {
  await expect(page).toHaveURL(/\/how-to-use$/);
  await expect(page).toHaveTitle("How to use · Swell Travel Agent");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("How to use Swell Travel Agent");
  await expect(page.getByRole("heading", { level: 2 })).toHaveText(SECTIONS);
  const toc = page.getByRole("navigation", { name: "On this page" });
  await expect(toc.getByRole("link")).toHaveCount(SECTIONS.length);
  return toc;
}

/** Following a TOC link puts its section's heading in view, below the sticky header. */
async function expectAnchorScrolls(page: Page, title: string, hash: string) {
  await page.getByRole("navigation", { name: "On this page" }).getByRole("link", { name: title }).click();
  await expect(page).toHaveURL(new RegExp(`#${hash}$`));
  const heading = page.getByRole("heading", { level: 2, name: title });
  await expect(heading).toBeInViewport();
  const headerBottom = await page.locator("header").first().evaluate((h) => h.getBoundingClientRect().bottom);
  await expect.poll(() => heading.evaluate((h) => h.getBoundingClientRect().top)).toBeGreaterThanOrEqual(headerBottom - 1);
}

async function expectNoHorizontalScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
}

test.describe("How to use · desktop", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the tab opens the guide from the map, and its table of contents scrolls to each section", async ({ page }) => {
    await stubTiles(page);
    await page.goto("/map");
    await expect(page.getByLabel("Map of monitored surf spots")).toBeVisible();
    const nav = page.getByRole("navigation", { name: "Main" });
    await nav.getByRole("link", { name: "How to use" }).click();
    await expectGuide(page);
    await expect(nav.getByRole("link", { name: "How to use" })).toHaveAttribute("aria-current", "page");
    await expect(nav.getByRole("link", { name: "Surf map" })).not.toHaveAttribute("aria-current", "page");
    await shot(page, "howto-desktop-full");

    await expectAnchorScrolls(page, "Tips and questions", "faq");
    await expectAnchorScrolls(page, "Read the surf map", "surf-map");
    // The table of contents stays beside the text while it scrolls.
    await expect(page.getByRole("navigation", { name: "On this page" })).toBeInViewport();
    await expectNoHorizontalScroll(page);

    // The footer links the guide too.
    await expect(page.getByRole("contentinfo").getByRole("link", { name: "How to use" })).toHaveAttribute("href", "/how-to-use");
  });

  test("the landing page links the guide", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: /Read the guide/ }).click();
    await expectGuide(page);
  });
});

test.describe("How to use · phone", () => {
  test.use({ viewport: { width: 412, height: 915 }, hasTouch: true, isMobile: true });

  test("the menu reaches the guide, which fits the screen", { tag: "@mobile" }, async ({ page }) => {
    await stubTiles(page);
    await page.goto("/map");
    await expect(page.getByLabel("Map of monitored surf spots")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden();
    await page.getByRole("button", { name: "Toggle menu" }).click();
    const menu = page.getByRole("navigation", { name: "Mobile" });
    await menu.getByRole("link", { name: "How to use" }).click();
    await expectGuide(page);
    await expect(menu).toBeHidden();
    await expectNoHorizontalScroll(page);
    await shot(page, "howto-mobile-full");

    await expectAnchorScrolls(page, "Save a search", "searches");
    await expectNoHorizontalScroll(page);
  });
});
