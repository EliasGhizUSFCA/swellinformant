import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * Smoke test of the local Vercel-style stack (docker-compose.vercel.yml): the app as Vercel
 * runs it, through the stack's edge. Run it with playwright.vercel.config.ts, after
 * `scripts/vercel-local.sh up` and a sync (`scripts/vercel-local.sh smoke` does both checks
 * and passes the values below); the default playwright.config.ts ignores this file.
 *
 * Accounts are synthetic (example.com addresses); every email goes to the stack's Mailpit.
 */

const MAILPIT = (process.env.VERCEL_SMOKE_MAILPIT_URL ?? "http://localhost:18025").replace(/\/$/, "");
const CRON_SECRET = process.env.VERCEL_SMOKE_CRON_SECRET ?? "";
const EXPECT_MAP_FIELDS = process.env.VERCEL_SMOKE_MAP_FIELDS === "1";
const SHOTS = process.env.VERCEL_SMOKE_SHOTS;
const CRON_PATH = "/api/internal/cron/daily";
// SyncComponentOut.state (backend/app/schemas/sync.py), shown as each component row's data-state.
const COMPONENT_STATES = ["current", "new", "waiting", "in_progress", "failed", "unknown", "disabled"];
// Headers only the API sets (backend/app/core/middleware.py). A Next.js rewrite would add the
// web's own (next.config.ts: "default-src 'self'", "geolocation=(self)").
const API_CSP = "default-src 'none'; frame-ancestors 'none'";
const API_PERMISSIONS = "geolocation=(), camera=(), microphone=()";

// Playwright passes the fixtures first; none is needed here. playwright.config.ts already
// ignores this file; the skip keeps a run with any other config from failing on it.
test.beforeEach(({}, testInfo) => {
  test.skip(testInfo.config.metadata?.stack !== "vercel-local", "runs with playwright.vercel.config.ts (scripts/vercel-local.sh smoke)");
});

