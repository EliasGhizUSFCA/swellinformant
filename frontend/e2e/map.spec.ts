import { expect, test, type Page } from "@playwright/test";

import { apiGet, apiPost, verifyEmailFromOutbox } from "./helpers";
import { syntheticValidTimes } from "./map-fields.mock";
import { field, openMap, openMapWithFields, REAL_TILES, requested, screenPoint, shot, type Snapshot, type Spot } from "./map-helpers";

/**
 * Interactive map against the production build: markers, the spot panel, filters, the
 * forecast timeline, locate and the phone layout. The ocean layers themselves (colour field,
 * particles, readout, legend) are in map-layers.spec.ts; shared helpers in map-helpers.ts.
 *
 * Timeline tests use route-mocked SYNTHETIC fields from ./map-fields.mock.ts (made-up
 * patterns, not a forecast); the backend in this suite has no live NOAA data.
 */

const visibleCount = async (page: Page) => Number(/Showing (\d+) of/.exec(await page.getByTestId("visible-count").innerText())![1]);

/** "8am Sun PDT", the way the timeline bubble writes a time in `tz`. */
function bubble(iso: string, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", hour: "numeric", minute: "2-digit", hour12: true, timeZoneName: "short" }).formatToParts(new Date(iso));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const minute = get("minute") !== "00" ? `:${get("minute")}` : "";
  return `${get("hour")}${minute}${get("dayPeriod").toLowerCase()} ${get("weekday")} ${get("timeZoneName")}`;
}

