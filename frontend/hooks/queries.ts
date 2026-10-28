"use client";

import { keepPreviousData, useQuery, type PlaceholderDataFunction, type QueryKey } from "@tanstack/react-query";

import { sameExceptVersion, useVersionedRead, versionKey } from "@/hooks/use-forecast-version";
import { ApiError, get } from "@/lib/api";
import type {
  AccuracyMetric,
  AccuracyOverview,
  AccuracyPairs,
  Airport,
  MapSnapshot,
  MatchDetail,
  MatchSummary,
  NotificationPage,
  Region,
  Search,
  SpotDetail,
  SpotForecast,
  SpotSummary,
  SpotTimeline,
  SwellEvent,
  SystemStatus,
  User,
  VerificationStatus,
} from "@/types/api";

/**
 * Query keys. The public forecast reads (spot list, spot, forecast, timeline, events, map
 * conditions) end with the forecast version their data belongs to (`{ v }`, see
 * hooks/use-forecast-version.ts), and their URLs carry the same `?v=` (taken from the key, so
 * the version provider can read a new version under its own key before the page moves to it).
 */
export const qk = {
  me: ["me"] as const,
  spots: (v: number | null = null) => ["spots", versionKey(v)] as const,
  spot: (slug: string, v: number | null = null) => ["spot", slug, versionKey(v)] as const,
  forecast: (slug: string, days: number, v: number | null = null) => ["forecast", slug, days, versionKey(v)] as const,
  timeline: (slug: string, days: number, v: number | null = null) => ["timeline", slug, days, versionKey(v)] as const,
  spotEvents: (slug: string, v: number | null = null) => ["spot-events", slug, versionKey(v)] as const,
  spotOpportunities: (slug: string) => ["spot-opportunities", slug] as const,
  events: (limit: number, minScore: number, v: number | null = null) => ["events", limit, minScore, versionKey(v)] as const,
  searches: ["searches"] as const,
  search: (id: string) => ["search", id] as const,
  searchMatches: (id: string) => ["search-matches", id] as const,
  opportunities: (includePast: boolean) => ["opportunities", includePast] as const,
  opportunity: (id: string) => ["opportunity", id] as const,
  notifications: (offset: number, limit: number) => ["notifications", offset, limit] as const,
  airports: (q: string) => ["airports", q] as const,
  regions: ["regions"] as const,
  status: ["system-status"] as const,
  verification: ["verification-status"] as const,
  accuracy: (part: string, params: string) => ["accuracy", part, params] as const,
  mapConditions: (at: string | null, v: number | null = null) => ["map-conditions", at, versionKey(v)] as const,
};

/** placeholderData of a versioned read: the previous version's data of the same read stays
 * on screen while a read of the new version that was not read ahead loads (never another
 * spot's or another query's), so an update never blanks the page, unmounts a chart or loses
 * its selection or scroll. */
function previousVersion<T, K extends QueryKey>(key: K): PlaceholderDataFunction<T, Error, T, K> {
  return (previous, previousQuery) => (previousQuery && sameExceptVersion(previousQuery.queryKey, key) ? previous : undefined);
}

