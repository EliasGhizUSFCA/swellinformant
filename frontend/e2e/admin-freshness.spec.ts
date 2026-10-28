import { execFileSync } from "node:child_process";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { verifyEmailFromOutbox } from "./helpers";

const BACKEND = path.resolve(__dirname, "../../backend");

/** Promote an account with the documented CLI (there is deliberately no API for this). */
function makeAdmin(email: string) {
  execFileSync(path.join(BACKEND, ".venv/bin/python"), ["-m", "app.cli", "make-admin", email], { cwd: BACKEND, env: process.env, stdio: "pipe" });
}

test.describe("admin freshness dashboard", () => {
  test("is admin-only, never cached, and shows every section-16 group", { tag: "@mobile" }, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    // Signed out: the API refuses, the page sends the visitor to sign in.
    const anonymous = await page.request.get("/api/admin/freshness");
    expect(anonymous.status()).toBe(401);
    await page.goto("/admin/freshness");
    await page.waitForURL(/\/login\?next=%2Fadmin%2Ffreshness$/);

    const email = `freshness-${Date.now()}@example.com`;
    await page.goto("/register");
    await page.fill("#full_name", "Freshness Admin");
    await page.fill("#email", email);
    await page.fill("#password", "Freshness-adm-9!");
    await page.fill("#confirm_password", "Freshness-adm-9!");
    await page.getByRole("button", { name: "Create account" }).click();
    await page.waitForURL("**/dashboard**");
    await verifyEmailFromOutbox(page, email);

    // A user: 403 from the API and a plain message on the page; no link on the status page.
    expect((await page.request.get("/api/admin/freshness")).status()).toBe(403);
    await page.goto("/admin/freshness");
    await expect(page.getByTestId("freshness-forbidden")).toBeVisible();
    await page.goto("/status");
    await expect(page.getByRole("heading", { name: "Status & data freshness" })).toBeVisible();
    await expect(page.getByTestId("freshness-link")).toHaveCount(0);

    makeAdmin(email);
    const ok = await page.request.get("/api/admin/freshness");
    expect(ok.status()).toBe(200);
    expect(ok.headers()["cache-control"]).toBe("private, no-store");
    const body = await ok.json();
    expect(body.active_provider.primary).toBe("demo");

    await page.goto("/status");
    await page.getByTestId("freshness-link").click();
    await page.waitForURL("**/admin/freshness");
    await expect(page.getByTestId("freshness-dashboard")).toBeVisible();
    await expect(page.getByTestId("freshness-active-provider")).toHaveText("demo");
    for (const id of [
      "freshness-problems",
      "freshness-provider",
      "freshness-component-atmosphere",
      "freshness-component-wave",
      "freshness-events",
      "freshness-published",
      "freshness-scheduler",
      "freshness-timeline",
      "freshness-stale-spots",
      "freshness-failed-requests",
      "freshness-refresh-jobs",
      "freshness-usage",
      "freshness-processing",
      "freshness-latency",
    ]) {
      await expect(page.getByTestId(id), id).toBeVisible();
    }
    // The e2e pipeline stored demo runs: the last acquisition is reported.
    await expect(page.getByTestId("freshness-last-acquired")).toContainText("UTC");
    await expect(page.getByTestId("usage-open-meteo-day")).toContainText("of 10,000");

    // Focus re-reads the report (no timer does).
    const reread = page.waitForResponse((r) => r.url().endsWith("/api/admin/freshness") && r.status() === 200, { timeout: 20_000 });
    await page.waitForTimeout(16_000); // past the 15 s the report stays fresh
    // TanStack Query's focus manager listens for visibilitychange on window (it bubbles there
    // from the document when a tab becomes visible again).
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange", { bubbles: true })));
    await reread;

    // No horizontal page scroll (the tables scroll inside their cards).
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    expect(errors).toEqual([]);
  });
});
