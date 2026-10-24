"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";

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

export const qk = {
  me: ["me"] as const,
  spots: ["spots"] as const,
  spot: (slug: string) => ["spot", slug] as const,
  forecast: (slug: string, days: number) => ["forecast", slug, days] as const,
  timeline: (slug: string, days: number) => ["timeline", slug, days] as const,
  spotEvents: (slug: string) => ["spot-events", slug] as const,
  spotOpportunities: (slug: string) => ["spot-opportunities", slug] as const,
  events: (limit: number, minScore: number) => ["events", limit, minScore] as const,
  searches: ["searches"] as const,
  search: (id: string) => ["search", id] as const,
  searchMatches: (id: string) => ["search-matches", id] as const,
  opportunities: (includePast: boolean) => ["opportunities", includePast] as const,
  opportunity: (id: string) => ["opportunity", id] as const,
  notifications: (offset: number) => ["notifications", offset] as const,
  airports: (q: string) => ["airports", q] as const,
  regions: ["regions"] as const,
  status: ["system-status"] as const,
  verification: ["verification-status"] as const,
  accuracy: (part: string, params: string) => ["accuracy", part, params] as const,
  mapConditions: (at: string | null) => ["map-conditions", at] as const,
};

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

export const useSpots = () =>
  useQuery({ queryKey: qk.spots, queryFn: ({ signal }) => get<SpotSummary[]>("/api/spots", signal), staleTime: 120_000 });

export const useSpot = (slug: string) =>
  useQuery({ queryKey: qk.spot(slug), queryFn: ({ signal }) => get<SpotDetail>(`/api/spots/${slug}`, signal) });

export const useForecast = (slug: string, days = 10) =>
  useQuery({
    queryKey: qk.forecast(slug, days),
    queryFn: ({ signal }) => get<SpotForecast>(`/api/spots/${slug}/forecast?days=${days}`, signal),
    staleTime: 120_000,
  });

/** Every chart row of the spot page on one time axis (fetched once; selection is client-side). */
export const useSpotTimeline = (slug: string, days = 10) =>
  useQuery({
    queryKey: qk.timeline(slug, days),
    queryFn: ({ signal }) => get<SpotTimeline>(`/api/spots/${slug}/timeline?days=${days}`, signal),
    staleTime: 120_000,
  });

export const useSpotEvents = (slug: string) =>
  useQuery({ queryKey: qk.spotEvents(slug), queryFn: ({ signal }) => get<SwellEvent[]>(`/api/spots/${slug}/events`, signal) });

export const useSpotOpportunities = (slug: string, enabled: boolean) =>
  useQuery({
    queryKey: qk.spotOpportunities(slug),
    queryFn: ({ signal }) => get<MatchSummary[]>(`/api/spots/${slug}/opportunities`, signal),
    enabled,
  });

export const useEvents = (limit = 12, minScore = 0) =>
  useQuery({
    queryKey: qk.events(limit, minScore),
    queryFn: ({ signal }) => get<SwellEvent[]>(`/api/events?limit=${limit}&min_score=${minScore}`, signal),
    staleTime: 120_000,
  });

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
 * stayed visible, while matching runs hourly and after syncs anyway. */
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
    queryKey: qk.notifications(offset),
    queryFn: ({ signal }) => get<NotificationPage>(`/api/notifications?limit=${limit}&offset=${offset}`, signal),
  });

export const useAirports = (q: string) =>
  useQuery({
    queryKey: qk.airports(q),
    queryFn: ({ signal }) => get<Airport[]>(`/api/airports?q=${encodeURIComponent(q)}&limit=12`, signal),
    enabled: q.trim().length >= 2,
    staleTime: 600_000,
  });

export const useAllAirports = () =>
  useQuery({
    queryKey: ["airports-all"],
    queryFn: ({ signal }) => get<Airport[]>("/api/airports?limit=200", signal),
    staleTime: 3_600_000,
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

/** All spots at one forecast hour (cached predictions). Keeps the previous hour on screen
 * while the next loads so scrubbing the time control never blanks the map. The "now" snapshot
 * (`at` null) is refreshed every 5 minutes and when the window regains focus, so a page left
 * open picks up new model runs. */
export const useMapConditions = (at: string | null) =>
  useQuery({
    queryKey: qk.mapConditions(at),
    queryFn: ({ signal }) =>
      get<MapSnapshot>(at ? `/api/map/conditions?at=${encodeURIComponent(at)}` : "/api/map/conditions", signal),
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
    refetchInterval: at === null ? 300_000 : false,
    refetchOnWindowFocus: at === null,
  });
