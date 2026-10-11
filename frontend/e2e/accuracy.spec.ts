import { execFileSync } from "node:child_process";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { verifyEmailFromOutbox } from "./helpers";

const BACKEND = path.resolve(__dirname, "../../backend");

/** Promote an account with the documented CLI (there is deliberately no API for this). */
function makeAdmin(email: string) {
  execFileSync(path.join(BACKEND, ".venv/bin/python"), ["-m", "app.cli", "make-admin", email], { cwd: BACKEND, env: process.env, stdio: "pipe" });
}

test.describe("developer accuracy dashboard", () => {
  test("is admin-only and renders its empty state honestly", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const email = `accuracy-${Date.now()}@example.com`;
    await page.goto("/register");
    await page.fill("#full_name", "Accuracy Dev");
    await page.fill("#email", email);
    await page.fill("#password", "Accuracy-dev-9!");
    await page.fill("#confirm_password", "Accuracy-dev-9!");
    await page.getByRole("button", { name: "Create account" }).click();
    await page.waitForURL("**/dashboard**");
    await verifyEmailFromOutbox(page, email);

    await page.goto("/status/accuracy");
    await expect(page.getByText("The accuracy dashboard is for admin accounts")).toBeVisible();
    expect((await page.request.get("/api/admin/accuracy/overview")).status()).toBe(403);

    makeAdmin(email);
    await page.goto("/status");
    await page.getByTestId("accuracy-link").click();
    await page.waitForURL("**/status/accuracy");
    await expect(page.getByTestId("accuracy-notes")).toContainText("not surf (breaking) height");
    await expect(page.getByText("none deployed")).toBeVisible();
    await expect(page.getByTestId("accuracy-filters")).toBeVisible();
    await expect(page.getByText(/No model has been trained yet/)).toBeVisible();
    expect(errors).toEqual([]);
  });
});
