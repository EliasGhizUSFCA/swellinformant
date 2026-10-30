import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SpotList } from "@/components/map/spot-list";
import type { SpotSummary } from "@/types/api";

// SYNTHETIC spots: names and slugs only, no forecast values.
const SPOTS = [
  { slug: "a", name: "Spot A", country: "Nowhere" },
  { slug: "b", name: "Spot B", country: "Nowhere" },
] as SpotSummary[];

const list = (props: Partial<Parameters<typeof SpotList>[0]> = {}) => (
  <SpotList spots={SPOTS} total={50} conditions={() => undefined} onSelect={() => undefined} open={false} onOpenChange={() => undefined} updating={false} {...props} />
);

describe("best spots header", () => {
  it("says 'now' at the current step and names any other selected time", () => {
    const { rerender } = render(list());
    expect(screen.getByTestId("best-spots-title")).toHaveTextContent(/^Best spots now$/);
    expect(screen.getByRole("region", { name: "Best spots now" })).toBeInTheDocument();
    rerender(list({ timeLabel: "8pm Sat PDT" }));
    expect(screen.getByTestId("best-spots-title")).toHaveTextContent(/^Best spots · 8pm Sat PDT$/);
    expect(screen.getByRole("region", { name: "Best spots · 8pm Sat PDT" })).toBeInTheDocument();
  });

  it("keeps the count's full sentence for screen readers and a short one on screen", () => {
    const { rerender } = render(list());
    const count = screen.getByTestId("visible-count");
    expect(count).toHaveTextContent(/^Showing 2 of 50 spots$/);
    expect(count.querySelector(".sr-only")).toHaveTextContent("Showing");
    rerender(list({ updating: true }));
    expect(screen.getByTestId("visible-count")).toHaveTextContent(/^Showing 2 of 50 spots · updating…$/);
  });
});

describe("out-of-date spots in the list (review r1-frontend-FE-2)", () => {
  // SYNTHETIC conditions and freshness: B rates higher but its forecast is out of date.
  const conditions = (slug: string) =>
    ({ a: { label: "fair", score: 40, is_daylight: true }, b: { label: "good", score: 70, is_daylight: true } })[slug] as never;
  const nowMs = Date.parse("2026-10-29T12:00:00Z");
  const freshness = (slug: string) =>
    slug === "b" ? { versionId: 4, lastUpdatedAt: new Date(nowMs - 9 * 3_600_000).toISOString(), stale: true, reason: "refresh_failed" } : null;

  it("marks a stale spot and lists it after the current ones", () => {
    render(list({ open: true, conditions, freshness }));
    const rows = screen.getAllByRole("button").filter((b) => b.closest("#map-top-spots"));
    expect(rows.map((r) => r.textContent?.slice(0, 6))).toEqual(["Spot A", "Spot B"]);
    const chip = screen.getByTestId("list-stale");
    expect(rows[1]).toContainElement(chip);
    expect(chip).toHaveTextContent("out of date");
    expect(chip.querySelector(".sr-only")).toHaveTextContent(/forecast last updated \d+ hours? ago/);
  });

  it("ranks by score when every forecast is current", () => {
    render(list({ open: true, conditions, freshness: () => null }));
    const rows = screen.getAllByRole("button").filter((b) => b.closest("#map-top-spots"));
    expect(rows.map((r) => r.textContent?.slice(0, 6))).toEqual(["Spot B", "Spot A"]);
    expect(screen.queryByTestId("list-stale")).toBeNull();
  });
});
