"use client";

import { Info } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { GLOSSARY, type GlossaryKey } from "@/lib/glossary";
import { cn } from "@/lib/utils";

/**
 * "Learn more" for a technical term. A popover (not a tooltip) so it opens on tap as well as
 * click and keyboard, and stays open while being read; Escape or a click outside closes it.
 */
export function InfoPopover({
  term,
  title,
  children,
  label,
  className,
  tone = "default",
}: {
  term?: GlossaryKey;
  title?: string;
  children?: React.ReactNode;
  /** Visible trigger text; without it the trigger is an (i) icon with an accessible name. */
  label?: string;
  className?: string;
  tone?: "default" | "dark";
}) {
  const entry = term ? GLOSSARY[term] : undefined;
  const heading = title ?? entry?.title ?? "Learn more";
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label ? undefined : `Learn more: ${heading}`}
          className={cn(
            "inline-flex shrink-0 items-center gap-1 rounded-full align-middle outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
            label ? "text-xs font-medium text-ocean hover:underline" : "-m-1 size-6 justify-center p-1",
            !label && (tone === "dark" ? "text-white/60 hover:text-white" : "text-muted-foreground hover:text-ocean"),
            className,
          )}
        >
          <Info className="size-3.5" aria-hidden="true" />
          {label}
        </button>
      </PopoverTrigger>
      <PopoverContent side="top" className="w-72 max-w-[calc(100vw-2rem)] text-xs leading-relaxed">
        <div className="mb-1 text-sm font-semibold text-ink">{heading}</div>
        <div className="space-y-1.5 text-muted-foreground">{children ?? <p>{entry?.body}</p>}</div>
      </PopoverContent>
    </Popover>
  );
}