test.describe("interactive map", () => {
  test("loads with 50 markers, working tiles and no uncaught errors", async ({ page }) => {
    const { errors, tiles } = await openMap(page);
    // Default: the dark Natural Earth basemap drawn on a canvas (no tile requests).
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "1");
    await expect(page.locator(".leaflet-control-attribution")).toContainText("Natural Earth");
    expect(tiles).toEqual([]);
    // Without live NOAA data the backend's index says so; the map still works.
    await expect(page.getByTestId("fields-notice")).toBeVisible();
    // "Streets" in Settings switches to the OpenStreetMap tiles.
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("radio", { name: "Streets" }).click();
    await page.keyboard.press("Escape");
    await expect.poll(async () => page.locator("img.leaflet-tile-loaded").count()).toBeGreaterThan(0);
    expect(tiles.length).toBeGreaterThan(0);
    expect(tiles.every((u) => u.startsWith("https://tile.openstreetmap.org/"))).toBe(true);
    await expect(page.locator(".leaflet-control-attribution")).toContainText("OpenStreetMap");
    await expect(page.locator("canvas.map-basemap-canvas")).toHaveAttribute("data-land", "0");
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("map-legend")).toContainText("No usable forecast");
    await expect(page.getByTestId("visible-count")).toHaveText(/Showing 50 of 50 spots/);
    // zoom and pan
    await page.locator(".leaflet-control-zoom-in").click();
    await page.mouse.move(400, 600);
    await page.mouse.down();
    await page.mouse.move(520, 640, { steps: 5 });
    await page.mouse.up();
    expect(errors).toEqual([]);
    if (REAL_TILES) {
      const ok = await page.evaluate(() => [...document.querySelectorAll<HTMLImageElement>("img.leaflet-tile")].every((t) => t.naturalWidth > 0));
      expect(ok, "real OpenStreetMap tiles decoded").toBe(true);
    }
  });

  test("clicking a marker opens that spot's raw model data and our prediction", async ({ page, request }) => {
    await openMap(page);
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const jbay = snap.spots.find((s) => s.slug === "jeffreys-bay")!;
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    const panel = page.getByTestId("map-spot-panel");
    await expect(panel.getByTestId("panel-spot-name")).toHaveText("Jeffreys Bay (Supertubes)");
    await expect(panel.getByTestId("panel-country")).toHaveText("South Africa");
    if (jbay.conditions) {
      await expect(panel.getByTestId("raw-model")).toContainText("Significant wave height (offshore)");
      await expect(panel.getByTestId("raw-model")).toContainText("Main component direction");
      await expect(panel.getByTestId("our-prediction")).toContainText("est. breaking faces");
      await expect(panel.getByTestId("our-prediction")).toContainText(String(jbay.conditions.score));
    } else {
      await expect(panel.getByTestId("no-forecast")).toBeVisible();
    }
    await expect(panel.getByText("Upcoming swell events")).toBeVisible();
    await expect(panel.getByTestId("opps-signed-out")).toBeVisible(); // signed out: no private data
    await expect(panel.getByTestId("forecast-confidence-slot")).toBeVisible();
    // Research confidence is its own per-spot line; the demo backend has no evidence loaded
    // in e2e (reset database + seed), so it reads whatever the API says, never a default.
    const research = panel.getByTestId("panel-research-confidence");
    await expect(research).toContainText("Spot research confidence");
    await panel.getByRole("button", { name: "Close spot details" }).click();
    await expect(panel).toHaveCount(0);
  });

  test("the forecast time control requests cached predictions for the chosen hour", async ({ page, request }) => {
    await openMap(page);
    const before = await page.getByTestId("time-label").innerText();
    // Eight steps after the server's "now" hour. The request is debounced (250 ms), so clicks
    // slower than that may also ask for earlier hours: wait for the final hour's request.
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const final = Date.parse(snap.valid_time!) + 8 * snap.step_hours * 3600_000;
    const req = page.waitForRequest((r) => {
      const at = new URL(r.url()).searchParams.get("at");
      return r.url().includes("/api/map/conditions?at=") && at !== null && Date.parse(at) === final;
    });
    for (let i = 0; i < 8; i++) await page.getByRole("button", { name: "Later" }).click();
    const url = new URL((await req).url());
    const at = new Date(url.searchParams.get("at")!);
    expect(at.getTime() - Date.now()).toBeGreaterThan(20 * 3600_000); // ≥ ~1 day ahead
    await expect(page.getByTestId("time-label")).not.toHaveText(before);
    await page.locator("path.spot-nazare").click({ force: true });
    const shown = await page.getByTestId("forecast-breakdown").innerText();
    expect(shown).toMatch(/local/);
    await page.getByRole("button", { name: "Now", exact: true }).click();
    await expect(page.getByTestId("time-label")).toHaveText(before);
  });

  test("filters narrow the visible spots consistently with the data", async ({ page, request }) => {
    await openMap(page);
    const spots = (await apiGet(request, "/api/spots")) as Spot[];
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const bySlug = new Map(snap.spots.map((s) => [s.slug, s]));

    // The filters live in the Settings popover, with the matching count.
    await page.getByRole("button", { name: "Settings" }).click();
    const settings = page.getByTestId("map-settings");
    await expect(settings.getByTestId("filter-count")).toHaveText("50 of 50 spots match");

    await settings.getByRole("button", { name: "Hawaii", exact: true }).click();
    const hawaii = spots.filter((s) => s.region_group === "Hawaii").length;
    await expect.poll(() => visibleCount(page)).toBe(hawaii);
    await expect(page.locator("path.spot-marker")).toHaveCount(hawaii);
    await expect(settings.getByTestId("filter-count")).toHaveText(`${hawaii} of 50 spots match`);

    await settings.getByRole("button", { name: "All", exact: true }).click();
    await page.getByLabel("Break type").selectOption("reef");
    await expect.poll(() => visibleCount(page)).toBe(spots.filter((s) => s.break_type === "reef").length);

    await page.getByLabel("Break type").selectOption("any");
    await page.getByLabel("Country").selectOption("Indonesia");
    await expect.poll(() => visibleCount(page)).toBe(spots.filter((s) => s.country === "Indonesia").length);

    await page.getByLabel("Country").selectOption("All");
    await page.getByLabel("Minimum quality").selectOption("good");
    const good = snap.spots.filter((s) => s.conditions && s.conditions.score >= 57).length;
    await expect.poll(() => visibleCount(page)).toBe(good);

    await page.getByLabel("Minimum quality").selectOption("");
    await page.getByLabel("Minimum breaking wave height").fill("6");
    const tall = [...bySlug.values()].filter((s) => s.conditions && (s.conditions.breaking_height_min_ft + s.conditions.breaking_height_max_ft) / 2 >= 6).length;
    await expect.poll(() => visibleCount(page)).toBe(tall);

    await page.getByLabel("Minimum breaking wave height").fill("");
    await page.getByLabel("Swell event at this time").check();
    await expect.poll(() => visibleCount(page)).toBe(snap.spots.filter((s) => s.event_at_time).length);

    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect.poll(() => visibleCount(page)).toBe(50);
  });

  test("still works after a reload", async ({ page }) => {
    const { errors } = await openMap(page);
    await page.reload();
    await expect(page.locator("path.spot-marker")).toHaveCount(50);
    await page.locator("path.spot-uluwatu").click({ force: true });
    await expect(page.getByTestId("panel-spot-name")).toHaveText(/Uluwatu/);
    expect(errors).toEqual([]);
  });

  test("the best-spots list ranks daylight hours above night ones and tags the night", async ({ page, request }) => {
    await openMap(page);
    const spots = (await apiGet(request, "/api/spots")) as Spot[];
    const snap = (await apiGet(request, "/api/map/conditions")) as Snapshot;
    const cond = new Map(snap.spots.map((s) => [s.slug, s.conditions]));
    const rank = (slug: string) => {
      const c = cond.get(slug);
      return c ? (c.is_daylight ? 1000 : 0) + c.score : -1;
    };
    // The same ordering as before the redesign: daylight first, then score; 15 rows.
    const expected = [...spots].sort((a, b) => rank(b.slug) - rank(a.slug)).slice(0, 15);
    const rows = page.getByTestId("top-spots").locator("li");
    await expect(rows).toHaveCount(15);
    for (const [i, s] of expected.entries()) {
      await expect(rows.nth(i)).toContainText(`${s.name} · ${s.country}`);
      const c = cond.get(s.slug);
      await expect(rows.nth(i).getByTestId("night-tag")).toHaveCount(c && !c.is_daylight ? 1 : 0);
    }
    // A row opens that spot's panel.
    await rows.first().getByRole("button").click();
    await expect(page.getByTestId("panel-spot-name")).toHaveText(expected[0]!.name);
  });

  test("signed-in users see their own opportunities in the spot panel", async ({ page }) => {
    const email = `map-${Date.now()}@example.com`;
    await page.goto("/register");
    await page.fill("#full_name", "Map Tester");
    await page.fill("#email", email);
    await page.fill("#password", "Map-tester-pass-1!");
    await page.fill("#confirm_password", "Map-tester-pass-1!");
    await page.getByRole("button", { name: "Create account" }).click();
    await page.waitForURL(/dashboard/);
    await verifyEmailFromOutbox(page, email);
    await apiPost(page, "/api/searches", {
      name: "Map test",
      date_mode: "flexible",
      horizon_days: 30,
      units: "ft",
      wave_min: 3,
      wave_max: 20,
      min_quality: "fair",
      destination_mode: "spots",
      destinations: { spot_slugs: ["jeffreys-bay"] },
      origins: ["SFO"],
      notification_channel: "dashboard",
      priority: "balanced",
      min_window_hours: 3,
      travel: { max_price: 5000, currency: "USD", max_layovers: 2, max_flight_hours: 45, min_days_at_destination: 3, max_trip_days: 14, travelers: 1, arrival_buffer_days: 2, departure_buffer_days: 1 },
    });
    await apiPost(page, "/api/dev/simulate-swell", { spot_slug: "jeffreys-bay", days_ahead: 7, duration_hours: 48, target_breaking_ft: 8, period_s: 16 });
    await openMap(page);
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    const panel = page.getByTestId("map-spot-panel");
    await expect(panel.getByText("Your travel opportunities")).toBeVisible();
    await expect(panel.getByTestId("opps-signed-out")).toHaveCount(0);
    await expect(panel.getByRole("link", { name: /\$|Surf only/ }).first()).toBeVisible();
  });
});

