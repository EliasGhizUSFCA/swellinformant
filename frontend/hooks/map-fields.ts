"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { ApiError, get } from "@/lib/api";
import { FieldStore, type FieldGrid, type FieldIndex, type FieldVariableName } from "@/lib/map-fields";
import { layerDef } from "@/lib/map-layers";
import type { MapSettings } from "@/lib/map-settings";
import type { Airport } from "@/types/api";

const UNAVAILABLE: FieldIndex = {
  available: false,
  message: "Ocean layers could not be loaded right now.",
  valid_times: [],
  variables: [],
  // What the API itself sends with an unavailable index (no run, so nothing to call stale).
  stale_after_hours: 12,
  stale: false,
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
  run: string | null;
  grids: FieldSet;
}

/**
 * The given fields at one valid time of model run `run` (the index's run_key). Keeps the
 * previous time's fields on screen while the next ones load (no blank flashes when
 * scrubbing). Once they are on screen it prefetches the `prefetch` times, which the caller
 * derives from the displayed (settled) time: steps passed while dragging the timeline are
 * never downloaded.
 */
export function useFieldGrids(variables: FieldVariableName[], at: string | null, prefetch: string[] = [], run: string | null = null) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState<{ key: string; message: string } | null>(null);
  const key = at && variables.length ? `${run ?? ""}|${variables.join(",")}@${at}` : null;
  const prefetchKey = prefetch.join("|");

  useEffect(() => {
    if (!key || !at) return;
    let cancelled = false;
    const s = fieldStore();
    Promise.all(variables.map((v) => s.load(v, at, run).then((g) => [v, g] as const))).then(
      (pairs) => {
        if (cancelled) return;
        setLoaded({ key, at, run, grids: Object.fromEntries(pairs) as FieldSet });
        setFailed(null);
      },
      (e: unknown) => {
        if (!cancelled) setFailed({ key, message: e instanceof Error ? e.message : String(e) });
      },
    );
    return () => {
      cancelled = true;
    };
    // `variables`, `at` and `run` are captured through `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const shown = !!key && loaded?.key === key;
  useEffect(() => {
    if (!shown || !prefetchKey) return;
    const s = fieldStore();
    for (const t of prefetchKey.split("|")) {
      for (const v of variables) s.load(v, t, run).catch(() => undefined);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown, key, prefetchKey]);

  if (!key) return { grids: null, at: null, run: null, loading: false, error: null };
  return {
    grids: loaded?.grids ?? null,
    at: loaded?.at ?? null,
    run: loaded?.run ?? null,
    loading: loaded?.key !== key && failed?.key !== key,
    error: failed?.key === key ? failed.message : null,
  };
}

export interface MapLayerFields {
  /** Fields of the active layer on screen (null without ocean data). */
  fields: FieldSet | null;
  /** The pressure field when the pressure overlay is on. */
  pressure: FieldGrid | null;
  loading: boolean;
  error: string | null;
  /** Valid time and model run of the fields on screen (the previous step's while the next loads). */
  servedValidTime: string | null;
  servedRunKey: string | null;
}

/**
 * The fields the map's layers need at the settled time `at`: the active layer's variables
 * (only those the index offers), the pressure field when that overlay is on, and a prefetch
 * of the neighbouring valid times.
 */
export function useMapLayerFields({ settings, at, index }: { settings: MapSettings; at: string | null; index: FieldIndex | null }): MapLayerFields {
  const available = !!index?.available && index.valid_times.length > 0;
  const offered = useMemo(() => new Set((index?.variables ?? []).map((v) => v.name)), [index]);
  const layerVars = useMemo(() => layerDef(settings.layer).needs.filter((v) => offered.size === 0 || offered.has(v)), [settings.layer, offered]);
  const pressureVars = useMemo<FieldVariableName[]>(() => (settings.pressure && offered.has("pressure") ? ["pressure"] : []), [settings.pressure, offered]);
  // Prefetch around the settled time only, never around steps passed while scrubbing.
  const neighbours = useMemo(() => {
    const times = available ? index.valid_times : [];
    const i = at ? times.indexOf(at) : -1;
    return i < 0 ? [] : [times[i + 1], times[i - 1]].filter((s): s is string => !!s);
  }, [available, index, at]);
  const runKey = available ? (index.run_key ?? null) : null;
  const layerFields = useFieldGrids(layerVars, at, neighbours, runKey);
  const pressureField = useFieldGrids(pressureVars, at, neighbours, runKey);
  return {
    fields: available ? layerFields.grids : null,
    pressure: settings.pressure ? (pressureField.grids?.pressure ?? null) : null,
    loading: layerFields.loading,
    error: layerFields.error,
    servedValidTime: layerFields.at,
    servedRunKey: layerFields.run,
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
