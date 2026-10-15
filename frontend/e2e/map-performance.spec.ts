import { expect, test, type Page } from "@playwright/test";

import { field, openMapWithFields, requested } from "./map-helpers";

/**
 * The map's frame costs (colour field, wave crests, wind particles, basemap) and field-cache
 * memory against the production build, with route-mocked SYNTHETIC fields from
 * ./map-fields.mock.ts (made-up patterns, not a forecast). Moved here from map-layers.spec.ts
 * so that it can run without the Playwright trace recorder: its DOM snapshots and screenshots
 * run on the page's main thread and add to every frame (measured on the same view and
 * synthetic fields: crest draw time p95 4.5 ms without a recorder, 6.7 ms with one). `trace`
 * can only be set per file. The crest frame cost is measured once the layer has settled (see
 * below). Every threshold is unchanged.
 */
test.use({ viewport: { width: 1440, height: 900 }, trace: "off" });

const wave = (page: Page) => page.locator("canvas.map-wave-canvas");

test.describe("map frame cost (synthetic fields)", () => {
  test("performance: colour-field redraw, particle frame cost and field-cache memory", async ({ page }) => {
    const { mock } = await openMapWithFields(page);
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Performance.enable");
    const metrics = async () => {
      const { metrics: m } = (await cdp.send("Performance.getMetrics")) as { metrics: Array<{ name: string; value: number }> };
      return Object.fromEntries(m.map((x) => [x.name, x.value]));
    };
    /** Main-thread script and task time per animation frame over `ms`. */
    const perFrame = async (ms: number) => {
      const m0 = await metrics();
      const frames = await page.evaluate(
        (dur) =>
          new Promise<number>((resolve) => {
            let n = 0;
            const t0 = performance.now();
            const tick = () => {
              n++;
              if (performance.now() - t0 < dur) requestAnimationFrame(tick);
              else resolve(n);
            };
            requestAnimationFrame(tick);
          }),
        ms,
      );
      const m1 = await metrics();
      return { frames, scriptMs: ((m1.ScriptDuration! - m0.ScriptDuration!) * 1000) / frames, taskMs: ((m1.TaskDuration! - m0.TaskDuration!) * 1000) / frames };
    };
    // Wave crests (the default Wave Height layer), then the wind particles, then nothing.
    await expect(wave(page)).toHaveAttribute("data-mode", "animated");
    // Steady state: the p95 is over the last 120 frames, and the first frames after the layer
    // opens trace every crest afresh (one-off: on the production build here the window's p95
    // read 17.9 ms one second after opening and about 4 ms from five seconds on), so wait
    // until they have left that window before measuring.
    await expect.poll(async () => Number(await wave(page).getAttribute("data-frames")), { timeout: 30_000 }).toBeGreaterThan(160);
    const size = await wave(page).evaluate((c: HTMLCanvasElement) => `${c.width}x${c.height}`);
    const crests = await perFrame(3000);
    const drawP95 = Number(await wave(page).getAttribute("data-draw-ms-p95"));
    const vertices = Number(await wave(page).getAttribute("data-vertices"));
    await page.getByRole("radio", { name: "Wind Speed" }).click();
    const particles = page.locator("canvas.map-particle-canvas");
    await expect(particles).toHaveAttribute("data-mode", "animated");
    const animated = await perFrame(3000);
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByTestId("map-settings").getByRole("switch", { name: /Animated flow/ }).click();
    await page.keyboard.press("Escape");
    await expect(particles).toHaveAttribute("data-mode", "off");
    const still = await perFrame(3000);
    const note =
      `wave crests: ${crests.scriptMs.toFixed(2)} ms script / ${crests.taskMs.toFixed(2)} ms task per frame, draw p95 ${drawP95} ms, ${vertices} vertices (canvas ${size}); ` +
      `wind particles: ${animated.scriptMs.toFixed(2)} / ${animated.taskMs.toFixed(2)} ms (${animated.frames} frames); off: ${still.scriptMs.toFixed(2)} / ${still.taskMs.toFixed(2)} ms`;
    console.log(note);
    test.info().annotations.push({ type: "flow frame cost (1440x900)", description: note });
    expect(animated.scriptMs).toBeLessThan(12); // regression guard; one frame at 60 Hz is 16.7 ms
    expect(crests.scriptMs).toBeLessThan(12);
    expect(drawP95, "crest frame draw time p95 (ms)").toBeLessThanOrEqual(8);
    expect(vertices).toBeLessThanOrEqual(20_000);
    // The dark basemap redraw at zoom 2 (no closePath per ring).
    const basemapMs = Number(await page.locator("canvas.map-basemap-canvas").getAttribute("data-draw-ms"));
    console.log(`basemap redraw at zoom 2: ${basemapMs} ms`);
    expect(basemapMs).toBeLessThan(30);
    await page.getByRole("radio", { name: "Wave Height" }).click();

    // Field cache: step through 12 times (36 fields with the prefetch); the LRU keeps 48.
    const heap = async () => (await cdp.send("Runtime.getHeapUsage")) as { usedSize: number; totalSize: number; backingStorageSize?: number };
    await cdp.send("HeapProfiler.collectGarbage");
    const h0 = await heap();
    const slider = page.getByRole("slider", { name: "Forecast time" });
    await slider.focus();
    await page.keyboard.press("Home");
    for (let i = 1; i <= 12; i++) {
      await page.keyboard.press("ArrowRight");
      await expect.poll(() => requested(mock, "hs", mock.validTimes[i]!)).toBe(true);
      await expect(field(page)).toHaveAttribute("data-at", mock.validTimes[i]!);
    }
    await page.waitForTimeout(500);
    await cdp.send("HeapProfiler.collectGarbage");
    const h1 = await heap();
    const mb = (b: number) => (b / 1e6).toFixed(1);
    const grew = h1.usedSize + (h1.backingStorageSize ?? 0) - h0.usedSize - (h0.backingStorageSize ?? 0);
    const memNote = `JS heap ${mb(h0.usedSize)} → ${mb(h1.usedSize)} MB, array buffers ${mb(h0.backingStorageSize ?? 0)} → ${mb(h1.backingStorageSize ?? 0)} MB after ${new Set(mock.requests).size} field downloads (decoded cache: at most 48 × 720 × 361 × 4 B = 49.9 MB)`;
    console.log(memNote);
    test.info().annotations.push({ type: "field cache memory", description: memNote });
    expect(grew / 1e6).toBeLessThan(80);
  });
});
