import { expect, test } from "@playwright/test";

test("landing page explains the product and shows live swells", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Chase the Swell/ })).toBeVisible();
  await expect(page.getByText("Find world-class surf and affordable flights automatically.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Get started" })).toBeVisible();
  await expect(page.getByText("How it works")).toBeVisible();
});

test("protected pages redirect to sign-in", async ({ page }) => {
  for (const path of ["/dashboard", "/searches", "/notifications", "/settings"]) {
    await page.goto(path);
    await page.waitForURL("**/login?next=**");
  }
});

test("spots catalogue, spot page and map are public", async ({ page }) => {
  await page.goto("/spots");
  await expect(page.getByRole("link", { name: /Banzai Pipeline/ })).toBeVisible();
  await page.goto("/spots/pipeline");
  await expect(page.getByRole("heading", { name: "Banzai Pipeline" })).toBeVisible();
  await expect(page.getByText("Spot characteristics")).toBeVisible();
  await expect(page.getByTestId("quality-chart")).toBeVisible();
  await page.goto("/map");
  await expect(page.getByLabel("Map of monitored surf spots")).toBeVisible();
});

test("registration validates input", async ({ page }) => {
  await page.goto("/register");
  await page.fill("#full_name", "X");
  await page.fill("#email", "not-an-email");
  await page.fill("#password", "short");
  await page.fill("#confirm_password", "different");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("Enter a valid email address.")).toBeVisible();
  await expect(page.getByText("Use at least 10 characters.")).toBeVisible();
});
