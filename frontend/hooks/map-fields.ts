"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { ApiError, get } from "@/lib/api";
import { FieldMismatchError, FieldStore, FIELD_STORE_CAPACITY, isFieldVariable, type FieldGrid, type FieldIndex, type FieldVariableName } from "@/lib/map-fields";
import { layerDef } from "@/lib/map-layers";
import type { MapSettings } from "@/lib/map-settings";
import { effectiveSystems, SYSTEM_VARIABLES, type WaveSystem } from "@/lib/map-waves";
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

export const FIELD_INDEX_KEY = ["map-fields-index"] as const;
/** The index is re-read this often while the map is open (a new model run every 6-12 h). */
export const FIELD_INDEX_REFETCH_MS = 5 * 60_000;
/** After a field mismatch the index is re-read at most this often (no request loop). */
const MISMATCH_REFETCH_MIN_MS = 30_000;

/** Which ocean-layer fields exist for the current model run. Never throws: a failing
 * endpoint reads as "unavailable" so the rest of the map keeps working. Re-read every 5
 * minutes and when the window regains focus, so a map left open picks up new runs. */
export const useFieldIndex = () =>
  useQuery({
    queryKey: FIELD_INDEX_KEY,
    queryFn: async ({ signal }) => {
      try {
        return await get<FieldIndex>("/api/map/fields", signal);
      } catch (e) {
        if (e instanceof ApiError || e instanceof TypeError) return UNAVAILABLE;
        throw e;
      }
    },
    staleTime: 60_000,
    refetchInterval: FIELD_INDEX_REFETCH_MS,
    refetchOnWindowFocus: true,
    retry: false,
  });

// One store per page load: decoded fields are shared by every consumer and survive re-renders.
let store: FieldStore | null = null;
const fieldStore = () => (store ??= new FieldStore(FIELD_STORE_CAPACITY));

export type FieldSet = Partial<Record<FieldVariableName, FieldGrid>>;

interface Loaded {
  key: string;
  at: string;
  grids: FieldSet;
}

/**
 * The given fields at one valid time of model run `run` (the index's run_key). Keeps the
 * previous time's fields on screen while the next ones load (no blank flashes when
 * scrubbing). Once they are on screen it prefetches the `prefetch` times, which the caller
 * derives from the displayed (settled) time: steps passed while dragging the timeline are
 * never downloaded.
 *
 * `optional` variables (wave partitions) may fail without failing the set: they are simply
 * not drawn. A field the server answers for another valid time or run (or no longer has)
 * is rejected, and `onMismatch` runs so the caller can re-read the field index.
 */
