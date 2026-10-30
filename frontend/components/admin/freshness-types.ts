/**
 * Types of GET /api/admin/freshness (backend app/schemas/admin.py, contract C5), from the
 * generated OpenAPI schema.
 */
import type { components } from "@/types/openapi";

type S = components["schemas"];

export type AdminFreshness = S["AdminFreshnessOut"];
export type AdminProblem = S["AdminProblemOut"];
export type AdminComponent = S["AdminComponentFreshnessOut"];
export type AdminEvent = S["AdminEventOut"];
export type AdminActiveProvider = S["AdminActiveProviderOut"];
export type AdminVersionRow = S["AdminVersionRowOut"];
export type AdminRefreshJob = S["AdminRefreshJobOut"];
export type AdminUsageWindow = S["AdminUsageWindowOut"];
export type AdminDurationStats = S["AdminDurationStatsOut"];
export type AdminStaleSpot = S["AdminStaleSpotOut"];