async function getJson(request: APIRequestContext, path: string) {
  const res = await request.get(path);
  expect(res.status(), `GET ${path}`).toBe(200);
  return res.json();
}

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` });
}

/** The newest email to ``to`` in Mailpit (its REST API), polled until one arrives. */
async function mailpitText(request: APIRequestContext, to: string, pattern: RegExp): Promise<string> {
  let text = "";
  await expect
    .poll(
      async () => {
        const found = await request.get(`${MAILPIT}/api/v1/search?query=${encodeURIComponent(`to:"${to}"`)}`);
        if (!found.ok()) return false;
        const { messages } = (await found.json()) as { messages?: Array<{ ID: string }> };
        for (const m of messages ?? []) {
          const full = await request.get(`${MAILPIT}/api/v1/message/${m.ID}`);
          const body = (await full.json()) as { Text?: string };
          if (body.Text && pattern.test(body.Text)) {
            text = body.Text;
            return true;
          }
        }
        return false;
      },
      { message: `an email to ${to} matching ${pattern} in Mailpit (${MAILPIT})`, timeout: 30_000 },
    )
    .toBe(true);
  return text;
}

test.describe("local Vercel-style stack", () => {
  test("/api goes to the API itself, every other path to the web service", async ({ request }) => {
    const health = await request.get("/api/health");
    expect(health.status()).toBe(200);
    expect(await health.json()).toEqual({ status: "ok" });
    expect(health.headers()["content-security-policy"]).toBe(API_CSP);
    expect(health.headers()["permissions-policy"]).toBe(API_PERMISSIONS);

    // An unknown API path is FastAPI's JSON 404, not the web's not-found page.
    const missing = await request.get("/api/no-such-route");
    expect(missing.status()).toBe(404);
    expect(missing.headers()["content-type"]).toContain("application/json");
    expect(missing.headers()["content-security-policy"]).toBe(API_CSP);

    const home = await request.get("/");
    expect(home.status()).toBe(200);
    expect(home.headers()["content-type"]).toContain("text/html");
    expect(home.headers()["content-security-policy"]).toContain("default-src 'self'");
  });

  test("home and map load; the map index is available after a sync", async ({ page, request }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));

    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Chase the Swell.");
    await shot(page, "home");

    const index = (await getJson(request, "/api/map/fields")) as {
      available: boolean;
      enabled?: boolean;
      run_key: string | null;
      valid_times: string[];
      message: string | null;
      published_at: string | null;
    };
    if (EXPECT_MAP_FIELDS) {
      expect(index.available, "map layers were synced (MAP_FIELDS_ENABLED=true)").toBe(true);
      expect(index.run_key).toBeTruthy();
      expect(index.valid_times.length).toBeGreaterThan(0);
      // The map serves the run of the newest published sync (SYNC_PUBLISH_GATE).
      expect(index.published_at, "the served map run was published by a sync").toBeTruthy();
    } else {
      // Map layers off (the stack's default): the index answers and says so.
      expect(index.available).toBe(false);
      expect(index.message).toBeTruthy();
    }

    await page.goto("/map");
    await expect(page.getByLabel("Map of monitored surf spots")).toBeVisible();
    await expect(page.locator("path.spot-marker")).toHaveCount(50);
    if (EXPECT_MAP_FIELDS) {
      const field = page.locator("canvas.map-field-canvas");
      await expect.poll(async () => Number((await field.getAttribute("data-painted")) ?? 0), { message: "ocean pixels painted", timeout: 60_000 }).toBeGreaterThan(10_000);
      // The model-run label carries the publication the map shows (status-ui).
      await expect(page.getByTestId("map-lead-time")).toHaveAttribute("data-published-at", index.published_at!);
    } else if (index.enabled === false) {
      // MAP_FIELDS_ENABLED=false (the stack's default) is a choice, not an outage: no layer
      // tabs and no "unavailable" notice (review r4-spec-R4-SPEC-2; it showed the notice before).
      await expect(page.getByTestId("fields-off")).toBeVisible();
      await expect(page.getByTestId("fields-notice")).toHaveCount(0);
    } else {
      await expect(page.getByTestId("fields-notice")).toBeVisible();
    }
    await shot(page, "map");
    expect(errors).toEqual([]);
  });

  test("a spot page shows its forecast", async ({ page, request }) => {
    const spots = (await getJson(request, "/api/spots")) as Array<{ slug: string; name: string }>;
    expect(spots.length).toBe(50);
    const spot = spots.find((s) => s.slug === "jeffreys-bay") ?? spots[0]!;
    await page.goto(`/spots/${spot.slug}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(spot.name);
    await expect(page.getByTestId("surf-forecast")).toBeVisible();
    await expect(page.getByTestId("forecast-hour")).toBeVisible();
    await expect(page.getByTestId("timeline-error")).toHaveCount(0);
    await shot(page, "spot");
  });

  test("register, verify the email through Mailpit, sign out and log in", async ({ page, baseURL }) => {
    const email = `vercel-smoke-${Date.now()}@example.com`;
    const password = "Barrels4Days!";
    await page.goto("/register");
    await page.fill("#full_name", "Smoke Surfer");
    await page.fill("#email", email);
    await page.fill("#password", password);
    await page.fill("#confirm_password", password);
    await page.getByRole("button", { name: "Create account" }).click();
    await page.waitForURL("**/dashboard**");

    // Sent inline by the API (JOB_RUNNER=daemon), over SMTP to Mailpit; the link points at the
    // edge (APP_BASE_URL).
    const text = await mailpitText(page.request, email, /verify-email\?token=/);
    const link = /(\S+)\/verify-email\?token=([\w-]+)/.exec(text);
    expect(link, "verification link").toBeTruthy();
    expect(link![1]).toBe(baseURL!.replace(/\/$/, ""));
    await page.goto(`/verify-email?token=${link![2]}`);
    await expect(page.getByText("Email verified")).toBeVisible();

    await page.goto("/dashboard");
    await page.getByRole("button", { name: "Account menu" }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await page.waitForURL((url) => url.pathname === "/");
    await page.goto("/dashboard");
    await page.waitForURL("**/login**");
    await page.fill("#email", email);
    await page.fill("#password", password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL("**/dashboard");
    const me = (await getJson(page.request, "/api/auth/me")) as { email: string; email_verified: boolean };
    expect(me.email).toBe(email);
    expect(me.email_verified).toBe(true);
  });

  test("the /status page shows a published sync", async ({ page, request }) => {
    const health = (await getJson(request, "/api/health/data")) as {
      sync: {
        runner: string;
        last_check_at: string | null;
        last_published: { published_at: string | null } | null;
        components: Array<{ kind: string; source: string; state: string }> | null;
      } | null;
    };
    expect(health.sync, "GET /api/health/data has a sync block").toBeTruthy();
    expect(health.sync!.runner).toBe("daemon");
    expect(health.sync!.last_published?.published_at, "a sync was published").toBeTruthy();
    // The daemon's heartbeat: its last metadata check and the components it saw.
    expect(health.sync!.last_check_at, "the daemon has checked the providers").toBeTruthy();
    const components = health.sync!.components ?? [];
    expect(components.some((c) => c.kind === "spots"), "a spot-forecast component").toBe(true);

    await page.goto("/status");
    await expect(page.getByTestId("data-health-summary")).toBeVisible();
    const sync = page.getByTestId("sync-status");
    await expect(sync).toBeVisible();
    await expect(sync).toContainText(/published/i);
    // "Updated with the ... model run · published 14:32 UTC" or "Published Oct 18, 14:32 UTC",
    // never "Nothing published yet.".
    await expect(sync.getByTestId("sync-published")).toContainText(/published .*\d{2}:\d{2} UTC/i);
    await expect(sync.getByTestId("sync-last-check")).toContainText(/last check/i);
    await expect(sync.getByTestId("sync-last-check")).not.toContainText("Not checking");
    // One row per component of the report, each with its state.
    const rows = sync.getByTestId("sync-components").locator("li[data-state]");
    await expect(rows).toHaveCount(components.length);
    const states = await rows.evaluateAll((items) => items.map((item) => item.getAttribute("data-state")));
    for (const state of states) expect(COMPONENT_STATES).toContain(state);
    await expect(sync.locator('[data-testid^="sync-component-spots-"]').first()).toBeVisible();
    if (EXPECT_MAP_FIELDS) await expect(sync.locator('[data-testid^="sync-component-map-"]')).toHaveCount(1);
    await shot(page, "status");
  });

  test("the daily cron endpoint answers 401 without the token and 200 with it", async ({ request, baseURL }) => {
    expect(CRON_SECRET, "VERCEL_SMOKE_CRON_SECRET (the stack's CRON_SECRET)").not.toBe("");
    const denied = await request.get(CRON_PATH);
    expect(denied.status()).toBe(401);
    const wrong = await request.get(CRON_PATH, { headers: { Authorization: "Bearer not-the-cron-secret" } });
    expect(wrong.status()).toBe(401);

    // Node's fetch, not the traced `request` fixture: a retained trace (test-results/) records
    // request headers, and the real token must not end up there.
    const ok = await fetch(new URL(CRON_PATH, baseURL), { headers: { Authorization: `Bearer ${CRON_SECRET}` } });
    expect(ok.status).toBe(200);
    expect(ok.headers.get("cache-control")).toContain("no-store");
    const body = (await ok.json()) as Record<string, unknown>;
    // "refresh": the daily cron also runs one forecast refresh check, the backstop for a
    // scheduler that stopped calling /api/internal/refresh (backend/app/api/internal.py).
    // "jobs" (review r1-spec-SPEC-2): the periodic jobs no daemon ran on time, run by the
    // cron itself; the stack runs JOB_RUNNER=daemon, so it is the summary, never null.
    expect(Object.keys(body).sort()).toEqual(["alerts_sent", "heartbeat_age_minutes", "jobs", "problems", "refresh", "status"]);
    expect(typeof (body.refresh as { status?: unknown }).status).toBe("string");
    expect(body.jobs).toEqual({ overdue: expect.any(Array), jobs: expect.any(Array), deferred: expect.any(Array) });
  });
});
