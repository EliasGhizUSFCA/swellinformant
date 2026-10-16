"use client";

import * as RadioGroupPrimitive from "@radix-ui/react-radio-group";
import { Gauge } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { isLayerId, LAYERS, type LayerId } from "@/lib/map-layers";
import { cn } from "@/lib/utils";

import { ACTIVE_PILL, FOCUS, GLASS } from "./map-ui";

/**
 * Segmented layer switch (a radio group: arrow keys move between layers) and the pressure
 * toggle. On narrow screens the row scrolls sideways: edge fades show there is more, and the
 * selected layer is scrolled into view whenever it changes.
 */
export function LayerTabs({
  layer,
  onLayer,
  pressure,
  onPressure,
}: {
  layer: LayerId;
  onLayer: (l: LayerId) => void;
  pressure: boolean;
  onPressure: (on: boolean) => void;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });
  const measure = useCallback(() => {
    const el = rowRef.current;
    if (!el) return;
    setEdges({ left: el.scrollLeft > 2, right: el.scrollLeft + el.clientWidth < el.scrollWidth - 2 });
  }, []);
  useEffect(() => {
    const el = rowRef.current;
    if (!el) return;
    // Keep the checked layer visible (it scrolled out of view after toggling Pressure). Only the
    // row scrolls, never the page.
    const checked = el.querySelector<HTMLElement>('[data-state="checked"]');
    if (checked) {
      const row = el.getBoundingClientRect();
      const b = checked.getBoundingClientRect();
      const delta = b.left < row.left ? b.left - row.left - 8 : b.right > row.right ? b.right - row.right + 8 : 0;
      if (delta) el.scrollLeft += delta;
    }
    measure();
  }, [layer, pressure, measure]);
  useEffect(() => {
    const el = rowRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure]);
  return (
    <div className="relative min-w-0">
      <div ref={rowRef} onScroll={measure} className="flex min-w-0 items-center gap-2 overflow-x-auto p-1 [scrollbar-width:none]" data-testid="layer-tabs">
        <RadioGroupPrimitive.Root
          value={layer}
          onValueChange={(v) => isLayerId(v) && onLayer(v)}
          aria-label="Map layer"
          orientation="horizontal"
          loop
          className={cn("flex shrink-0 items-center gap-0.5 rounded-full p-1", GLASS)}
        >
          {LAYERS.map((l) => (
            <RadioGroupPrimitive.Item
              key={l.id}
              value={l.id}
              className={cn(
                "rounded-full px-3.5 py-1.5 text-sm font-medium whitespace-nowrap text-white transition-colors hover:bg-white/10",
                "data-[state=checked]:hover:bg-[#c4ec4a]",
                layer === l.id && ACTIVE_PILL,
                FOCUS,
              )}
            >
              {l.label}
            </RadioGroupPrimitive.Item>
          ))}
        </RadioGroupPrimitive.Root>
        <button
          type="button"
          aria-pressed={pressure}
          onClick={() => onPressure(!pressure)}
          className={cn(
            "flex shrink-0 items-center gap-1.5 rounded-full px-3 py-2 text-sm font-medium whitespace-nowrap transition-colors",
            pressure ? ACTIVE_PILL : cn(GLASS, "hover:bg-[#23292f]/90"),
            FOCUS,
          )}
        >
          <Gauge className="size-4" aria-hidden />
          Pressure
        </button>
      </div>
      {/* More layers beyond the edge: a fade, not a hidden scrollbar alone. */}
      {edges.left && <span className="pointer-events-none absolute inset-y-0 left-0 w-6 rounded-l-full bg-gradient-to-r from-[#151a21] to-transparent" aria-hidden="true" data-testid="layer-tabs-fade-left" />}
      {edges.right && <span className="pointer-events-none absolute inset-y-0 right-0 w-6 rounded-r-full bg-gradient-to-l from-[#151a21] to-transparent" aria-hidden="true" data-testid="layer-tabs-fade-right" />}
    </div>
  );
}
