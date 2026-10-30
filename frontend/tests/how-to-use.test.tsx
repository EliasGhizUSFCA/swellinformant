import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LAYERS } from "@/lib/map-layers";
import { MATCH_STATUS, QUALITY_THRESHOLDS, WIND_RELATION_LABEL } from "@/lib/quality";
import { WIZARD_STEPS } from "@/lib/validation";

// The "How to use" tab (app/how-to-use) and its nav entry (components/layout/app-shell.tsx).
// SYNTHETIC test data: an invented account; nothing here describes a real user.

const state = vi.hoisted(() => ({ pathname: "/map", user: undefined as unknown }));

vi.mock("next/navigation", () => ({
  usePathname: () => state.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/hooks/queries", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/queries")>()),
  useMe: () => ({ data: state.user, isLoading: false, error: null }),
  useSystemStatus: () => ({ data: undefined, isLoading: false, error: null }),
}));

const { AppShell, isActivePath } = await import("@/components/layout/app-shell");
const { SiteFooter } = await import("@/components/layout/site-footer");
const { HOW_TO_USE_SECTIONS, HowToUseGuide } = await import("@/app/how-to-use/guide");
const howToUsePage = await import("@/app/how-to-use/page");

const SIGNED_IN = { full_name: "Fixture Surfer", email: "fixture-surfer@example.com" };

function renderShell({ pathname = "/map", user }: { pathname?: string; user?: unknown } = {}) {
  state.pathname = pathname;
  state.user = user;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AppShell>
        <p>page body</p>
      </AppShell>
    </QueryClientProvider>,
  );
}

const mainNav = () => screen.getByRole("navigation", { name: "Main" });
const navLabels = (nav: HTMLElement) => within(nav).getAllByRole("link").map((a) => a.textContent?.trim());

afterEach(cleanup);

describe("the How to use nav entry", () => {
  it("is in the main nav for signed-out visitors, after the public pages", () => {
    renderShell();
    const link = within(mainNav()).getByRole("link", { name: "How to use" });
    expect(link).toHaveAttribute("href", "/how-to-use");
    expect(navLabels(mainNav())).toEqual(["Surf map", "Spots", "How to use"]);
  });

  it("is in the main nav for signed-in users, last of the six entries", () => {
    renderShell({ user: SIGNED_IN });
    expect(navLabels(mainNav())).toEqual(["Dashboard", "Searches", "Surf map", "Spots", "Alerts", "How to use"]);
    expect(within(mainNav()).getByRole("link", { name: "How to use" })).toHaveAttribute("href", "/how-to-use");
  });

  it("signed in, the entries are icons below lg, each still named by its label and a tooltip", () => {
    renderShell({ user: SIGNED_IN });
    const link = within(mainNav()).getByRole("link", { name: "How to use" });
    expect(link).toHaveAttribute("title", "How to use");
    // Labels from lg (1024 px), where the six labelled entries fit (e2e/how-to-use.spec.ts measures it).
    expect(within(link).getByText("How to use")).toHaveClass("sr-only", "lg:not-sr-only", "lg:whitespace-nowrap");
    // Never wraps onto two lines (the labels did at 768-900 px before).
    expect(link).toHaveClass("whitespace-nowrap");
  });

  it("signed out, the three entries keep visible labels", () => {
    renderShell();
    const link = within(mainNav()).getByRole("link", { name: "How to use" });
    expect(link).not.toHaveAttribute("title");
    expect(within(link).getByText("How to use")).not.toHaveClass("sr-only");
  });

  it("is the current page on /how-to-use, and only there", () => {
    renderShell({ pathname: "/how-to-use", user: SIGNED_IN });
    const current = within(mainNav())
      .getAllByRole("link")
      .filter((a) => a.getAttribute("aria-current") === "page");
    expect(current.map((a) => a.textContent?.trim())).toEqual(["How to use"]);
    expect(within(mainNav()).getByRole("link", { name: "How to use" })).toHaveClass("bg-secondary");
    cleanup();
    renderShell({ pathname: "/map" });
    expect(within(mainNav()).getByRole("link", { name: "How to use" })).not.toHaveAttribute("aria-current");
    expect(within(mainNav()).getByRole("link", { name: "Surf map" })).toHaveAttribute("aria-current", "page");
  });

  it("is in the mobile menu, signed out and signed in, marked current on its page", () => {
    renderShell({ pathname: "/how-to-use" });
    fireEvent.click(screen.getByRole("button", { name: "Toggle menu" }));
    let mobile = screen.getByRole("navigation", { name: "Mobile" });
    expect(within(mobile).getByRole("link", { name: "How to use" })).toHaveAttribute("aria-current", "page");
    cleanup();
    renderShell({ user: SIGNED_IN });
    fireEvent.click(screen.getByRole("button", { name: "Toggle menu" }));
    mobile = screen.getByRole("navigation", { name: "Mobile" });
    expect(navLabels(mobile)).toEqual(["Dashboard", "Searches", "Surf map", "Spots", "Alerts", "How to use", "Settings"]);
  });

  it("isActivePath matches the page and pages under it, not a longer name", () => {
    expect(isActivePath("/how-to-use", "/how-to-use")).toBe(true);
    expect(isActivePath("/how-to-use/anything", "/how-to-use")).toBe(true);
    expect(isActivePath("/how-to-used", "/how-to-use")).toBe(false);
    expect(isActivePath("/spots/pipeline", "/spots")).toBe(true);
  });

  it("the footer links the guide", () => {
    render(<SiteFooter />);
    expect(screen.getByRole("link", { name: "How to use" })).toHaveAttribute("href", "/how-to-use");
  });
});

