"use client";

import { useCallback, useSyncExternalStore } from "react";

import { DEFAULT_SETTINGS, readSettings, writeSettings, type MapSettings } from "@/lib/map-settings";

// One shared snapshot so every component sees the same object until a setting changes.
let current: MapSettings | null = null;
const listeners = new Set<() => void>();

function snapshot(): MapSettings {
  current ??= readSettings();
  return current;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Map display preferences (localStorage-backed; server render uses the defaults). */
export function useMapSettings(): [MapSettings, (patch: Partial<MapSettings>) => void] {
  const settings = useSyncExternalStore(subscribe, snapshot, () => DEFAULT_SETTINGS);
  const update = useCallback((patch: Partial<MapSettings>) => {
    current = { ...snapshot(), ...patch };
    writeSettings(current);
    listeners.forEach((l) => l());
  }, []);
  return [settings, update];
}
