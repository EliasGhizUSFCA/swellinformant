import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap [&>svg]:size-3",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary/10 text-primary",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        outline: "text-foreground",
        good: "border-transparent bg-emerald-50 text-emerald-700",
        info: "border-transparent bg-sky-50 text-sky-700",
        warn: "border-transparent bg-amber-50 text-amber-800",
        muted: "border-transparent bg-muted text-muted-foreground",
        danger: "border-transparent bg-red-50 text-red-700",
        demo: "border-amber-300 bg-amber-50 text-amber-800 uppercase tracking-wide",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

function Badge({ className, variant, ...props }: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
