"use client";

import { useQuery } from "@tanstack/react-query";

import { get } from "@/lib/api";
import type { components } from "@/types/openapi";

/** The deployment's defaults and limits for saved searches (GET /api/searches/defaults). */
export type SearchDefaults = components["schemas"]["SearchDefaultsOut"];

export const searchDefaultsKey = ["searches", "defaults"] as const;

/** Server settings change only on a redeploy: read once per session. */
export function useSearchDefaults() {
  return useQuery({
    queryKey: searchDefaultsKey,
    queryFn: ({ signal }) => get<SearchDefaults>("/api/searches/defaults", signal),
    staleTime: Infinity,
    retry: 1,
  });
}

/** "3–7 days ahead" for the short-notice band (falls back to the spec's 3–7 days). */
export function shortNoticeLabel(d: SearchDefaults | undefined): string {
  const lo = d?.short_notice_min_days ?? 3;
  const hi = d?.short_notice_max_days ?? 7;
  return `${Math.round(lo)}–${Math.round(hi)} days ahead`;
}