test.describe("ocean layers (synthetic fields)", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test.describe("in Los Angeles time", () => {
    test.use({ timezoneId: "America/Los_Angeles" });

    test("the timeline drives the ocean layers and the spot markers", async ({ page }) => {
      const { mock, nowIndex, now } = await openMapWithFields(page);
      const tz = "America/Los_Angeles";
      const slider = page.getByRole("slider", { name: "Forecast time" });
      const label = page.getByTestId("time-label");
      await expect(label).toHaveText(bubble(now, tz));
      await expect(slider).toHaveAttribute("aria-valuenow", String(nowIndex));
      await expect(slider).toHaveAttribute("aria-valuemax", String(mock.validTimes.length - 1));
      // day labels in local time under the ticks. The first and last days can be partial (a few
      // hours) and are labelled only when wide enough, so check a day in the middle of the range,
      // which is always whole: the test then holds at every time of day.
      const weekday = (iso: string) => new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short" }).format(new Date(iso));
      const middle = mock.validTimes[Math.floor(mock.validTimes.length / 2)]!;
      await expect(page.getByTestId("time-control")).toContainText(weekday(middle));
      await shot(page, "desktop-1440x900-synthetic-timeline-los-angeles");

      // Arrow key: one step later → new ?at= for the layer fields and the spot snapshot.
      const next = mock.validTimes[nowIndex + 1]!;
      const snapReq = page.waitForRequest((r) => r.url().includes(`/api/map/conditions?at=${encodeURIComponent(next)}`));
      await slider.focus();
      await page.keyboard.press("ArrowRight");
      await expect(label).toHaveText(bubble(next, tz));
      await snapReq;
      await expect.poll(() => requested(mock, "hs", next)).toBe(true);
      await expect(field(page)).toHaveAttribute("data-at", next);
      // neighbouring steps are prefetched
      await expect.poll(() => requested(mock, "hs", mock.validTimes[nowIndex + 2]!)).toBe(true);

      await page.keyboard.press("End");
      await expect(slider).toHaveAttribute("aria-valuenow", String(mock.validTimes.length - 1));
      await expect(label).toHaveText(bubble(mock.validTimes.at(-1)!, tz));
      await page.keyboard.press("Home");
      await expect(slider).toHaveAttribute("aria-valuenow", "0");

      // Click on the track at ~75 %.
      const box = (await slider.boundingBox())!;
      await page.mouse.click(box.x + box.width * 0.75, box.y + box.height / 2);
      const clicked = Number(await slider.getAttribute("aria-valuenow"));
      expect(Math.abs(clicked - Math.round(0.75 * (mock.validTimes.length - 1)))).toBeLessThanOrEqual(1);
      await expect(label).toHaveText(bubble(mock.validTimes[clicked]!, tz));
      await expect.poll(() => requested(mock, "hs", mock.validTimes[clicked]!)).toBe(true);

      // Drag the thumb back towards the start.
      await page.mouse.move(box.x + box.width * 0.75, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width * 0.25, box.y + box.height / 2, { steps: 6 });
      await page.mouse.up();
      const dragged = Number(await slider.getAttribute("aria-valuenow"));
      expect(Math.abs(dragged - Math.round(0.25 * (mock.validTimes.length - 1)))).toBeLessThanOrEqual(1);

      // "Now" snaps back to the server's current step.
      await page.getByRole("button", { name: "Now", exact: true }).click();
      await expect(slider).toHaveAttribute("aria-valuenow", String(nowIndex));
      await expect(label).toHaveText(bubble(now, tz));
    });
  });

  test("play advances through the forecast and stops at the end", async ({ page }) => {
    // five steps starting at the server's current 3-hour step
    const times = syntheticValidTimes(Date.now(), 7).slice(2);
    const { nowIndex } = await openMapWithFields(page, { validTimes: times });
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await expect(slider).toHaveAttribute("aria-valuenow", String(nowIndex));
    await page.getByRole("button", { name: "Play forecast" }).click();
    await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
    await expect.poll(async () => Number(await slider.getAttribute("aria-valuenow"))).toBeGreaterThan(nowIndex);
    await expect(slider).toHaveAttribute("aria-valuenow", String(times.length - 1), { timeout: 20_000 });
    // stopped at the last step
    await expect(page.getByRole("button", { name: "Play forecast" })).toBeDisabled();
    // pause works mid-way
    await slider.focus();
    await page.keyboard.press("Home");
    await page.getByRole("button", { name: "Play forecast" }).click();
    await expect(slider).toHaveAttribute("aria-valuenow", "1", { timeout: 10_000 });
    await page.getByRole("button", { name: "Pause" }).click();
    await page.waitForTimeout(1500);
    await expect(slider).toHaveAttribute("aria-valuenow", "1");
  });
  test("the locate button flies to the user's position", async ({ page, context, baseURL }) => {
    await context.grantPermissions(["geolocation"], { origin: baseURL });
    await context.setGeolocation({ latitude: -33.92, longitude: 18.42 });
    await openMapWithFields(page);
    await page.getByRole("button", { name: "Show my location" }).click();
    const me = page.locator("path.user-location-marker");
    await expect(me).toBeVisible();
    const map = (await page.locator(".leaflet-container").boundingBox())!;
    await expect
      .poll(async () => {
        const b = (await me.boundingBox())!;
        return Math.hypot(b.x + b.width / 2 - (map.x + map.width / 2), b.y + b.height / 2 - (map.y + map.height / 2));
      }, { message: "map centred on the user", timeout: 10_000 })
      .toBeLessThan(30);
  });

  test("a denied location shows an error and leaves the map as it was", async ({ page }) => {
    await page.addInitScript(() => {
      const denied = { code: 1, PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3, message: "denied" };
      Object.defineProperty(navigator, "geolocation", {
        value: { getCurrentPosition: (_ok: unknown, fail: (e: unknown) => void) => setTimeout(() => fail(denied), 50), watchPosition: () => 0, clearWatch: () => undefined },
      });
    });
    await openMap(page);
    await page.getByRole("button", { name: "Show my location" }).click();
    await expect(page.getByText(/Location access was denied/)).toBeVisible();
    await expect(page.locator("path.user-location-marker")).toHaveCount(0);
  });
});

