"use client";

import { ChevronDown } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Show / hide secondary details (WAI-ARIA disclosure pattern): a real <button> with
 * aria-expanded and aria-controls, so it works with mouse, keyboard (Enter / Space) and touch.
 *
 * Collapsed content stays in the DOM (hidden), so in-page search and text assertions still
 * find it; pass `lazy` for heavy content such as charts, which then mounts on first open.
 */
export function Disclosure({
  summary,
  children,
  defaultOpen = false,
  open: openProp,
  onOpenChange,
  lazy = false,
  variant = "plain",
  hint,
  className,
  triggerClassName,
  contentClassName,
  id,
  "data-testid": testId,
}: {
  summary: React.ReactNode;
  children: React.ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  lazy?: boolean;
  /** "plain": an inline text toggle; "card": a bordered panel with a full-width header. */
  variant?: "plain" | "card";
  /** Short text on the right of a card header (e.g. a count). */
  hint?: React.ReactNode;
  className?: string;
  triggerClassName?: string;
  contentClassName?: string;
  id?: string;
  "data-testid"?: string;
}) {
  const autoId = React.useId();
  const contentId = id ?? `disclosure-${autoId.replace(/:/g, "")}`;
  const [innerOpen, setInnerOpen] = React.useState(defaultOpen);
  const open = openProp ?? innerOpen;
  const [mounted, setMounted] = React.useState(open);
  if (open && !mounted) setMounted(true);

  const toggle = () => {
    const next = !open;
    if (openProp === undefined) setInnerOpen(next);
    onOpenChange?.(next);
  };

  const card = variant === "card";
  return (
    <div className={cn(card && "rounded-xl border bg-card", className)} data-state={open ? "open" : "closed"} data-testid={testId}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={toggle}
        className={cn(
          "group/disclosure inline-flex items-center gap-1.5 rounded-md text-left outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
          card
            ? "flex w-full justify-between gap-3 px-4 py-3 text-sm font-semibold text-ink hover:bg-muted/40"
            : "text-xs font-medium text-ocean hover:underline",
          triggerClassName,
        )}
      >
        <span className="flex min-w-0 items-center gap-1.5">{summary}</span>
        <span className="flex shrink-0 items-center gap-2">
          {hint && <span className="text-xs font-normal text-muted-foreground">{hint}</span>}
          <ChevronDown aria-hidden="true" className={cn("size-4 shrink-0 transition-transform", open && "rotate-180")} />
        </span>
      </button>
      <div id={contentId} hidden={!open} className={cn(card ? "border-t px-4 py-3" : "mt-2", contentClassName)}>
        {!lazy || mounted ? children : null}
      </div>
    </div>
  );
}
