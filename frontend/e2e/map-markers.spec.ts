import { expect, test, type Page } from "@playwright/test";

import { apiGet } from "./helpers";
import { mapSettled, markerCentre, openMap, setMapView, shot, type Snapshot } from "./map-helpers";

/**
 * Spot markers (spec 23) against the demo backend: each marker's swell and wind arrows point
 * where the swell and wind go (the API's FROM direction + 180, as the spot charts draw them),
 * its label is our predicted surf height, and the arrows and labels make way for each other
 * at world zoom. The markers stay the clickable SVG paths `path.spot-<slug>`.
 */

interface SnapshotWithDirections extends Snapshot {
  spots: Array<Snapshot["spots"][number] & { conditions: null | (NonNullable<Snapshot["spots"][number]["conditions"]> & { swell_direction_deg: number | null; wind_direction_deg: number | null }) }>;
}

const to = (from: number) => (((from + 180) % 360) + 360) % 360;
const glyph = (page: Page, slug: string) => page.locator(`[data-testid="spot-glyph"][data-slug="${slug}"]`);

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}
const overlaps = (a: Box, b: Box) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

async function labelBoxes(page: Page): Promise<Array<{ slug: string; box: Box; text: string }>> {
  return page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>('[data-testid="spot-glyph"] .sg-label')].map((el) => {
      const r = el.getBoundingClientRect();
      return { slug: el.closest<HTMLElement>("[data-slug]")!.dataset.slug!, box: { x: r.x, y: r.y, width: r.width, height: r.height }, text: el.textContent ?? "" };
    }),
  );
}

test.describe("spot marker arrows and labels", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("arrows point where the swell and wind go; labels are the predicted surf height", async ({ page, request }) => {
    const { errors } = await openMap(page);
    const snap = (await apiGet(request, "/api/map/conditions")) as SnapshotWithDirections;
    const withData = snap.spots.filter((s) => s.conditions && s.conditions.swell_direction_deg != null && s.conditions.wind_direction_deg != null);
    expect(withData.length, "the demo snapshot has directions").toBeGreaterThan(10);

    // California and Mexico at a regional zoom: several isolated markers with arrows and labels.
    await setMapView(page, 28, -112, 5);
    await mapSettled(page);
    let checked = 0;
    for (const s of withData) {
      const g = glyph(page, s.slug);
      if ((await g.count()) === 0 || !(await g.getAttribute("data-swell-rot"))) continue;
      const c = s.conditions!;
      expect(Number(await g.getAttribute("data-swell-rot")), `${s.slug} swell arrow`).toBeCloseTo(to(c.swell_direction_deg!), 1);
      expect(Number(await g.getAttribute("data-wind-rot")), `${s.slug} wind arrow`).toBeCloseTo(to(c.wind_direction_deg!), 1);
      checked += 1;
    }
    expect(checked, "markers with arrows at regional zoom").toBeGreaterThan(3);

    // Each label says what the marker's tooltip says: our predicted surf for that hour.
    const labels = await labelBoxes(page);
    expect(labels.length).toBeGreaterThan(3);
    const names = new Map(((await apiGet(request, "/api/spots")) as Array<{ slug: string; name: string }>).map((s) => [s.slug, s.name]));
    for (const l of labels.slice(0, 4)) {
      await page.locator(`path.spot-${l.slug}`).focus();
      await expect(page.locator(".leaflet-tooltip").filter({ hasText: names.get(l.slug)! })).toContainText(`${l.text} surf (our prediction)`);
    }
    await shot(page, "markers-regional-desktop");
    expect(errors).toEqual([]);
  });

  test("at world zoom labels stay out and arrows only show where markers have room", async ({ page }) => {
    await openMap(page);
    await mapSettled(page);
    // The one-world view (zoom about 2.5): no labels at all.
    await expect(page.locator('[data-testid="spot-glyph"] .sg-label')).toHaveCount(0);
    // No two drawn arrow glyphs belong to markers closer than the clearance.
    const centres = await page.evaluate(() =>
      [...document.querySelectorAll<HTMLElement>('[data-testid="spot-glyph"][data-swell-rot]')].map((el) => {
        const r = el.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      }),
    );
    expect(centres.length).toBeGreaterThan(5);
    const all = await page.evaluate(() =>
      [...document.querySelectorAll<SVGPathElement>("path.spot-marker")].map((el) => {
        const r = el.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      }),
    );
    for (const c of centres) {
      const close = all.filter((m) => Math.hypot(m.x - c.x, m.y - c.y) < 20);
      expect(close.length, "an arrowed marker has no neighbour within 20 px").toBe(1);
    }
    await shot(page, "markers-world-desktop");
  });

  test("labels never overlap each other at regional zoom, and the arrows never block a click", async ({ page }) => {
    await openMap(page);
    for (const [lat, lng, z] of [
      [42, -6, 4.6], // Portugal, Spain, France, Ireland, Morocco
      [-12, 118, 4.4], // Bali, Java, Sumatra, Western Australia
      [10, -85, 5], // Central America
    ] as const) {
      await setMapView(page, lat, lng, z);
      await mapSettled(page);
      const labels = await labelBoxes(page);
      for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) expect(overlaps(labels[i]!.box, labels[j]!.box), `${labels[i]!.slug} / ${labels[j]!.slug}`).toBe(false);
    }
    // A click on a marker centre, under its arrows and label, opens that spot.
    await setMapView(page, -34, 25, 7);
    await mapSettled(page);
    await expect(glyph(page, "jeffreys-bay")).toHaveCount(1);
    const c = await markerCentre(page, "jeffreys-bay");
    await page.mouse.click(c.x, c.y);
    await expect(page.getByTestId("panel-spot-name")).toHaveText(/Jeffreys Bay/);
  });

  test("the legend explains the arrows, the label and the stale ring", async ({ page }) => {
    await openMap(page);
    await page.getByRole("button", { name: "Legend", exact: true }).click();
    const key = page.getByTestId("legend-marker-glyphs");
    await expect(key).toContainText("The swell that drives the surf, pointing where it travels");
    await expect(key).toContainText("The wind, pointing where it blows");
    await expect(key).toContainText("Our predicted surf height");
    await expect(key).toContainText("Forecast out of date at this spot");
  });
});

test.describe("spot marker labels on a phone", () => {
  test("regional zoom shows labels that do not overlap", { tag: "@mobile" }, async ({ page }) => {
    await openMap(page);
    await setMapView(page, 34, -119, 6);
    await mapSettled(page);
    const labels = await labelBoxes(page);
    expect(labels.length).toBeGreaterThan(0);
    for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) expect(overlaps(labels[i]!.box, labels[j]!.box)).toBe(false);
    await shot(page, "markers-regional-phone");
  });
});
