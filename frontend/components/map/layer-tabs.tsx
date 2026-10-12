"use client";

import * as RadioGroupPrimitive from "@radix-ui/react-radio-group";
import { Gauge } from "lucide-react";

import { isLayerId, LAYERS, type LayerId } from "@/lib/map-layers";
import { cn } from "@/lib/utils";

import { ACTIVE_PILL, FOCUS, GLASS } from "./map-ui";

/**
 * Segmented layer switch (a radio group: arrow keys move between layers) and the pressure
 * toggle. On narrow screens the row scrolls sideways.
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
  return (
    <div className="flex min-w-0 items-center gap-2 overflow-x-auto p-1 [scrollbar-width:none]" data-testid="layer-tabs">
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
  );
}
