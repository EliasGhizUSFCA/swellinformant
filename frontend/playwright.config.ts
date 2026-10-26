import { defineConfig, devices, type Project } from "@playwright/test";

/**
 * End-to-end tests run against the real stack: Next.js (production build) on :3000 and
 * the FastAPI backend on :8000 in demo mode with developer tools enabled. Start them
 * yourself (see README "Running tests") or let `scripts/run-e2e.sh` do it.
 *
 * Projects: "chromium" (desktop Chrome) runs every test. "mobile" (Pixel 7 emulation, also
 * Chromium) runs the tests tagged @mobile. "firefox" and "webkit" run the tests tagged
 * @cross-browser, and only when PLAYWRIGHT_ALL_BROWSERS=1 (their browsers must be installed).
 * Tag a test with `test("title", { tag: "@mobile" }, async ({ page }) => ...)`.
 */
const chromiumLaunch = process.env.PLAYWRIGHT_CHROMIUM_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } : {};

const otherBrowsers: Project[] =
  process.env.PLAYWRIGHT_ALL_BROWSERS === "1"
    ? [
        { name: "firefox", use: { ...devices["Desktop Firefox"] }, grep: /@cross-browser/ },
        { name: "webkit", use: { ...devices["Desktop Safari"] }, grep: /@cross-browser/ },
      ]
    : [];

export default defineConfig({
  testDir: "./e2e",
  // The local Vercel-style stack's smoke spec runs only with playwright.vercel.config.ts
  // (scripts/vercel-local.sh smoke), against that stack's edge.
  testIgnore: /vercel-smoke\.spec\.ts$/,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: chromiumLaunch,
      },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"], launchOptions: chromiumLaunch },
      grep: /@mobile/,
    },
    ...otherBrowsers,
  ],
});
