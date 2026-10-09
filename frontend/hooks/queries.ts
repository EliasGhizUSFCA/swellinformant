"use client";

import { useQuery } from "@tanstack/react-query";

import { ApiError, get } from "@/lib/api";
import type {
  Airport,
  MatchDetail,
  MatchSummary,
  NotificationPage,
  Region,
  Search,
  SpotDetail,
  SpotForecast,
  SpotSummary,
  SwellEvent,
  SystemStatus,
  User,
} from "@/types/api";

export const qk = {
  me: ["me"] as const,
  spots: ["spots"] as const,
  spot: (slug: string) => ["spot", slug] as const,
  forecast: (slug: string, days: number) => ["forecast", slug, days] as const,
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

export const useOpportunities = (includePast = false) =>
  useQuery({
    queryKey: qk.opportunities(includePast),
    queryFn: ({ signal }) => get<MatchSummary[]>(`/api/opportunities?include_past=${includePast}`, signal),
    refetchInterval: 60_000,
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
