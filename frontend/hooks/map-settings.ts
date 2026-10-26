"use client";

import { useCallback, useSyncExternalStore } from "react";

import { DEFAULT_SETTINGS, readSettings, SETTINGS_KEY, WIND_UNIT_EVENT, WIND_UNIT_KEY, writeSettings, type MapSettings } from "@/lib/map-settings";

// One shared snapshot so every component sees the same object until a setting changes.
let current: MapSettings | null = null;
const listeners = new Set<() => void>();

function snapshot(): MapSettings {
  current ??= readSettings();
  return current;
}

/** Re-reads storage after another tab or the spot charts changed a shared preference. */
function reload(): void {
  const next = readSettings();
  const prev = current;
  if (prev && JSON.stringify(prev) === JSON.stringify(next)) return;
  current = next;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key === SETTINGS_KEY || e.key === WIND_UNIT_KEY) reload();
  };
  if (listeners.size === 1) {
    window.addEventListener("storage", onStorage);
    window.addEventListener(WIND_UNIT_EVENT, reload);
    detach = () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(WIND_UNIT_EVENT, reload);
    };
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      detach?.();
      detach = null;
    }
  };
}
let detach: (() => void) | null = null;

/** Map display preferences (localStorage-backed; server render uses the defaults). The wind
 * unit is shared with the spot charts through the `swell.windUnit` key. */
export function useMapSettings(): [MapSettings, (patch: Partial<MapSettings>) => void] {
  const settings = useSyncExternalStore(subscribe, snapshot, () => DEFAULT_SETTINGS);
  const update = useCallback((patch: Partial<MapSettings>) => {
    current = { ...snapshot(), ...patch };
    writeSettings(current);
    listeners.forEach((l) => l());
  }, []);
  return [settings, update];
}