describe("the How to use page", () => {
  it("has a metadata title and one h1", () => {
    expect(howToUsePage.metadata.title).toBe("How to use");
    state.pathname = "/how-to-use";
    state.user = undefined;
    const client = new QueryClient();
    render(<QueryClientProvider client={client}>{howToUsePage.default()}</QueryClientProvider>);
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["How to use Swell Travel Agent"]);
  });

  it("has an anchored section with an h2 for every table-of-contents entry, in order", () => {
    const { container } = render(<HowToUseGuide />);
    const ids = HOW_TO_USE_SECTIONS.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
    const toc = screen.getByRole("navigation", { name: "On this page" });
    const links = within(toc).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(ids.map((id) => `#${id}`));
    expect(links.map((a) => a.textContent?.replace(/^\d+/, ""))).toEqual(HOW_TO_USE_SECTIONS.map((s) => s.title));
    const sections = [...container.querySelectorAll("section[id]")];
    expect(sections.map((s) => s.id)).toEqual(ids);
    for (const { id, title } of HOW_TO_USE_SECTIONS) {
      const section = container.querySelector<HTMLElement>(`section#${id}`)!;
      // The section is named by its own h2 (a landmark readers can jump to).
      expect(screen.getByRole("region", { name: title })).toBe(section);
      const h2 = within(section).getByRole("heading", { level: 2 });
      expect(h2).toHaveTextContent(title);
      expect(h2.id).toBe(`${id}-title`);
      // Headings go h2 then h3, never deeper or skipping.
      for (const h of section.querySelectorAll("h1, h4, h5, h6")) throw new Error(`${id}: unexpected ${h.tagName}`);
    }
  });

  it("covers the topics the tab promises", () => {
    render(<HowToUseGuide />);
    expect(HOW_TO_USE_SECTIONS.map((s) => s.title)).toEqual([
      "What the app does",
      "Read the surf map",
      "Read a spot page",
      "Ratings and confidence",
      "How forecasts stay current",
      "Create an account",
      "Save a search",
      "How matching works",
      "Trips and flights",
      "Alerts and emails",
      "Demo mode and test sites",
      "Status page",
      "Tips and questions",
    ]);
    // Forecast and research confidence are explained apart, never as one number.
    const ratings = screen.getByRole("region", { name: "Ratings and confidence" });
    expect(within(ratings).getByText(/never combines them into one number/)).toBeInTheDocument();
    // The demo bar is quoted as the app shows it (components/layout/app-shell.tsx DemoRibbon).
    const demo = screen.getByRole("region", { name: "Demo mode and test sites" });
    expect(within(demo).getByText("Demo mode — forecasts are synthetic · flight fares are mock data. Nothing here is a real booking.")).toBeInTheDocument();
  });

  it("reads the quality classes from lib/quality, with their x/10 lower bounds", () => {
    render(<HowToUseGuide />);
    const rows = within(screen.getByTestId("howto-quality-table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(QUALITY_THRESHOLDS.length);
    expect(rows.map((r) => within(r).getAllByRole("cell")[0]!.textContent)).toEqual([
      "9.5/10 and up",
      "8.5/10 and up",
      "7.2/10 and up",
      "5.7/10 and up",
      "4.0/10 and up",
      "below 4.0/10",
    ]);
    expect(rows.map((r) => within(r).getByRole("rowheader").textContent)).toEqual(["Exceptional", "Excellent", "Very Good", "Good", "Fair", "Poor"]);
  });

  it("names the wizard's steps, the map's layers and the wind classes as the app does", () => {
    render(<HowToUseGuide />);
    const text = (name: string) => screen.getByRole("region", { name }).textContent ?? "";
    // A renamed step, layer or class fails here: the guide is updated with it.
    for (const s of WIZARD_STEPS) expect(text("Save a search")).toContain(`${s.title}:`);
    for (const l of LAYERS) expect(text("Read the surf map")).toContain(l.label);
    for (const label of Object.values(WIND_RELATION_LABEL).filter((l) => l !== "Unknown")) expect(text("Ratings and confidence")).toContain(label);
    for (const { label } of Object.values(MATCH_STATUS)) expect(text("How matching works")).toContain(label);
  });

  it("links only to pages that exist in the app", () => {
    const { container } = render(<HowToUseGuide />);
    const pages = new Set([...container.querySelectorAll("a[href^='/']")].map((a) => a.getAttribute("href")));
    expect([...pages].sort()).toEqual(["/dashboard", "/login", "/map", "/notifications", "/searches", "/settings", "/spots", "/status"]);
  });
});
