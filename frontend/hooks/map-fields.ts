"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError, get } from "@/lib/api";
import { FieldStore, type FieldGrid, type FieldIndex, type FieldVariableName } from "@/lib/map-fields";
import type { Airport } from "@/types/api";

const UNAVAILABLE: FieldIndex = {
  available: false,
  message: "Ocean layers could not be loaded right now.",
  valid_times: [],
  variables: [],
};

/** Which ocean-layer fields exist for the current model run. Never throws: a failing
 * endpoint reads as "unavailable" so the rest of the map keeps working. */
export const useFieldIndex = () =>
  useQuery({
    queryKey: ["map-fields-index"],
    queryFn: async ({ signal }) => {
      try {
        return await get<FieldIndex>("/api/map/fields", signal);
      } catch (e) {
        if (e instanceof ApiError || e instanceof TypeError) return UNAVAILABLE;
        throw e;
      }
    },
    staleTime: 5 * 60_000,
    retry: false,
  });

// One store per page load: decoded fields are shared by every consumer and survive re-renders.
let store: FieldStore | null = null;
const fieldStore = () => (store ??= new FieldStore(24));

export type FieldSet = Partial<Record<FieldVariableName, FieldGrid>>;

interface Loaded {
  key: string;
  at: string;
  grids: FieldSet;
}

/**
 * The given fields at one valid time. Keeps the previous time's fields on screen while the
 * next ones load (no blank flashes when scrubbing) and prefetches `prefetch` times afterwards.
 */
export function useFieldGrids(variables: FieldVariableName[], at: string | null, prefetch: string[] = []) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState<{ key: string; message: string } | null>(null);
  const key = at && variables.length ? `${variables.join(",")}@${at}` : null;
  const prefetchKey = prefetch.join("|");

  useEffect(() => {
    if (!key || !at) return;
    let cancelled = false;
    const s = fieldStore();
    Promise.all(variables.map((v) => s.load(v, at).then((g) => [v, g] as const))).then(
      (pairs) => {
        if (cancelled) return;
        setLoaded({ key, at, grids: Object.fromEntries(pairs) as FieldSet });
        setFailed(null);
        for (const t of prefetchKey ? prefetchKey.split("|") : []) {
          for (const v of variables) s.load(v, t).catch(() => undefined);
        }
      },
      (e: unknown) => {
        if (!cancelled) setFailed({ key, message: e instanceof Error ? e.message : String(e) });
      },
    );
    return () => {
      cancelled = true;
    };
    // `variables` is captured through `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, prefetchKey]);

  if (!key) return { grids: null, at: null, loading: false, error: null };
  return {
    grids: loaded?.grids ?? null,
    at: loaded?.at ?? null,
    loading: loaded?.key !== key && failed?.key !== key,
    error: failed?.key === key ? failed.message : null,
  };
}

/** Airports (for city labels at zoom ≥ 4); fetched only once `enabled`. */
export const useCityAirports = (enabled: boolean) =>
  useQuery({
    queryKey: ["airports-all"],
    queryFn: ({ signal }) => get<Airport[]>("/api/airports?limit=200", signal),
    enabled,
    staleTime: 3_600_000,
  });
