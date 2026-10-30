import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { apiGet, apiPost, verifyEmailFromOutbox } from "./helpers";

/**
 * THE CRITICAL LIVE UPDATE TEST (spec 32, steps 1-12) against the real stack: the demo
 * provider is given a new model run of both components (POST /api/dev/simulate-model-run,
 * contract C6), and the scheduler's call (POST /api/internal/refresh with the Bearer
 * CRON_SECRET, design D8) must then, with no browser reload:
 *
 *   1 detect the new run; 2 start acquiring it; 3 retrieve the updated values; 4 recompute the
 *   scores; 5 publish a new forecast version; 6 the open pages detect it (GET
 *   /api/forecasts/version); 7 refresh by themselves; 8 the charts show the new values; 9 the
 *   map markers update; 10 the swell events are re-evaluated; 11 matching and the flight checks
 *   run for a saved search; 12 a second, identical refresh publishes nothing and alerts nobody
 *   twice.
 *
 * It also measures publication -> visible on each page (the version's published_at to the
 * moment the page showed it) and prints it; the spec target is 60 s (spec 12).
 *
 * The values come from the demo provider (synthetic by design, labelled demo everywhere); the
 * simulated swell at J-Bay is the developer tool's (simulate-swell). The poll interval is
 * NEXT_PUBLIC_FORECAST_VERSION_POLL_MS (scripts/run-e2e.sh: 2 s); CRON_SECRET comes from the
 * runner too. The route-mocked counterpart is live-update-mocked.spec.ts.
 */

const POLL_MS = Number(process.env.NEXT_PUBLIC_FORECAST_VERSION_POLL_MS || 20_000);
const CRON_SECRET = process.env.CRON_SECRET ?? "";
// Schedulers call the API directly (on Vercel the platform routes /api/* to the Python
// service): the backend's own URL when the runner names it, else through the site's proxy.
const API = (process.env.API_BASE_URL ?? "").replace(/\/$/, "");
const SLUG = "jeffreys-bay";
const TARGET_MS = 60_000;

interface RefreshOut {
  status: string;
  due?: boolean;
  sync_run_id: number | null;
  version_ids: number[];
  published_at: string | null;
  stages?: Array<{ name: string; status: string; duration_ms: number }>;
  refreshes?: Array<{ source: string; run_id: number | null; run_key: string | null; status: string; spots_updated: number | null }>;
  timings?: Record<string, number>;
}
interface VersionOut {
  version_id: number | null;
  published_at: string | null;
  wind_model_run: string | null;
  wave_model_run: string | null;
  spots_updated: number;
}
interface EventOut {
  id: string;
  status: string;
  start_time: string;
  end_time: string;
  version: number;
  last_updated_at: string;
  history: Array<{ run_key?: string }>;
}
interface Conditions {
  score: number;
}
interface Snapshot {
  spots: Array<{ slug: string; conditions: Conditions | null }>;
}
interface NotificationsOut {
  total: number;
  items: Array<{ match_id: string | null; channel: string; kind: string; peak_label: string | null; window_label: string | null; price: string | null }>;
}
interface TimelineOut {
  steps: Array<{ t: string; breaking: { central_ft: number }; wind: { speed_kmh: number | null } | null }>;
}

const score10 = (score: number) => (Math.max(0, Math.min(100, Math.round(score))) / 10).toFixed(1);

async function refresh(request: APIRequestContext): Promise<RefreshOut> {
  const res = await request.post(`${API}/api/internal/refresh`, { headers: { Authorization: `Bearer ${CRON_SECRET}` }, timeout: 300_000 });
  expect(res.status(), `refresh -> ${res.status()} ${await res.text()}`).toBe(200);
  expect(res.headers()["cache-control"]).toContain("no-store");
  return (await res.json()) as RefreshOut;
}

const currentVersion = async (request: APIRequestContext) => (await apiGet(request, "/api/forecasts/version")) as VersionOut;

/** Records, in the page, when a "Forecast updated" label first names another version than
 * `from` (window.__versionShownAt, epoch ms) and sets a marker that a reload would erase. */
async function watchLabel(page: Page, from: number) {
  await page.evaluate((v) => {
    const w = window as unknown as { __versionShownAt?: number; __liveMarker?: number };
    w.__liveMarker = 7;
    const check = () => {
      const el = document.querySelector('[data-testid="forecast-updated"]');
      if (w.__versionShownAt == null && el && el.getAttribute("data-version-id") !== String(v)) w.__versionShownAt = Date.now();
    };
    new MutationObserver(check).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-version-id"] });
  }, from);
}

const shownAt = (page: Page) => page.evaluate(() => (window as unknown as { __versionShownAt?: number }).__versionShownAt ?? null);
const liveMarker = (page: Page) => page.evaluate(() => (window as unknown as { __liveMarker?: number }).__liveMarker ?? null);