/** The signed-in user, or `null` when signed out (never throws for 401). */
export function useMe() {
  return useQuery({
    queryKey: qk.me,
    queryFn: async ({ signal }) => {
      try {
        return await get<User>("/api/auth/me", signal);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
    retry: false,
  });
}

export function useSpots() {
  const read = useVersionedRead();
  const queryKey = qk.spots(read.v);
  return useQuery({
    queryKey,
    queryFn: async ({ queryKey: key, signal }) => get<SpotSummary[]>(await read.url("/api/spots", key, signal), signal),
    placeholderData: previousVersion<SpotSummary[], typeof queryKey>(queryKey),
    staleTime: 120_000,
  });
}

export function useSpot(slug: string) {
  const read = useVersionedRead();
  const queryKey = qk.spot(slug, read.v);
  return useQuery({
    queryKey,
    queryFn: async ({ queryKey: key, signal }) => get<SpotDetail>(await read.url(`/api/spots/${slug}`, key, signal), signal),
    placeholderData: previousVersion<SpotDetail, typeof queryKey>(queryKey),
  });
}

export function useForecast(slug: string, days = 10) {
  const read = useVersionedRead();
  const queryKey = qk.forecast(slug, days, read.v);
  return useQuery({
    queryKey,
    queryFn: async ({ queryKey: key, signal }) => get<SpotForecast>(await read.url(`/api/spots/${slug}/forecast?days=${days}`, key, signal), signal),
    placeholderData: previousVersion<SpotForecast, typeof queryKey>(queryKey),
    staleTime: 120_000,
  });
}

/** Every chart row of the spot page on one time axis (fetched once per version; selection is
 * client-side and survives a new version: every row snaps it to the nearest step). */
export function useSpotTimeline(slug: string, days = 10) {
  const read = useVersionedRead();
  const queryKey = qk.timeline(slug, days, read.v);
  return useQuery({
    queryKey,
    queryFn: async ({ queryKey: key, signal }) => get<SpotTimeline>(await read.url(`/api/spots/${slug}/timeline?days=${days}`, key, signal), signal),
    placeholderData: previousVersion<SpotTimeline, typeof queryKey>(queryKey),
    staleTime: 120_000,
  });
}

export function useSpotEvents(slug: string) {
  const read = useVersionedRead();
  const queryKey = qk.spotEvents(slug, read.v);
  return useQuery({
    queryKey,
    queryFn: async ({ queryKey: key, signal }) => get<SwellEvent[]>(await read.url(`/api/spots/${slug}/events`, key, signal), signal),
    placeholderData: previousVersion<SwellEvent[], typeof queryKey>(queryKey),
  });
}

export const useSpotOpportunities = (slug: string, enabled: boolean) =>
  useQuery({
    queryKey: qk.spotOpportunities(slug),
    queryFn: ({ signal }) => get<MatchSummary[]>(`/api/spots/${slug}/opportunities`, signal),
    enabled,
  });

export function useEvents(limit = 12, minScore = 0) {
  const read = useVersionedRead();
  const queryKey = qk.events(limit, minScore, read.v);
  return useQuery({
    queryKey,
    queryFn: async ({ queryKey: key, signal }) => get<SwellEvent[]>(await read.url(`/api/events?limit=${limit}&min_score=${minScore}`, key, signal), signal),
    placeholderData: previousVersion<SwellEvent[], typeof queryKey>(queryKey),
    staleTime: 120_000,
  });
}

export const useSearches = () =>
  useQuery({ queryKey: qk.searches, queryFn: ({ signal }) => get<Search[]>("/api/searches", signal) });

export const useSearch = (id: string) =>
  useQuery({ queryKey: qk.search(id), queryFn: ({ signal }) => get<Search>(`/api/searches/${id}`, signal) });

export const useSearchMatches = (id: string) =>
  useQuery({
    queryKey: qk.searchMatches(id),
    queryFn: ({ signal }) => get<MatchSummary[]>(`/api/searches/${id}/matches`, signal),
  });

/** The signed-in user's opportunities (no-store: every read reaches the database). Read on
 * mount and when the window regains focus (after the 30 s staleTime), never on a timer: a
 * poll every minute kept a scale-to-zero database (Neon) awake for as long as any dashboard
 * stayed visible, while matching runs hourly and after syncs anyway. A new forecast version
 * re-reads it once, after the refresh's matching (hooks/use-forecast-version.ts, SETTLE_MS). */
export const useOpportunities = (includePast = false) =>
  useQuery({
    queryKey: qk.opportunities(includePast),
    queryFn: ({ signal }) => get<MatchSummary[]>(`/api/opportunities?include_past=${includePast}`, signal),
    refetchOnWindowFocus: true,
  });

export const useOpportunity = (id: string) =>
  useQuery({ queryKey: qk.opportunity(id), queryFn: ({ signal }) => get<MatchDetail>(`/api/opportunities/${id}`, signal) });

export const useNotifications = (offset = 0, limit = 50) =>
  useQuery({
    queryKey: qk.notifications(offset, limit),
    queryFn: ({ signal }) => get<NotificationPage>(`/api/notifications?limit=${limit}&offset=${offset}`, signal),
  });

export const useAirports = (q: string) =>
  useQuery({
    queryKey: qk.airports(q),
    queryFn: ({ signal }) => get<Airport[]>(`/api/airports?q=${encodeURIComponent(q)}&limit=12`, signal),
    enabled: q.trim().length >= 2,
    staleTime: 600_000,
  });

export const useRegions = () =>
  useQuery({ queryKey: qk.regions, queryFn: ({ signal }) => get<Region[]>("/api/regions", signal), staleTime: 3_600_000 });

export const useSystemStatus = () =>
  useQuery({ queryKey: qk.status, queryFn: ({ signal }) => get<SystemStatus>("/api/system/status", signal), staleTime: 60_000 });

/** Developer accuracy dashboard (admin accounts only). */
export const useAccuracyOverview = (enabled: boolean) =>
  useQuery({
    queryKey: qk.accuracy("overview", ""),
    queryFn: ({ signal }) => get<AccuracyOverview>("/api/admin/accuracy/overview", signal),
    enabled,
    staleTime: 60_000,
  });

export const useAccuracyMetrics = (params: URLSearchParams, enabled: boolean) =>
  useQuery({
    queryKey: qk.accuracy("metrics", params.toString()),
    queryFn: ({ signal }) => get<AccuracyMetric[]>(`/api/admin/accuracy/metrics?${params}`, signal),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });

export const useAccuracyPairs = (params: URLSearchParams, enabled: boolean) =>
  useQuery({
    queryKey: qk.accuracy("pairs", params.toString()),
    queryFn: ({ signal }) => get<AccuracyPairs>(`/api/admin/accuracy/pairs?${params}`, signal),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });

/** What happened to the latest verification email; polls while it is still in flight. */
export const useVerificationStatus = (enabled: boolean) =>
  useQuery({
    queryKey: qk.verification,
    queryFn: ({ signal }) => get<VerificationStatus>("/api/auth/verification-status", signal),
    enabled,
    refetchInterval: (q) => {
      const s = q.state.data?.last_email?.status;
      return s === "queued" || s === "sending" ? 2000 : false;
    },
  });

/** All spots at one forecast hour (cached predictions). Keeps the previous hour (and the
 * previous version) on screen while the next loads, so scrubbing the time control or a new
 * forecast never blanks the map. A page left open picks up a new forecast through its version
 * (the key and ?v=, hooks/use-forecast-version.ts), never on a timer; the "now" snapshot (`at`
 * null) is also read again when the window regains focus (its hour moves on). */
export function useMapConditions(at: string | null) {
  const read = useVersionedRead();
  return useQuery({
    queryKey: qk.mapConditions(at, read.v),
    queryFn: async ({ queryKey: key, signal }) =>
      get<MapSnapshot>(await read.url(at ? `/api/map/conditions?at=${encodeURIComponent(at)}` : "/api/map/conditions", key, signal), signal),
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: at === null,
  });
}