test.describe("map on a phone (390 × 844)", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });
  test("tabs scroll, the spot panel is a bottom sheet and the timeline works", { tag: "@mobile" }, async ({ page }) => {
    const { mock, nowIndex } = await openMapWithFields(page);
    // Best spots start collapsed on small screens.
    const listButton = page.getByRole("button", { name: /Best spots now/ });
    await expect(listButton).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("top-spots")).toHaveCount(0);
    await shot(page, "mobile-390x844-synthetic-wave-height");

    // The layer row scrolls sideways; every tab and the pressure toggle stay reachable.
    const tabs = page.getByTestId("layer-tabs");
    expect(await tabs.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    await page.getByRole("radio", { name: "Wave Period" }).click();
    await expect(field(page)).toHaveAttribute("data-layer", "tp");
    await page.getByRole("button", { name: "Pressure" }).click();
    await expect(page.getByRole("button", { name: "Pressure" })).toHaveAttribute("aria-pressed", "true");

    // A tap on the ocean shows the readout for a few seconds.
    const sea = await screenPoint(page, -30, -20);
    await page.touchscreen.tap(sea.x, sea.y);
    await expect(page.getByTestId("map-readout")).toContainText("Offshore peak period");
    await expect(page.getByTestId("map-readout")).toHaveCount(0, { timeout: 8_000 });

    // Timeline: tap on the track.
    const slider = page.getByRole("slider", { name: "Forecast time" });
    const box = (await slider.boundingBox())!;
    await page.touchscreen.tap(box.x + box.width * 0.8, box.y + box.height / 2);
    const i = Number(await slider.getAttribute("aria-valuenow"));
    expect(i).toBeGreaterThan(nowIndex);
    await expect.poll(() => requested(mock, "tp", mock.validTimes[i]!)).toBe(true);

    // The list opens on demand.
    await listButton.click();
    await expect(page.getByTestId("top-spots")).toBeVisible();
    await shot(page, "mobile-390x844-synthetic-best-spots");
    await listButton.click();

    // Spot panel: a bottom sheet over the timeline.
    await page.locator("path.spot-jeffreys-bay").click({ force: true });
    const panel = page.getByTestId("map-spot-panel");
    await expect(panel.getByTestId("panel-spot-name")).toHaveText("Jeffreys Bay (Supertubes)");
    const sheet = (await panel.boundingBox())!;
    expect(sheet.y + sheet.height).toBeGreaterThan(844 - 12);
    expect(sheet.width).toBeGreaterThan(370);
    await expect(panel.getByTestId("our-prediction").or(panel.getByTestId("no-forecast"))).toBeVisible();
    await shot(page, "mobile-390x844-synthetic-spot-sheet", 1500);
    await panel.getByRole("button", { name: "Close spot details" }).click();
    await expect(panel).toHaveCount(0);
    await expect(slider).toBeVisible();

    // Settings and legend fit the screen.
    await page.getByRole("button", { name: "Settings" }).click();
    const settings = (await page.getByTestId("map-settings").boundingBox())!;
    expect(settings.x).toBeGreaterThanOrEqual(0);
    expect(settings.x + settings.width).toBeLessThanOrEqual(390);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    await expect(page.getByTestId("legend-drawer")).toBeVisible();
    // The open drawer leaves the zoom buttons at the right edge uncovered and usable.
    const drawer = (await page.getByTestId("legend-drawer").boundingBox())!;
    const zoom = (await page.locator(".leaflet-control-zoom").boundingBox())!;
    expect(drawer.x + drawer.width).toBeLessThan(zoom.x);
    await page.locator(".leaflet-control-zoom-in").click({ timeout: 5_000 });
    await shot(page, "mobile-390x844-synthetic-legend");
  });
});