export function useFieldGrids(
  variables: FieldVariableName[],
  at: string | null,
  prefetch: string[] = [],
  run: string | null = null,
  optional: FieldVariableName[] = [],
  onMismatch?: (e: FieldMismatchError) => void,
) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState<{ key: string; message: string } | null>(null);
  const all = useMemo(() => [...variables, ...optional.filter((v) => !variables.includes(v))], [variables, optional]);
  const key = at && variables.length ? `${run ?? ""}|${variables.join(",")}+${optional.join(",")}@${at}` : null;
  const prefetchKey = prefetch.join("|");
  const mismatchRef = useRef(onMismatch);
  useEffect(() => {
    mismatchRef.current = onMismatch;
  });

  useEffect(() => {
    if (!key || !at) return;
    let cancelled = false;
    const s = fieldStore();
    const report = (e: unknown) => {
      if (e instanceof FieldMismatchError) mismatchRef.current?.(e);
    };
    const required = Promise.all(variables.map((v) => s.load(v, at, run).then((g) => [v, g] as const)));
    const extra = Promise.all(
      optional.map((v) =>
        s.load(v, at, run).then(
          (g) => [v, g] as const,
          (e: unknown) => {
            report(e);
            return null;
          },
        ),
      ),
    );
    Promise.all([required, extra]).then(
      ([pairs, more]) => {
        if (cancelled) return;
        const grids = Object.fromEntries([...pairs, ...more.filter((p): p is NonNullable<typeof p> => !!p)]) as FieldSet;
        setLoaded({ key, at, grids });
        setFailed(null);
      },
      (e: unknown) => {
        report(e);
        if (!cancelled) setFailed({ key, message: e instanceof Error ? e.message : String(e) });
      },
    );
    return () => {
      cancelled = true;
    };
    // `variables`, `optional`, `at` and `run` are captured through `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const shown = !!key && loaded?.key === key;
  useEffect(() => {
    if (!shown || !prefetchKey) return;
    const s = fieldStore();
    for (const t of prefetchKey.split("|")) {
      for (const v of all) s.load(v, t, run).catch(() => undefined);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown, key, prefetchKey]);

  if (!key) return { grids: null, at: null, loading: false, error: null };
  return {
    grids: loaded?.grids ?? null,
    at: loaded?.at ?? null,
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
  /** Valid time of the fields on screen (the previous step's while the next loads). */
  servedValidTime: string | null;
}

/** Wave systems the index offers: "combined" when hs/tp/wave_dir exist, partitions when listed. */
export function availableSystems(index: FieldIndex | null | undefined): Set<WaveSystem> {
  const names = new Set((index?.variables ?? []).map((v) => v.name));
  const out = new Set<WaveSystem>();
  for (const [system, vars] of Object.entries(SYSTEM_VARIABLES) as Array<[WaveSystem, (typeof SYSTEM_VARIABLES)[WaveSystem]]>) {
    if (names.has(vars.hs) && names.has(vars.tp) && names.has(vars.dir)) out.add(system);
  }
  return out;
}

/** Whether the index lists any wave partition (group "partition"). */
export const hasPartitions = (index: FieldIndex | null | undefined): boolean => (index?.variables ?? []).some((v) => v.group === "partition");

/**
 * The variables the map's layers need at one step: the active layer's (only those the
 * index offers), the wave partitions of the chosen systems (wave layers, only when the index
 * lists them) and the wind components for the wind-particle overlay on a wave layer.
 */
export function layerVariables(settings: MapSettings, index: FieldIndex | null | undefined): { required: FieldVariableName[]; optional: FieldVariableName[] } {
  const offered = new Set((index?.variables ?? []).map((v) => v.name));
  const has = (v: string) => offered.size === 0 || offered.has(v);
  const required = layerDef(settings.layer).needs.filter(has);
  const optional: FieldVariableName[] = [];
  if (settings.layer !== "wind") {
    for (const system of effectiveSystems(settings.waveSystems, availableSystems(index))) {
      if (system === "combined") continue;
      for (const v of Object.values(SYSTEM_VARIABLES[system])) if (isFieldVariable(v) && offered.has(v)) optional.push(v);
    }
    if (settings.windOverlay) for (const v of ["wind_u", "wind_v"] as const) if (has(v)) optional.push(v);
  }
  return { required, optional };
}

/**
 * The fields the map's layers need at the settled time `at`: the active layer's variables
 * (only those the index offers), the chosen wave partitions, the wind overlay, the pressure
 * field when that overlay is on, and a prefetch of the neighbouring valid times. A field
 * served for another time or run than the index named makes the index refetch.
 */
export function useMapLayerFields({ settings, at, index }: { settings: MapSettings; at: string | null; index: FieldIndex | null }): MapLayerFields {
  const queryClient = useQueryClient();
  const available = !!index?.available && index.valid_times.length > 0;
  // Stable lists while the variables stay the same (any other setting may change).
  const varsKey = useMemo(() => {
    const v = layerVariables(settings, index);
    return `${v.required.join(",")}+${v.optional.join(",")}`;
  }, [settings, index]);
  const [layerVars, optionalVars] = useMemo(() => {
    const [required = "", optional = ""] = varsKey.split("+");
    const list = (s: string) => s.split(",").filter(isFieldVariable);
    return [list(required), list(optional)];
  }, [varsKey]);
  const offered = useMemo(() => new Set((index?.variables ?? []).map((v) => v.name)), [index]);
  const pressureVars = useMemo<FieldVariableName[]>(() => (settings.pressure && offered.has("pressure") ? ["pressure"] : []), [settings.pressure, offered]);
  // Prefetch around the settled time only, never around steps passed while scrubbing.
  const neighbours = useMemo(() => {
    const times = available ? index.valid_times : [];
    const i = at ? times.indexOf(at) : -1;
    return i < 0 ? [] : [times[i + 1], times[i - 1]].filter((s): s is string => !!s);
  }, [available, index, at]);
  const runKey = available ? (index.run_key ?? null) : null;
  const lastRefetch = useRef(0);
  const onMismatch = useMemo(
    () => () => {
      const now = Date.now();
      if (now - lastRefetch.current < MISMATCH_REFETCH_MIN_MS) return;
      lastRefetch.current = now;
      void queryClient.invalidateQueries({ queryKey: FIELD_INDEX_KEY });
    },
    [queryClient],
  );
  const layerFields = useFieldGrids(layerVars, at, neighbours, runKey, optionalVars, onMismatch);
  const pressureField = useFieldGrids(pressureVars, at, neighbours, runKey, [], onMismatch);
  return {
    fields: available ? layerFields.grids : null,
    pressure: settings.pressure ? (pressureField.grids?.pressure ?? null) : null,
    loading: layerFields.loading,
    error: layerFields.error,
    servedValidTime: layerFields.at,
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

// --- what the map canvases show, for the legend --------------------------------------------

/** State of the map's flow layers, published by the canvases (map-canvas-layers) for the
 * legend, which lives outside the Leaflet map. */
export interface MapFlowStatus {
  /** Leaflet zoom and the latitude of the view centre (the stated crest exaggeration). */
  zoom: number;
  centreLat: number;
  /** Wave systems drawn as crests ([] on the Wind layer or without wave data). */
  systems: WaveSystem[];
  /** Valid time of the fields on screen. */
  validTime: string | null;
}

const INITIAL_STATUS: MapFlowStatus = { zoom: 2, centreLat: 15, systems: [], validTime: null };
let flowStatus = INITIAL_STATUS;
const flowListeners = new Set<() => void>();

export function publishMapFlowStatus(patch: Partial<MapFlowStatus>): void {
  const next = { ...flowStatus, ...patch };
  if ((Object.keys(next) as Array<keyof MapFlowStatus>).every((k) => JSON.stringify(next[k]) === JSON.stringify(flowStatus[k]))) return;
  flowStatus = next;
  flowListeners.forEach((l) => l());
}

export function useMapFlowStatus(): MapFlowStatus {
  return useSyncExternalStore(
    (l) => {
      flowListeners.add(l);
      return () => flowListeners.delete(l);
    },
    () => flowStatus,
    () => INITIAL_STATUS,
  );
}
