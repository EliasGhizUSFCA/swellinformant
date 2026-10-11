/** Shared look of the floating map controls: translucent dark, white text, lime accent. */
import type { CSSProperties } from "react";

export const GLASS = "bg-[#15191d]/85 text-white shadow-[0_6px_22px_rgba(0,0,0,0.38)] ring-1 ring-white/10 backdrop-blur-md";

/** Focus ring that stays visible on the dark map and on the lime active pill. */
export const FOCUS =
  "outline-none focus-visible:ring-2 focus-visible:ring-[#d9f75e] focus-visible:ring-offset-2 focus-visible:ring-offset-[#15191d]";

/** Active pill: lime with near-black text (contrast ~13:1). */
export const ACTIVE_PILL = "bg-[#c4ec4a] text-[#101418]";

/**
 * Design tokens re-pointed for dark panels, so shared components (cards, badges, the
 * forecast breakdown) render light-on-dark without changes. Contrast of --muted-foreground
 * (#aab6bf) on --card (#171b20) is about 8.6:1.
 */
export const DARK_TOKENS = {
  "--background": "#111418",
  "--foreground": "#f3f5f7",
  "--card": "#171b20",
  "--card-foreground": "#f3f5f7",
  "--popover": "#171b20",
  "--popover-foreground": "#f3f5f7",
  "--muted": "#22282f",
  "--muted-foreground": "#aab6bf",
  "--secondary": "#22282f",
  "--secondary-foreground": "#f3f5f7",
  "--accent": "#262d35",
  "--accent-foreground": "#f3f5f7",
  "--border": "rgba(255, 255, 255, 0.12)",
  "--input": "rgba(255, 255, 255, 0.18)",
  "--ink": "#f3f5f7",
  "--ocean": "#5fd0de",
  "--primary": "#5fd0de",
  "--primary-foreground": "#0b1a20",
  "--ring": "#d9f75e",
  colorScheme: "dark",
} as CSSProperties;
