import { expect, test, type Page } from "@playwright/test";

import { apiGet, apiPost, verifyEmailFromOutbox } from "./helpers";

/**
 * Spec 28: each saved search chooses how far ahead it alerts (alert lead time), the wizard
 * explains that forecasts get less certain the further ahead they look, and short-notice
 * swells (about 3–7 days ahead) lead the dashboard. The backend runs in demo mode: the swell
 * is simulated and the fares are clearly labelled mock fares.
 */
const SCREENS = process.env.FLIGHTS_SCREENS_DIR;

/** Design-QA screenshots (only when FLIGHTS_SCREENS_DIR is set): desktop at 1440 × 900. */
async function shot(page: Page, name: string) {
  if (!SCREENS) return;
  const size = page.viewportSize();
  if (size && size.width >= 1024 && size.width !== 1440) await page.setViewportSize({ width: 1440, height: 900 });
  // From the top, so the sticky header is not captured halfway down a full-page image.
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.screenshot({ path: `${SCREENS}/${name}.png`, fullPage: true });
}

async function signUp(page: Page, prefix: string) {
  const email = `${prefix}-${Date.now()}@example.com`;
  await page.goto("/register");
  await page.fill("#full_name", "Lead Time Tester");
  await page.fill("#email", email);
  await page.fill("#password", "Lead-time-pass-1!");
  await page.fill("#confirm_password", "Lead-time-pass-1!");
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL(/dashboard/);
  await verifyEmailFromOutbox(page, email);
}

async function toDatesStep(page: Page) {
  await page.goto("/searches/new");
  await page.getByRole("button", { name: "Continue" }).click(); // surf defaults
  await page.getByRole("radio", { name: /Specific spots/ }).click();
  await page.getByPlaceholder("Filter spots…").fill("Uluwatu");
  await page.getByRole("checkbox", { name: /Uluwatu/ }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByTestId("alert-lead-time")).toBeVisible();
}

test.describe.serial("alert lead time in the search wizard", () => {
  test("is validated, saved, shown, editable, and short-notice trips lead the dashboard", async ({ page }) => {
    await signUp(page, "lead");
    await toDatesStep(page);
    // The deployment's defaults (ALERT_LEAD_DEFAULT_*: 2–10 days) and the uncertainty copy.
    await expect(page.locator("#alert_lead_min_days")).toHaveValue("2");
    await expect(page.locator("#alert_lead_max_days")).toHaveValue("10");
    await expect(page.getByTestId("lead-time-uncertainty")).toContainText("less certain the further ahead");
    await expect(page.getByTestId("lead-time-uncertainty")).toContainText("3–7 days ahead are listed first");

    // The earliest after the latest is refused before anything is sent.
    await page.locator("#alert_lead_min_days").fill("8");
    await page.locator("#alert_lead_max_days").fill("3");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText("Must be at least the earliest lead time.")).toBeVisible();
    await page.locator("#alert_lead_min_days").fill("1");
    await page.locator("#alert_lead_max_days").fill("6");
    await expect(page.getByText("Must be at least the earliest lead time.")).toHaveCount(0); // fixed fields drop it
    await shot(page, "wizard-lead-time-desktop");
    await page.getByRole("button", { name: "Continue" }).click();

    await page.locator("#origins").fill("SYD");
    await page.getByRole("option", { name: /SYD/ }).first().click();
    await page.locator("#max_price").fill("3000");
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Continue" }).click(); // email notifications
    await expect(page.getByText("Alerts for swells 1–6 days ahead")).toBeVisible();
    await page.locator("#name").fill("Bali at short notice");
    await page.getByRole("button", { name: "Activate search" }).click();
    await page.waitForURL(/\/searches\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("search-lead-time")).toContainText("Swells starting 1–6 days ahead");
    const searchUrl = page.url();

    // Editing keeps the saved values and changes them.
    await page.getByRole("link", { name: "Edit" }).click();
    await page.waitForURL(/\/edit$/);
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.locator("#alert_lead_min_days")).toHaveValue("1");
    await expect(page.locator("#alert_lead_max_days")).toHaveValue("6");
    await page.locator("#alert_lead_max_days").fill("7");
    for (let i = 0; i < 3; i++) await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Save changes" }).click();
    await page.waitForURL(searchUrl);
    await expect(page.getByTestId("search-lead-time")).toContainText("Swells starting 1–7 days ahead");

    // A swell 4.5 days out (a 5-day floor once hid it) becomes a short-notice trip.
    const sim = await apiPost(page, "/api/dev/simulate-swell", {
      spot_slug: "uluwatu",
      days_ahead: 4.5,
      duration_hours: 36,
      target_breaking_ft: 8,
      period_s: 16,
      deliver: false,
    });
    expect(sim.simulation.status).toBe("success");
    await page.goto("/dashboard");
    const soon = page.getByTestId("short-notice-trips");
    await expect(soon).toBeVisible();
    await expect(soon.getByTestId("opportunity-card").filter({ hasText: "Uluwatu" })).toBeVisible();
    await shot(page, "dashboard-short-notice-desktop");
    await soon.getByTestId("opportunity-card").first().click();
    await page.waitForURL(/\/opportunities\//);
    await expect(page.getByTestId("offer-card").first()).toBeVisible();
    await expect(page.getByTestId("offer-operated-by").first()).toContainText("Operated by");
    await expect(page.getByText("Route searches open a new search; fares there may differ.").first()).toBeVisible();
    await shot(page, "opportunity-offers-desktop");

    // The alert email (spec 29): delivered to the development inbox by the next run.
    await apiPost(page, "/api/dev/run-pipeline");
    const outbox = (await apiGet(page.request, "/api/dev/outbox?limit=200")) as Array<{ subject?: string; text?: string; html?: string | null }>;
    const alert = outbox.find((m) => (m.subject ?? "").includes("Uluwatu"));
    expect(alert, "the Uluwatu alert email").toBeTruthy();
    for (const line of ["short notice", "Operated by:", "Flight dates:", "Destination airport: DPS —", "(route search — fares may differ)", "Your dashboard:", "Unsubscribe from email alerts:"]) {
      expect(alert!.text, line).toContain(line);
    }
    if (SCREENS && alert!.html) {
      await page.setContent(alert!.html);
      await shot(page, "alert-email-desktop");
    }
  });

  test("fits a phone screen", { tag: "@mobile" }, async ({ page }) => {
    await signUp(page, "lead-mobile");
    await toDatesStep(page);
    await page.getByTestId("alert-lead-time").scrollIntoViewIfNeeded();
    const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(scroll).toBeLessThanOrEqual(1); // no sideways scrolling
    await expect(page.locator("#alert_lead_min_days")).toBeVisible();
    await expect(page.locator("#alert_lead_max_days")).toBeVisible();
    await shot(page, "wizard-lead-time-pixel7");
  });
});
