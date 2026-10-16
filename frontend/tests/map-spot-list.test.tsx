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
