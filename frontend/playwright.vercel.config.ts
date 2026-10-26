import { defineConfig, devices } from "@playwright/test";

/**
 * Smoke test of the local Vercel-style stack (docker-compose.vercel.yml,
 * docs/local-vercel-stack.md): e2e/vercel-smoke.spec.ts against the stack's edge, which
 * routes /api/* to the lean API and everything else to the Next.js production build, as
 * Vercel does. Start the stack and run a sync first; `scripts/vercel-local.sh smoke` runs the
 * shell checks and then this config with the stack's values:
 *
 *   VERCEL_SMOKE_BASE_URL     the edge (default http://localhost:3100)
 *   VERCEL_SMOKE_MAILPIT_URL  Mailpit's inbox and API (default http://localhost:18025)
 *   VERCEL_SMOKE_CRON_SECRET  the stack's CRON_SECRET
 *   VERCEL_SMOKE_MAP_FIELDS   "1" when the stack syncs the map layers (MAP_FIELDS_ENABLED)
 *   VERCEL_SMOKE_SHOTS        optional directory for screenshots of the pages it opens
 *
 * The spec runs only with this config (metadata.stack); the default playwright.config.ts
 * skips it.
 */
const chromiumLaunch = process.env.PLAYWRIGHT_CHROMIUM_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } : {};

export default defineConfig({
  testDir: "./e2e",
  testMatch: /vercel-smoke\.spec\.ts$/,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  outputDir: "test-results/vercel-smoke",
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report/vercel-smoke" }]],
  metadata: { stack: "vercel-local" },
  use: {
    baseURL: process.env.VERCEL_SMOKE_BASE_URL ?? "http://localhost:3100",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], launchOptions: chromiumLaunch } }],
});
