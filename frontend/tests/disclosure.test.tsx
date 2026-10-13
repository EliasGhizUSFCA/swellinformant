import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { InfoPopover } from "@/components/info-popover";
import { Disclosure } from "@/components/ui/disclosure";

describe("Disclosure", () => {
  it("is a button that toggles aria-expanded and the controlled region", () => {
    render(<Disclosure summary="More details">Hidden text</Disclosure>);
    const button = screen.getByRole("button", { name: "More details" });
    expect(button.tagName).toBe("BUTTON");
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveAttribute("aria-expanded", "false");
    const region = document.getElementById(button.getAttribute("aria-controls")!);
    expect(region).not.toBeNull();
    expect(region).toHaveAttribute("hidden");
    // Collapsed content stays in the DOM (searchable) but is not shown.
    expect(screen.getByText("Hidden text")).not.toBeVisible();

    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(region).not.toHaveAttribute("hidden");
    expect(screen.getByText("Hidden text")).toBeVisible();

    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Hidden text")).not.toBeVisible();
  });

  it("is reachable by keyboard", () => {
    render(<Disclosure summary="Keyboard">Body</Disclosure>);
    const button = screen.getByRole("button", { name: "Keyboard" });
    act(() => button.focus());
    expect(document.activeElement).toBe(button);
  });

  it("can start open and gives each instance its own region", () => {
    render(
      <>
        <Disclosure summary="One" defaultOpen>
          First
        </Disclosure>
        <Disclosure summary="Two">Second</Disclosure>
      </>,
    );
    const one = screen.getByRole("button", { name: "One" });
    const two = screen.getByRole("button", { name: "Two" });
    expect(one).toHaveAttribute("aria-expanded", "true");
    expect(one.getAttribute("aria-controls")).not.toBe(two.getAttribute("aria-controls"));
    expect(screen.getByText("First")).toBeVisible();
  });

  it("mounts lazy content on first open and keeps it mounted", () => {
    render(
      <Disclosure summary="Chart" lazy>
        <span>Heavy chart</span>
      </Disclosure>,
    );
    expect(screen.queryByText("Heavy chart")).toBeNull();
    const button = screen.getByRole("button", { name: "Chart" });
    fireEvent.click(button);
    expect(screen.getByText("Heavy chart")).toBeVisible();
    fireEvent.click(button);
    expect(screen.getByText("Heavy chart")).not.toBeVisible();
  });

  it("supports controlled use", () => {
    const onChange = vi.fn();
    function Controlled() {
      const [open, setOpen] = useState(false);
      return (
        <Disclosure
          summary="Controlled"
          open={open}
          onOpenChange={(v) => {
            onChange(v);
            setOpen(v);
          }}
        >
          Inside
        </Disclosure>
      );
    }
    render(<Controlled />);
    fireEvent.click(screen.getByRole("button", { name: "Controlled" }));
    expect(onChange).toHaveBeenCalledWith(true);
    expect(screen.getByText("Inside")).toBeVisible();
  });
});

describe("InfoPopover", () => {
  it("has an accessible trigger and opens its explanation on click", () => {
    render(<InfoPopover term="range80" />);
    const trigger = screen.getByRole("button", { name: "Learn more: 80% range" });
    expect(screen.queryByText(/about 8 times in 10/)).toBeNull();
    fireEvent.click(trigger);
    expect(screen.getByText(/about 8 times in 10/)).toBeInTheDocument();
  });
});
