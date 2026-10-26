"use client";

import { createContext, useContext, useEffect, useState, useSyncExternalStore } from "react";

/**
 * One selected time for every chart row of the spot timeline, plus the hover position.
 *
 * A tiny external store (no dependency) read through useSyncExternalStore with a selector,
 * so a component re-renders only when the slice it reads changes: moving the pointer
 * re-renders the cursor layer and nothing else, and the rows never subscribe at all.
 * Selecting a time also writes ?t=<ISO time> to the address bar (history.replaceState, no
 * navigation and no scroll jump), so the selected step can be linked.
 */

interface SelectionState {
  /** Epoch ms of the selected step's valid time. */
  selectedT: number | null;
  /** Epoch ms under the pointer (snapped to a step by the caller), or null. */
  hoverT: number | null;
  /** Where the tooltip for hoverT points: px from the left edge of the timeline frame. */
  hoverX: number | null;
}

interface SelectionActions {
  select: (t: number | null) => void;
  hover: (t: number | null, x?: number | null) => void;
}

export class SelectionStore implements SelectionActions {
  private state: SelectionState;
  private listeners = new Set<() => void>();
  private urlTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    initial: Partial<SelectionState> = {},
    private readonly syncUrl = false,
  ) {
    this.state = { selectedT: initial.selectedT ?? null, hoverT: initial.hoverT ?? null, hoverX: initial.hoverX ?? null };
  }

  getState = (): SelectionState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(patch: Partial<SelectionState>) {
    const next = { ...this.state, ...patch };
    if (next.selectedT === this.state.selectedT && next.hoverT === this.state.hoverT && next.hoverX === this.state.hoverX) return;
    this.state = next;
    for (const l of this.listeners) l();
  }

  select = (t: number | null): void => {
    this.set({ selectedT: t });
    if (this.syncUrl && t != null) this.writeUrl(t);
  };

  hover = (t: number | null, x: number | null = null): void => this.set({ hoverT: t, hoverX: t == null ? null : x });

  /** Seed the selection (a default or a deep link) unless something is selected already;
   * unlike select() it leaves the address bar alone. */
  initialise = (t: number | null): void => {
    if (this.state.selectedT == null && t != null) this.set({ selectedT: t });
  };

  private writeUrl(t: number) {
    if (typeof window === "undefined") return;
    if (this.urlTimer) clearTimeout(this.urlTimer);
    this.urlTimer = setTimeout(() => {
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("t", new Date(t).toISOString().replace(".000Z", "Z"));
        window.history.replaceState(window.history.state, "", url);
      } catch {
        // Some embedded browsers refuse history updates; the selection still works.
      }
    }, 150);
  }

  dispose() {
    if (this.urlTimer) clearTimeout(this.urlTimer);
  }
}

const SelectionContext = createContext<SelectionStore | null>(null);

export function TimelineSelectionProvider({
  initialT,
  syncUrl = true,
  store: given,
  children,
}: {
  initialT: number | null;
  syncUrl?: boolean;
  /** Pass a store to control it from outside (tests). */
  store?: SelectionStore;
  children: React.ReactNode;
}) {
  // One store per provider instance; the initial time only seeds it.
  const [store] = useState(() => given ?? new SelectionStore({ selectedT: initialT }, syncUrl));
  useEffect(() => () => store.dispose(), [store]);
  return <SelectionContext.Provider value={store}>{children}</SelectionContext.Provider>;
}

function useStore(): SelectionStore {
  const store = useContext(SelectionContext);
  if (!store) throw new Error("useTimelineSelection must be used inside TimelineSelectionProvider");
  return store;
}

/** A slice of the selection; the component re-renders only when the slice changes. Return
 * primitives (or memoised values) from the selector. */
export function useTimelineSelection<T>(selector: (s: SelectionState) => T): T {
  const store = useStore();
  return useSyncExternalStore(
    store.subscribe,
    () => selector(store.getState()),
    () => selector(store.getState()),
  );
}

/** select / hover; stable for the provider's lifetime (never causes a re-render). */
export function useTimelineActions(): SelectionActions {
  return useStore();
}

/** The store itself, for effects that follow the selection without re-rendering
 * (store.subscribe + store.getState). */
export function useTimelineStore(): SelectionStore {
  return useStore();
}
