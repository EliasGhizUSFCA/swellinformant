import { expect, test } from "@playwright/test";

import { apiGet, apiPost, DEV_INBOX, waitForDevEmail } from "./helpers";

/**
 * The 15-step reproducible end-to-end scenario from the upgrade specification (Part 16).
 *
 * Runs against the demo forecast provider with DEMO_NATURAL_SWELLS=false (calm seas), so the
 * only swell is the clearly labelled simulated one, and the demo flight provider, whose fares
 * are labelled MOCK. Email goes to a development inbox: Mailpit when E2E_MAILPIT_URL is set
 * (`E2E_MAILPIT=1 ./scripts/run-e2e.sh`), otherwise the console provider's capture file. No
 * real mailbox is involved, so this does not verify external provider delivery.
 */
test.describe.serial("upgrade specification: 15-step scenario", () => {
  const email = `scenario-${Date.now()}@example.com`;
  const password = "Scenario-swell-9!";

  test("account → verification → map → search → simulated swell and flight → alert → opportunity", async ({ page }) => {
    test.info().annotations.push({ type: "dev inbox", description: DEV_INBOX });

    await test.step("1. create a new user account", async () => {
      await page.goto("/register");
      await page.fill("#full_name", "Scenario Surfer");
      await page.fill("#email", email);
      await page.fill("#password", password);
      await page.fill("#confirm_password", password);
      await page.getByRole("button", { name: "Create account" }).click();
      await page.waitForURL("**/dashboard**");
    });

    let verifyLink = "";
    await test.step("2. the verification email reaches the development inbox", async () => {
      const msg = await waitForDevEmail(page.request, email, /verify-email\?token=/);
      expect(msg.subject).toMatch(/verify/i);
      verifyLink = /(https?:\/\/\S+\/verify-email\?token=[\w-]+)/.exec(msg.text)![1]!;
      const status = await apiGet(page.request, "/api/auth/verification-status");
      expect(status.email_verified).toBe(false);
      expect(status.last_email.status).toBe("captured"); // a dev inbox captures; nothing claims "delivered"
    });

    await test.step("3. complete verification from the emailed link", async () => {
      await page.goto(new URL(verifyLink).pathname + new URL(verifyLink).search);
      await expect(page.getByText("Email verified")).toBeVisible();
      expect((await apiGet(page.request, "/api/auth/verification-status")).email_verified).toBe(true);
    });

    await test.step("4. log into the application", async () => {
      await page.goto("/dashboard");
      await page.getByRole("button", { name: "Account menu" }).click();
      await page.getByRole("menuitem", { name: "Sign out" }).click();
      await page.waitForURL((url) => url.pathname === "/");
      await page.goto("/login");
      await page.fill("#email", email);
      await page.fill("#password", password);
      await page.getByRole("button", { name: "Sign in" }).click();
      await page.waitForURL("**/dashboard");
    });

    await test.step("5. open the interactive map", async () => {
      await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (route) => route.fulfill({ status: 204 }));
      await page.goto("/map");
      await expect(page.locator("path.spot-marker")).toHaveCount(50);
    });

    await test.step("6. select a surf spot", async () => {
      await page.locator("path.spot-jeffreys-bay").click({ force: true });
      await expect(page.getByTestId("panel-spot-name")).toHaveText("Jeffreys Bay (Supertubes)");
      await expect(page.getByTestId("map-spot-panel").getByTestId("our-prediction")).toBeVisible();
    });

    await test.step("7. view its forecast", async () => {
      await page.getByRole("link", { name: /Full forecast & charts/ }).click();
      await page.waitForURL("**/spots/jeffreys-bay");
      await expect(page.getByTestId("quality-chart")).toBeVisible();
      await expect(page.getByTestId("forecast-hour").getByTestId("raw-model")).toBeVisible();
      await expect(page.getByTestId("forecast-hour").getByTestId("uncertainty-range")).toBeVisible();
    });

    let searchId = "";
    await test.step("8. create an active saved search", async () => {
      await page.goto("/searches/new");
      await page.getByRole("button", { name: "Continue" }).click();
      await page.getByRole("radio", { name: /Specific spots/ }).click();
      await page.getByPlaceholder("Filter spots…").fill("Jeffreys");
      await page.getByRole("checkbox", { name: /Jeffreys Bay/ }).click();
      await page.getByRole("button", { name: "Continue" }).click();
      await page.getByRole("button", { name: "Continue" }).click();
      await page.locator("#origins").fill("SFO");
      await page.getByRole("option", { name: /SFO/ }).first().click();
      await page.locator("#max_price").fill("3000");
      await page.locator("#max_flight_hours").fill("40");
      await page.getByRole("button", { name: "Continue" }).click();
      await page.getByRole("button", { name: "Continue" }).click();
      await page.locator("#name").fill("Scenario J-Bay");
      await page.getByRole("button", { name: "Activate search" }).click();
      await page.waitForURL(/\/searches\/[0-9a-f-]{36}$/);
      searchId = page.url().split("/").pop()!;
      await expect(page.getByText("Active").first()).toBeVisible();
    });

    await test.step("9. insert a clearly labelled simulated qualifying swell", async () => {
      const sim = await apiPost(page, "/api/dev/simulate-swell", {
        spot_slug: "jeffreys-bay",
        days_ahead: 5.8,
        duration_hours: 48,
        run_pipeline: false,
      });
      expect(sim.simulation.status).toBe("success");
      expect(sim.simulation.run_key).toMatch(/^demo-sim:/);
      const fc = await apiGet(page.request, "/api/spots/jeffreys-bay/forecast?days=10");
      expect(fc.source.is_demo).toBe(true); // DEMO source, labelled as such everywhere
    });

    type Counts = Record<string, number>;
    let pipeline: Record<string, Counts> = {};
    await test.step("10–11. run matching with the labelled mock flight provider", async () => {
      pipeline = await apiPost(page, "/api/dev/run-pipeline?acquire=false&deliver=false");
      const { detection, matching, flights } = pipeline as Record<"detection" | "matching" | "flights", Counts>;
      expect(detection.created! + detection.updated!).toBeGreaterThan(0);
      expect(matching.created! + matching.updated!).toBeGreaterThan(0);
      // New matches may get their flights from a search the matching stage dispatches itself,
      // so the outcome is checked on the opportunity (step 12), not on this stage's counter.
      expect(flights.errors).toBe(0);
    });

    let matchId = "";
    await test.step("12. a notification is queued", async () => {
      interface Opp {
        id: string;
        status: string;
        search_id: string;
        spot: { slug: string };
        has_mock_flights: boolean;
        best_price: string | number | null;
      }
      const opps = (await apiGet(page.request, "/api/opportunities")) as Opp[];
      const jbay = opps.find((o) => o.spot.slug === "jeffreys-bay" && o.search_id === searchId);
      expect(jbay, "opportunity for the new search").toBeTruthy();
      expect(jbay!.status).toBe("flight_found");
      expect(jbay!.has_mock_flights).toBe(true); // labelled mock fares from the demo provider
      expect(Number(jbay!.best_price)).toBeLessThanOrEqual(3000);
      matchId = jbay!.id;
      const history = await apiGet(page.request, "/api/notifications");
      expect(history.items[0].status).toBe("queued");
      expect(history.items[0].kind).toBe("new_opportunity");
    });

    let alertLink = "";
    await test.step("13. the alert email appears in the development inbox", async () => {
      await apiPost(page, "/api/dev/run-pipeline?acquire=false&deliver=true");
      const msg = await waitForDevEmail(page.request, email, /Opportunity details: /);
      expect(msg.text).toContain("Jeffreys Bay");
      expect(msg.text).toContain("MOCK FARE");
      expect(msg.text).toMatch(/Predicted breaking waves \(our estimate\)/);
      alertLink = /Opportunity details: (\S+)/.exec(msg.text)![1]!;
      const history = await apiGet(page.request, "/api/notifications");
      expect(history.items[0].status).toBe("captured");
    });

    await test.step("14. open the alert link", async () => {
      const url = new URL(alertLink);
      await page.goto(url.pathname + url.search);
    });

    await test.step("15. it leads to the correct opportunity", async () => {
      expect(page.url()).toContain(`/opportunities/${matchId}`);
      await expect(page.getByRole("heading", { name: /Jeffreys Bay/ }).first()).toBeVisible();
      await expect(page.getByTestId("offer-card").first()).toBeVisible();
      await expect(page.getByText("MOCK FARES").first()).toBeVisible();
    });
  });
});