test.describe.serial("live update pipeline (spec 32)", () => {
  test("a new model run reaches the open map and spot pages without a reload", async ({ page, request }) => {
    test.setTimeout(10 * 60_000);
    expect(CRON_SECRET, "CRON_SECRET (scripts/run-e2e.sh exports it)").not.toBe("");

    // The refresh route is the scheduler's: no secret, no refresh.
    expect((await request.post(`${API}/api/internal/refresh`)).status()).toBe(401);
    expect((await request.post(`${API}/api/internal/refresh`, { headers: { Authorization: "Bearer not-the-cron-secret" } })).status()).toBe(401);

    // --- seed: a verified user, a saved search for J-Bay and a qualifying swell there -----
    const email = `e2e-live-${Date.now()}@example.com`;
    await page.goto("/");
    await page.request.get("/api/auth/csrf");
    await apiPost(page, "/api/auth/register", { full_name: "E2E Live Surfer", email, password: "Barrels4Days!", confirm_password: "Barrels4Days!" });
    await verifyEmailFromOutbox(page, email);
    const search = (await apiPost(page, "/api/searches", {
      name: "E2E live J-Bay",
      units: "ft",
      wave_min: 5,
      wave_max: 15,
      min_quality: "good",
      destination_mode: "spots",
      destinations: { spot_slugs: [SLUG] },
      origins: ["SFO"],
      travel: { max_price: 3000, max_flight_hours: 40 },
    })) as { id: string };
    const swellHours = 48;
    const sim = await apiPost(page, "/api/dev/simulate-swell", { spot_slug: SLUG, days_ahead: 5.8, duration_hours: swellHours, deliver: false });
    expect(sim.simulation.status).toBe("success");
    const eventsBefore = (await apiGet(page.request, `/api/spots/${SLUG}/events`)) as EventOut[];
    // The event of this simulated swell: earlier specs in the same database can leave other
    // active J-Bay events (full-flow.spec.ts adds one 2 days ahead, before the search's
    // default alert lead time), so the first active event is not necessarily this one.
    const swellStart = Date.parse(sim.swell_start as string);
    const swellEnd = swellStart + swellHours * 3_600_000;
    const event = eventsBefore.find((e) => e.status === "active" && Date.parse(e.start_time) <= swellEnd && Date.parse(e.end_time) >= swellStart);
    expect(event, "the simulated J-Bay swell event").toBeTruthy();
    const matchesBefore = (await apiGet(page.request, `/api/searches/${search.id}/matches`)) as Array<{ id: string; event_id: string }>;
    expect(matchesBefore.some((m) => m.event_id === event!.id), "the saved search matched the swell").toBe(true);
    const notificationsBefore = ((await apiGet(page.request, "/api/notifications")) as { total: number }).total;

    const v1 = await currentVersion(request);
    expect(v1.version_id, "a published version before the new run").not.toBeNull();
    const snap1 = (await apiGet(request, "/api/map/conditions")) as Snapshot;

    // --- open the map (J-Bay selected) and the spot page (a step selected) ------------------
    await page.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (r) => r.fulfill({ status: 204 }));
    await page.goto("/map");
    await expect(page.locator("path.spot-marker")).toHaveCount(50);
    await page.locator(`path.spot-${SLUG}`).click();
    const panel = page.getByTestId("map-spot-panel");
    await expect(panel.getByTestId("panel-spot-name")).toHaveText(/^Jeffreys Bay/);
    await expect(page.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", String(v1.version_id));

    const spotPage = await page.context().newPage();
    await spotPage.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (r) => r.fulfill({ status: 204 }));
    await spotPage.goto(`/spots/${SLUG}`);
    const scroller = spotPage.getByTestId("timeline-scroller");
    await expect(scroller).toBeVisible();
    await expect(spotPage.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", String(v1.version_id));
    // A step a few hours ahead, far from the simulated swell (whose hours keep their values).
    const step = 2;
    const seg = spotPage.locator(`[data-testid="quality-strip"] rect[data-step="${step}"]`);
    await scroller.evaluate((el) => el.scrollIntoView({ block: "center" }));
    const box = (await seg.boundingBox())!;
    await spotPage.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await expect(scroller).toHaveAttribute("aria-valuenow", String(step));
    const selected = spotPage.locator('[data-testid="wave-bars"] g[data-selected]');
    const windSelected = spotPage.locator('[data-testid="wind-row"] g[data-selected]');
    const central1 = await selected.getAttribute("data-central");
    const selectedT = await selected.getAttribute("data-t");
    const wind1 = await windSelected.getAttribute("data-speed");
    const summaryTime = await spotPage.getByTestId("summary-time").textContent();
    await watchLabel(page, v1.version_id!);
    await watchLabel(spotPage, v1.version_id!);

    // --- steps 1-5: a new run, detected, acquired, scored and published -----------------
    const run = (await apiPost(page, "/api/dev/simulate-model-run?component=both", {})) as { component_keys: Record<string, string> };
    expect(Object.keys(run.component_keys).sort()).toEqual(["atmosphere", "wave"]);
    const refreshedAt = Date.now();
    const r1 = await refresh(request);
    console.log(`refresh 1: ${JSON.stringify({ status: r1.status, sync_run_id: r1.sync_run_id, version_ids: r1.version_ids, timings: r1.timings })}`);
    expect(["success", "partial"]).toContain(r1.status);
    expect(r1.due, "1: the new run was detected").toBe(true);
    const stages = new Map((r1.stages ?? []).map((s) => [s.name, s.status]));
    expect(stages.get("spots"), "2: acquisition ran").toBe("success");
    const stored = (r1.refreshes ?? []).filter((x) => x.run_id != null);
    expect(stored.length, "3: a refreshed run was stored").toBeGreaterThan(0);
    for (const key of Object.values(run.component_keys)) expect(stored.some((x) => String(x.run_key).includes(key)), `3: the stored run is the simulated ${key}`).toBe(true);
    expect(stages.get("predict"), "4: scores recomputed").toBe("success");
    expect(stages.get("publish"), "5: published").toBe("success");
    expect(r1.version_ids.length).toBeGreaterThan(0);
    // The endpoint answers from a short process memo, then Redis: allow it its few seconds.
    const newest = r1.version_ids[r1.version_ids.length - 1];
    await expect.poll(async () => (await currentVersion(request)).version_id, { message: "5: the version endpoint names the new version", timeout: 15_000 }).toBe(newest);
    const v2 = await currentVersion(request);
    expect(v2.version_id!).toBeGreaterThan(v1.version_id!);
    expect(v2.spots_updated).toBeGreaterThan(0);
    const health = (await apiGet(request, "/api/health/data?strict=false")) as { sync?: { last_run?: { id: number; trigger: string } | null } };
    expect(health.sync?.last_run?.id, "the sync status shows the refresh's sync").toBe(r1.sync_run_id);
    expect(health.sync?.last_run?.trigger).toBe("http");

    // --- steps 6-9: the open pages show it, without a reload ------------------------------
    const v2id = String(v2.version_id);
    await expect(page.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", v2id, { timeout: POLL_MS + 15_000 });
    await expect(spotPage.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", v2id, { timeout: POLL_MS + 15_000 });
    const published = Date.parse(v2.published_at!);
    const latency = { map: (await shownAt(page))! - published, spot: (await shownAt(spotPage))! - published };
    console.log(`publication -> visible: map ${latency.map} ms, spot page ${latency.spot} ms (poll ${POLL_MS} ms; refresh call took ${Date.now() - refreshedAt} ms)`);
    expect(latency.map).toBeLessThanOrEqual(TARGET_MS);
    expect(latency.spot).toBeLessThanOrEqual(TARGET_MS);
    expect(await liveMarker(page), "7: the map page was not reloaded").toBe(7);
    expect(await liveMarker(spotPage), "7: the spot page was not reloaded").toBe(7);
    await expect(spotPage.getByTestId("forecast-updated-announcement")).toHaveText("New forecast loaded");

    // 8: the selected step shows the new version's values (the wave and wind parts both moved).
    const tl2 = (await apiGet(request, `/api/spots/${SLUG}/timeline?days=10&v=${v2id}`)) as TimelineOut;
    // The selected step's valid time (data-t: epoch ms) in the new version's timeline.
    const raw = (await selected.getAttribute("data-t")) ?? "";
    const t = /^\d+$/.test(raw) ? Number(raw) : Date.parse(raw);
    const step2 = tl2.steps.find((s) => Date.parse(s.t) === t);
    expect(step2, "8: the selected time is in the new version").toBeTruthy();
    await expect(selected).toHaveAttribute("data-central", String(step2!.breaking.central_ft));
    await expect(windSelected).toHaveAttribute("data-speed", String(step2!.wind!.speed_kmh));
    expect(await selected.getAttribute("data-central"), "8: the wave chart changed").not.toBe(central1);
    expect(await windSelected.getAttribute("data-speed"), "8: the wind chart changed").not.toBe(wind1);
    // The same valid time stays selected (its index may move: the new run's steps can start
    // at another hour).
    expect(raw, "7: the selected time is kept").toBe(selectedT);
    await expect(spotPage.getByTestId("summary-time")).toHaveText(summaryTime!);

    // 9: the markers: J-Bay's panel and another spot's marker label show the new ratings.
    const snap2 = (await apiGet(request, `/api/map/conditions?v=${v2id}`)) as Snapshot;
    const before = new Map(snap1.spots.map((s) => [s.slug, s.conditions]));
    const changed = snap2.spots.find((s) => s.conditions && before.get(s.slug) && score10(s.conditions.score) !== score10(before.get(s.slug)!.score));
    expect(changed, "9: a spot whose rating changed with the new run").toBeTruthy();
    await page.locator(`path.spot-${changed!.slug}`).focus();
    await expect(page.locator(".leaflet-tooltip")).toContainText(`${score10(changed!.conditions!.score)}/10`);
    const jbay = snap2.spots.find((s) => s.slug === SLUG)!.conditions;
    await expect(panel.getByTestId("panel-spot-name")).toHaveText(/^Jeffreys Bay/);
    if (jbay) await expect(panel.getByTestId("our-prediction").getByTestId("rating-title")).toContainText(`${score10(jbay.score)}/10`);

    // --- step 10: the swell event was re-evaluated against the new run --------------------
    const eventsAfter = (await apiGet(request, `/api/spots/${SLUG}/events?v=${v2id}`)) as EventOut[];
    expect(eventsAfter.some((e) => e.id === event!.id), "10: the J-Bay event is still listed").toBe(true);
    // The event itself, with its history (one entry per evaluation, naming the run).
    const again = (await apiGet(request, `/api/events/${event!.id}`)) as EventOut;
    expect(again!.version, "10: re-evaluated").toBeGreaterThan(event!.version);
    expect(Date.parse(again!.last_updated_at)).toBeGreaterThanOrEqual(refreshedAt - 1000);
    const lastKey = again!.history[again!.history.length - 1]?.run_key ?? "";
    for (const key of Object.values(run.component_keys)) expect(lastKey, "10: against the new run").toContain(key);
    expect(stages.get("detect")).toBe("success");

    // --- step 11: matching and the flight checks ran for the saved search -----------------
    expect(stages.get("match"), "11: matching ran after the publication").toBe("success");
    expect(stages.has("flights"), "11: the flight stage ran").toBe(true);
    expect(stages.get("flights")).not.toBe("failed");
    const status = (await apiGet(request, `/api/system/status?v=${v2id}`)) as { jobs?: Array<{ job: string; status: string; started_at: string | null }> };
    for (const job of ["matching.match", "flights.discover"]) {
      const row = status.jobs?.find((j) => j.job === job);
      expect(row?.started_at, `11: ${job} ran for this refresh`).toBeTruthy();
      expect(Date.parse(row!.started_at!)).toBeGreaterThanOrEqual(refreshedAt - 1000);
      expect(row!.status).not.toBe("failed");
    }
    const matchesAfter = (await apiGet(page.request, `/api/searches/${search.id}/matches`)) as Array<{ id: string; event_id: string }>;
    expect(matchesAfter.some((m) => m.event_id === event!.id), "11: the search's match follows the re-evaluated event").toBe(true);
    const notificationsAfter = ((await apiGet(page.request, "/api/notifications")) as { total: number }).total;
    expect(notificationsAfter).toBeGreaterThanOrEqual(notificationsBefore);

    // --- step 12: the same refresh again publishes nothing and alerts nobody twice --------
    const r2 = await refresh(request);
    console.log(`refresh 2: ${JSON.stringify({ status: r2.status, version_ids: r2.version_ids })}`);
    expect(r2.status, "12: nothing is due").toBe("not_due");
    expect(r2.version_ids).toEqual([]);
    expect((await currentVersion(request)).version_id).toBe(v2.version_id);
    const notifications = (await apiGet(page.request, "/api/notifications?limit=200")) as NotificationsOut;
    expect(notifications.total, "12: no duplicate alert").toBe(notificationsAfter);
    // Across the first alert, the refresh and its repeat, the user was never told the same
    // thing twice: one notification per match, channel, kind and content (the dedup rule).
    const told = notifications.items.map((n) => JSON.stringify([n.match_id, n.channel, n.kind, n.peak_label, n.window_label, n.price]));
    expect(told.length, "12: every notification listed").toBe(notifications.total);
    expect(new Set(told).size, `12: no alert repeated (${told.join(" | ")})`).toBe(told.length);
    expect(((await apiGet(page.request, `/api/searches/${search.id}/matches`)) as unknown[]).length).toBe(matchesAfter.length);
    // The pages stay on the version (no reload, no second update).
    await spotPage.waitForTimeout(POLL_MS * 2);
    await expect(page.getByTestId("forecast-updated")).toHaveAttribute("data-version-id", v2id);
    expect(await liveMarker(page)).toBe(7);
    await spotPage.close();
  });
});
