"use client";

/**
 * Data of the admin freshness dashboard (/admin/freshness).
 *
 * GET /api/admin/freshness reads the database (a few bounded statements), so it never runs on
 * a timer: it is read when the page mounts, when the tab regains focus (a report older than
 * 15 s), when the published forecast version changes, and when the admin presses "Reload".
 *
 * The version comes from GET /api/forecasts/version, which the API answers from Redis or its
 * process memo (never the database while a version is cached; design D11). It is asked every
 * NEXT_PUBLIC_FORECAST_VERSION_POLL_MS (default 20 s) only while the tab is visible (TanStack
 * Query pauses interval refetches in the background), and again on focus. The version id is the
 * last part of the report's query key, so a new version is a new key: the report is read again
 * while the previous one stays on screen. Without the version endpoint (an API without it
 * answers 404) the poll stops and the report is still read on focus.
 *
 * Integration seam: the live-updates package's useForecastVersion() store can replace
 * {@link useForecastVersionId} once both are merged (one poll per tab).
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { get } from "@/lib/api";

import type { AdminFreshness } from "./freshness-types";

export const FRESHNESS_PATH = "/api/admin/freshness";
export const VERSION_PATH = "/api/forecasts/version";
export const DEFAULT_POLL_MS = 20_000;
export const MIN_POLL_MS = 1_000;
/** A report younger than this is not read again on focus. */
export const FRESHNESS_STALE_MS = 15_000;

export const freshnessKey = (versionId: number | null) => ["admin", "freshness", { v: versionId }] as const;
export const VERSION_KEY = ["admin", "forecast-version"] as const;

/** The poll interval from NEXT_PUBLIC_FORECAST_VERSION_POLL_MS (inlined at build time). */
export function versionPollMs(raw: string | undefined = process.env.NEXT_PUBLIC_FORECAST_VERSION_POLL_MS): number {
  const n = raw == null || raw.trim() === "" ? Number.NaN : Number(raw);
  return Number.isFinite(n) ? Math.max(MIN_POLL_MS, Math.round(n)) : DEFAULT_POLL_MS;
}

/** The published forecast version id (null: none published; undefined while unknown). */
export function useForecastVersionId(enabled: boolean, pollMs: number = versionPollMs()) {
  return useQuery({
    queryKey: VERSION_KEY,
    queryFn: async ({ signal }) => {
      const body = await get<{ version_id?: number | null }>(VERSION_PATH, signal);
      return typeof body?.version_id === "number" && body.version_id > 0 ? body.version_id : null;
    },
    enabled,
    staleTime: 0,
    retry: false,
    refetchOnWindowFocus: true,
    // Stops after a failure (e.g. an API without the endpoint); focus asks again.
    refetchInterval: (q) => (q.state.status === "error" ? false : pollMs),
    refetchIntervalInBackground: false,
  });
}

/** The dashboard report, keyed by the forecast version (see the module comment). */
export function useAdminFreshness(enabled: boolean, pollMs?: number) {
  const version = useForecastVersionId(enabled, pollMs);
  const settled = version.status !== "pending";
  const versionId = version.data ?? null;
  const report = useQuery({
    queryKey: freshnessKey(versionId),
    queryFn: ({ signal }) => get<AdminFreshness>(FRESHNESS_PATH, signal),
    enabled: enabled && settled,
    staleTime: FRESHNESS_STALE_MS,
    refetchOnWindowFocus: true,
    placeholderData: keepPreviousData,
  });
  return { report, versionId, versionKnown: version.status === "success" };
}
