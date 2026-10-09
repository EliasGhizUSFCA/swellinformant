import Link from "next/link";

import { cn } from "@/lib/utils";

export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true" className={cn("size-8", className)}>
      <rect width="64" height="64" rx="16" fill="#06263a" />
      <path d="M8 40c6 0 8-6 14-6s8 6 14 6 8-6 14-6 6 4 6 4v14H8z" fill="#2bb3a3" />
      <path d="M10 32c4-10 14-18 26-16-8 2-12 8-12 14 4-4 10-5 15-2-6 1-10 4-12 8z" fill="#ffffff" />
    </svg>
  );
}

export function Logo({ light = false, href = "/" }: { light?: boolean; href?: string }) {
  return (
    <Link href={href} className="flex items-center gap-2.5" aria-label="Swell Travel Agent home">
      <LogoMark />
      <span className={cn("font-display text-lg font-semibold tracking-tight", light ? "text-white" : "text-ink")}>
        Swell<span className={light ? "text-seafoam" : "text-ocean"}>Travel</span>
      </span>
    </Link>
  );
}
