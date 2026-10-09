import { expect, test } from "@playwright/test";

import { apiGet, apiPost, verifyEmailFromOutbox } from "./helpers";

/**
 * The end-to-end scenario from the product specification. The backend runs in demo mode
 * with DEMO_NATURAL_SWELLS=false (calm seas) so the only swells are the ones simulated here.
 */
test.describe.serial("swell → flight → alert lifecycle", () => {
  const email = `e2e-${Date.now()}@example.com`;
  const password = "Barrels4Days!";

  test("full journey", async ({ page }) => {
    // 1. Create a user account
    await page.goto("/register");
    await page.fill("#full_name", "E2E Surfer");
    await page.fill("#email", email);
    await page.fill("#password", password);
    await page.fill("#confirm_password", password);
    await page.getByRole("button", { name: "Create account" }).click();
    await page.waitForURL("**/dashboard**");
    await verifyEmailFromOutbox(page, email);

    // 2. Log in (sign out first to exercise the real login form)
    await page.goto("/dashboard");
    await page.getByRole("button", { name: "Account menu" }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await page.waitForURL((url) => url.pathname === "/");
    await page.goto("/dashboard");
    await page.waitForURL("**/login**"); // protected route redirects
    await page.fill("#email", email);
    await page.fill("#password", password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL("**/dashboard");

    // 3. Create a saved surf search with the six-step wizard
    await page.goto("/searches/new");
    await page.getByRole("button", { name: "Continue" }).click(); // surf defaults: 5–15 ft, Good+
    await page.getByRole("radio", { name: /Specific spots/ }).click();
    await page.getByPlaceholder("Filter spots…").fill("Jeffreys");
    await page.getByRole("checkbox", { name: /Jeffreys Bay/ }).click();
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Continue" }).click(); // flexible dates, 2/1 day buffers
    await page.locator("#origins").fill("SFO");
    await page.getByRole("option", { name: /SFO/ }).first().click();
    await page.locator("#max_price").fill("3000");
    await page.locator("#max_flight_hours").fill("40");
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Continue" }).click(); // email notifications
    await page.locator("#name").fill("E2E J-Bay");
    await page.getByRole("button", { name: "Activate search" }).click();
    await page.waitForURL(/\/searches\/[0-9a-f-]{36}$/);

    // 4. View the search dashboard
    await page.goto("/searches");
    const row = page.getByTestId("search-row").filter({ hasText: "E2E J-Bay" });
    await expect(row).toBeVisible();
    await expect(row.getByTestId("search-status")).toHaveText("Active");
    await page.goto("/dashboard");
    await expect(page.getByText("Active searches").first()).toBeVisible();

    // 5 + 6. Simulate a qualifying swell (demo forecast) — the pipeline then searches
    // flights with the demo provider, which returns mock but schedulable itineraries.
    const sim = await apiPost(page, "/api/dev/simulate-swell", {
      spot_slug: "jeffreys-bay",
      days_ahead: 5.8, // inside the 5–10 day alert window, even with the swell's ramp-up
      duration_hours: 48,
      deliver: false,
    });
    expect(sim.simulation.status).toBe("success");
    expect(sim.pipeline.detection.created + sim.pipeline.detection.updated).toBeGreaterThan(0);
    expect(sim.pipeline.flights.flight_found).toBeGreaterThan(0);

    // 7. Verify an opportunity was created with an eligible (mock) flight
    const opps = (await apiGet(page.request, "/api/opportunities")) as Array<Record<string, unknown>>;
    const jbay = opps.find((o) => (o.spot as { slug: string }).slug === "jeffreys-bay");
    expect(jbay, "J-Bay opportunity").toBeTruthy();
    expect(jbay!.status).toBe("flight_found");
    expect(jbay!.has_mock_flights).toBe(true);
    expect(Number(jbay!.best_price)).toBeLessThanOrEqual(3000);

    // 8. Verify a notification is queued … then delivered by the next delivery run
    let history = await apiGet(page.request, "/api/notifications");
    expect(history.total).toBe(1);
    expect(history.items[0].status).toBe("queued");
    expect(history.items[0].kind).toBe("new_opportunity");
    await apiPost(page, "/api/dev/run-pipeline");
    history = await apiGet(page.request, "/api/notifications");
    expect(history.items[0].status).toBe("sent");

    // 9. The opportunity appears on the dashboard and its detail page shows the flight
    await page.goto("/dashboard");
    const card = page.getByTestId("opportunity-card").filter({ hasText: "Jeffreys Bay" });
    await expect(card).toBeVisible();
    await expect(card.getByText("Flights found")).toBeVisible();
    await card.click();
    await page.waitForURL(/\/opportunities\//);
    await expect(page.getByTestId("offer-card").first()).toBeVisible();
    await expect(page.getByText("Recommended trip")).toBeVisible();
    await expect(page.getByText("MOCK FARES").first()).toBeVisible();

    // 10. Pause the search
    await page.goto("/searches");
    await row.getByRole("button", { name: "Pause" }).click();
    await expect(row.getByTestId("search-status")).toHaveText("Paused");

    // 11. A new qualifying swell while paused generates no new notifications
    const second = await apiPost(page, "/api/dev/simulate-swell", {
      spot_slug: "jeffreys-bay",
      days_ahead: 9.6, // > 1 full daylight period after the first swell ends → a new event
      duration_hours: 18,
    });
    expect(second.pipeline.detection.created).toBeGreaterThan(0); // a separate, new swell event
    const events = (await apiGet(page.request, "/api/spots/jeffreys-bay/events")) as unknown[];
    expect(events.length).toBeGreaterThanOrEqual(2);
    history = await apiGet(page.request, "/api/notifications");
    expect(history.total).toBe(1);
    await page.goto("/notifications");
    await expect(page.getByTestId("notification-table").getByRole("row")).toHaveCount(2); // header + 1 alert
  });
});
