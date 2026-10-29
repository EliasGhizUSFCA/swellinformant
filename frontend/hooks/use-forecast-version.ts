"use client";

/**
 * Live forecast updates without a page reload (spec 12 and 15, design D11, contract C4).
 *
 * One {@link ForecastVersionStore} per tab (created by {@link ForecastVersionProvider} in
 * components/providers.tsx) asks GET /api/forecasts/version, which the API answers from Redis
 * or its memo (never the database while a version is cached) and the CDN shares for a few
 * seconds:
 *
 * - every NEXT_PUBLIC_FORECAST_VERSION_POLL_MS (default 20 s), and only while the tab is
 *   visible: nothing while it is hidden, and at once when it becomes visible again;
 * - after a failed request the wait doubles (up to 5 minutes) until one succeeds;
 * - only while a component shows versioned data (the public forecast hooks of hooks/queries.ts
 *   and the "Forecast updated" label retain the store): pages without forecast data never ask.
 *
 * The public forecast reads put the version in their query keys (`{ v }`, the last key part)
 * and URLs (`?v=<version_id>`): a new version is a new key, and a new URL for the browser cache
 * and the CDN. A new version is switched to as a whole: every public read on screen is first
 * read under the new version (into the query cache, while the page keeps showing the previous
 * version), and only then do the keys, and the "Forecast updated" label with them, move to it
 * (`shown`). If one of those reads fails (a server error or no network), the page stays on the
 * previous version, its data and its label, and the next answer of the poll tries again; an
 * update therefore never blanks a page or leaves half of it on another version. An answer older
 * than the newest one seen (another CDN copy, another API instance's memo) changes nothing. A
 * new map run (`map_run_id`) refreshes the map's field index. The first page load waits for
 * the version (at most INIT_TIMEOUT_MS) so its data is read once, already versioned.
 *
 * When a version replaces the one a tab showed, once its data is on screen: the "Forecast
 * updated" label announces it (aria-live), and the tab reports it once with
 * POST /api/forecasts/seen (navigator.sendBeacon) for the publication-to-browser latency, only
 * if the tab stayed visible from the answer that showed the previous version until the new data
 * was painted (a tab opened or shown late would measure its own absence). The signed-in
 * opportunity lists (no-store database reads) are re-read once SETTLE_MS later, after the
 * refresh's matching ran. When the UTC hour changes, the public reads on screen are read again
 * (with `&h=<hour>`): a spot whose forecast went out of date by age is then shown so. That
 * re-read keeps the data on screen, and a read that fails is tried again at the next answer;
 * an answer that also brings a new version reads only under the new version.
 */

import { useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { createContext, createElement, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";

import { ApiError, FORECAST_VERSION_PATH, get, sendSeenBeacon, versionParam, withParams, type ForecastVersion } from "@/lib/api";

/** NEXT_PUBLIC_FORECAST_VERSION_POLL_MS when it is not set (or not a number). */
export const DEFAULT_POLL_MS = 20_000;
/** The shortest interval accepted (a mistyped "20" must not poll fifty times a second). */
export const MIN_POLL_MS = 1_000;
/** The longest wait between attempts while the endpoint keeps failing. */
export const MAX_BACKOFF_MS = 5 * 60_000;
/** How long a first page load waits for the version before reading data without it. */
export const INIT_TIMEOUT_MS = 2_500;
/** After a new version, when the signed-in opportunity lists are read again. */
export const SETTLE_MS = 45_000;
/** The map's field index (hooks/map-fields.ts), refreshed when the map run changes. */
export const FIELD_INDEX_KEY = ["map-fields-index"] as const;
/** Signed-in lists that matching and the alerts change after a publication (no-store reads):
 * the opportunities and the alerts sent ("Alerts sent", "Recent alerts" on the dashboard). */
const SETTLED_LISTS = new Set(["opportunities", "spot-opportunities", "search-matches", "opportunity", "notifications"]);

/** The poll interval in ms from NEXT_PUBLIC_FORECAST_VERSION_POLL_MS (inlined at build time). */
export function pollIntervalMs(raw: string | undefined = process.env.NEXT_PUBLIC_FORECAST_VERSION_POLL_MS): number {
  const n = raw == null || raw.trim() === "" ? Number.NaN : Number(raw);
  return Number.isFinite(n) ? Math.max(MIN_POLL_MS, Math.round(n)) : DEFAULT_POLL_MS;
}

// --- versioned query keys -----------------------------------------------------------------

/** The last part of a public forecast query key: the version its data belongs to. */
export const versionKey = (v: number | null) => ({ v }) as const;

/** Whether a query key ends with a {@link versionKey}. */
export function isVersionedKey(key: QueryKey): boolean {
  const last = key[key.length - 1];
  return typeof last === "object" && last !== null && !Array.isArray(last) && "v" in last;
}

/** The version a versioned key names (undefined for any other key). */
export function keyVersion(key: QueryKey): number | null | undefined {
  return isVersionedKey(key) ? ((key[key.length - 1] as { v: number | null }).v ?? null) : undefined;
}

/** `key` with its version part set to `v` (a key that is not versioned is returned as is). */
export function withKeyVersion(key: QueryKey, v: number | null): QueryKey {
  return isVersionedKey(key) ? [...key.slice(0, -1), versionKey(v)] : key;
}

/** Two keys that differ at most in their version (the same spot, days, time...). */
export function sameExceptVersion(a: QueryKey, b: QueryKey): boolean {
  if (!isVersionedKey(a) || !isVersionedKey(b) || a.length !== b.length) return false;
  return JSON.stringify(a.slice(0, -1)) === JSON.stringify(b.slice(0, -1));
}

// --- the store ----------------------------------------------------------------------------

export type VersionStatus = "init" | "known" | "unknown";

export interface VersionState {
  /** `init`: waiting for the first answer; `known`: `version` is the API's latest answer;
   * `unknown`: the endpoint failed or did not answer in time (data is read without ?v= until
   * it answers). */
  status: VersionStatus;
  /** The newest version the API named (it can be ahead of `shown` while its data loads). */
  version: ForecastVersion | null;
  /** The version whose data the page shows: the public reads' keys and URLs carry its id,
   * and the "Forecast updated" label names it (null: data read without a version). */
  shown: ForecastVersion | null;
  /** Epoch ms when a version replaced the one this tab showed (null: none so far). */
  changedAt: number | null;
  /** The replacing version whose data is on screen (announced once). */
  renderedId: number | null;
  /** How many versions replaced the one shown since the tab opened. */
  changes: number;
}

const INITIAL: VersionState = { status: "init", version: null, shown: null, changedAt: null, renderedId: null, changes: 0 };
/** Outside a provider (tests, a component rendered alone) and on the server: no version, no wait. */
const DETACHED: VersionState = { ...INITIAL, status: "unknown" };

export interface VersionChange {
  /** The newest answer before `next` (null: the first answer after the endpoint failed or
   * timed out, while the page shows data read without a version). */
  previous: ForecastVersion | null;
  next: ForecastVersion;
  /** Another version_id than the newest one so far (false: only the map run changed). */
  replaced: boolean;
  /** The tab stayed visible since the answer that showed `previous`. */
  continuous: boolean;
  /** {@link ForecastVersionStore.hides} when `next` arrived (a hide since breaks `continuous`). */
  hides: number;
}

export interface ForecastVersionStoreOptions {
  pollMs?: number;
  initTimeoutMs?: number;
  fetchVersion?: (signal: AbortSignal) => Promise<ForecastVersion>;
  now?: () => number;
}

const visible = () => typeof document === "undefined" || document.visibilityState !== "hidden";

const same = (a: ForecastVersion, b: ForecastVersion) =>
  a.version_id === b.version_id && a.published_at === b.published_at && a.map_run_id === b.map_run_id && a.map_cycle === b.map_cycle;

const publishedMs = (v: ForecastVersion) => (v.published_at ? Date.parse(v.published_at) : Number.NaN);

/**
 * Whether `next` is an older answer than `newest`: a lower version id (or none after one), or
 * the same version with an older map run. Ids only grow (each publication is a new row), so
 * such an answer is a stale copy (another CDN edge within its window, another API instance's
 * memo), unless it was published later than `newest` (a database started again from scratch).
 */
export function olderAnswer(next: ForecastVersion, newest: ForecastVersion): boolean {
  const a = versionParam(next.version_id) ?? 0;
  const b = versionParam(newest.version_id) ?? 0;
  if (a !== b) return a < b && !(publishedMs(next) > publishedMs(newest));
  return (next.map_run_id ?? 0) < (newest.map_run_id ?? 0);
}

/** The version id the public query keys and URLs carry in `state`. */
export const keyVersionOf = (state: VersionState): number | null => (state.status === "known" ? versionParam(state.shown?.version_id) : null);

function abortError(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("The read was cancelled.", "AbortError");
}

export class ForecastVersionStore {
  readonly pollMs: number;
  /** Set by the provider: what a new version or map run does to the query cache. */
  onChange: ((change: VersionChange) => void) | null = null;
  /** Set by the provider: an answer that changed nothing (a pending switch tries again). */
  onRepeat: (() => void) | null = null;
  /** How many times the tab was hidden while the store was in use. */
  hides = 0;
  /** The UTC hour (YYYYMMDDHH) the public reads were last re-read for, added to their URLs as
   * `&h=` (null until the first hour change after the page loaded: the URLs stay as they
   * were). See {@link connectStore}. */
  hourParam: number | null = null;
  private state: VersionState = INITIAL;
  private readonly listeners = new Set<() => void>();
  private readonly waiters = new Set<() => void>();
  private readonly initTimeoutMs: number;
  private readonly fetchVersion: (signal: AbortSignal) => Promise<ForecastVersion>;
  private readonly now: () => number;
  private users = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private initTimer: ReturnType<typeof setTimeout> | null = null;
  private inFlight: AbortController | null = null;
  private failures = 0;
  private lastAttempt = Number.NEGATIVE_INFINITY;
  private hiddenSinceAnswer = false;

  constructor(options: ForecastVersionStoreOptions = {}) {
    this.pollMs = Math.max(MIN_POLL_MS, options.pollMs ?? pollIntervalMs());
    this.initTimeoutMs = options.initTimeoutMs ?? INIT_TIMEOUT_MS;
    this.fetchVersion = options.fetchVersion ?? ((signal) => get<ForecastVersion>(FORECAST_VERSION_PATH, signal));
    this.now = options.now ?? Date.now;
  }

  getState = (): VersionState => this.state;

  /** The store's clock (epoch ms). */
  clock = (): number => this.now();

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** A component that shows versioned data mounted: the store polls while any is mounted.
   * Returns the release (call it on unmount). */
  retain = (): (() => void) => {
    this.users += 1;
    if (this.users === 1) this.start();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.users = Math.max(0, this.users - 1);
      if (this.users === 0) this.stop();
    };
  };

  /** Resolves once the first answer arrived, failed or timed out; rejects when `signal` aborts. */
  settled(signal?: AbortSignal): Promise<void> {
    if (this.state.status !== "init") return Promise.resolve();
    return new Promise((resolve, reject) => {
      const done = () => {
        this.waiters.delete(done);
        signal?.removeEventListener("abort", abort);
        resolve();
      };
      const abort = () => {
        this.waiters.delete(done);
        reject(abortError(signal!));
      };
      if (signal?.aborted) return abort();
      this.waiters.add(done);
      signal?.addEventListener("abort", abort, { once: true });
    });
  }

  /** The public reads now show `next` (the provider calls it once they are all read under
   * it); `announce`: it replaces the version shown (false: the first versioned data after the
   * endpoint failed, which may well be the same forecast). */
  show(next: ForecastVersion, announce: boolean): void {
    if (this.state.shown === next) return;
    this.set(announce ? { shown: next, changedAt: this.now(), changes: this.state.changes + 1 } : { shown: next });
  }

  /** The data of version `id` is on screen (the provider calls it after the paint). */
  markRendered(id: number): void {
    if (this.state.renderedId !== id) this.set({ renderedId: id });
  }

  /** Ask now (unless a request is in flight). */
  poll = (): void => {
    if (this.inFlight) return;
    this.clearTimer();
    const controller = new AbortController();
    this.inFlight = controller;
    this.lastAttempt = this.now();
    this.fetchVersion(controller.signal)
      .then(
        (answer) => this.onAnswer(answer),
        () => this.onFailure(),
      )
      .finally(() => {
        if (this.inFlight === controller) this.inFlight = null;
        this.schedule();
      });
  };

  /** Stop everything (tests; the app's store stops when nothing retains it). */
  dispose(): void {
    this.stop();
    this.inFlight?.abort();
    this.inFlight = null;
    if (this.initTimer) clearTimeout(this.initTimer);
    this.initTimer = null;
  }

  private set(patch: Partial<VersionState>) {
    this.state = { ...this.state, ...patch };
    for (const l of [...this.listeners]) l();
  }

  private settle(patch: Partial<VersionState>) {
    if (this.initTimer) clearTimeout(this.initTimer);
    this.initTimer = null;
    this.set(patch);
    for (const w of [...this.waiters]) w();
  }

  private start() {
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", this.onVisibility);
    if (this.state.status === "init") {
      this.initTimer ??= setTimeout(() => {
        if (this.state.status === "init") this.settle({ status: "unknown" });
      }, this.initTimeoutMs);
      // The first answer decides the page's first data reads, so it is read even in a tab
      // opened in the background; polling itself waits until the tab is visible.
      this.hiddenSinceAnswer = !visible();
      this.poll();
      return;
    }
    if (!visible()) return;
    // A page change releases and retains again: do not ask twice within a second.
    const since = this.now() - this.lastAttempt;
    if (this.failures) this.schedule(Math.max(0, this.backoff() - since));
    else if (since < MIN_POLL_MS) this.schedule(MIN_POLL_MS - since);
    else this.poll();
  }

  private stop() {
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", this.onVisibility);
    this.clearTimer();
  }

  private onVisibility = () => {
    if (!visible()) {
      this.hiddenSinceAnswer = true;
      this.hides += 1;
      this.clearTimer();
      return;
    }
    if (this.users === 0 || this.inFlight) return;
    // At once, except while backing off after errors (switching tabs must not hammer it).
    const wait = this.failures ? Math.max(0, this.backoff() - (this.now() - this.lastAttempt)) : 0;
    if (wait > 0) this.schedule(wait);
    else this.poll();
  };

  private backoff(): number {
    return Math.min(MAX_BACKOFF_MS, this.pollMs * 2 ** this.failures);
  }

  private schedule(delay?: number) {
    this.clearTimer();
    if (this.users === 0 || this.inFlight || !visible()) return;
    this.timer = setTimeout(this.poll, delay ?? (this.failures ? this.backoff() : this.pollMs));
  }

  private clearTimer() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private onAnswer(next: ForecastVersion) {
    this.failures = 0;
    const continuous = !this.hiddenSinceAnswer;
    this.hiddenSinceAnswer = !visible();
    const { status, version: previous } = this.state;
    if (status === "init") {
      // The first answer: what the page's first reads are made under, not a change.
      this.settle({ status: "known", version: next, shown: next });
      return;
    }
    if (!previous) {
      // The first answer after the endpoint failed or timed out: the page shows data read
      // without a version, which the provider reads again under this one before showing it.
      this.settle({ status: "known", version: next });
      this.onChange?.({ previous: null, next, replaced: true, continuous: false, hides: this.hides });
      return;
    }
    if (same(previous, next) || olderAnswer(next, previous)) {
      this.onRepeat?.();
      return;
    }
    const replaced = previous.version_id !== next.version_id;
    this.set({ version: next });
    this.onChange?.({ previous, next, replaced, continuous, hides: this.hides });
  }

  private onFailure() {
    this.failures += 1;
    if (this.state.status === "init") this.settle({ status: "unknown" });
  }
}

// --- what a change does to the query cache -------------------------------------------------

const frame = (cb: () => void): (() => void) => {
  if (typeof requestAnimationFrame === "function") {
    const id = requestAnimationFrame(cb);
    return () => cancelAnimationFrame(id);
  }
  const id = setTimeout(cb, 16);
  return () => clearTimeout(id);
};

/**
 * Calls `done` once every active versioned query holds version `id`'s data and the browser
 * painted it (two animation frames); never when one of them fails. Returns the cancel.
 */
export function whenVersionRendered(client: QueryClient, id: number | null, done: () => void): () => void {
  const cache = client.getQueryCache();
  let finished = false;
  let cancelFrame: (() => void) | null = null;
  const stop = () => {
    finished = true;
    unsubscribe();
    clearTimeout(first);
  };
  const check = () => {
    if (finished) return;
    const active = cache.findAll({ type: "active", predicate: (q) => isVersionedKey(q.queryKey) });
    if (active.some((q) => keyVersion(q.queryKey) !== id || q.state.fetchStatus !== "idle" || q.state.status === "pending")) return;
    stop();
    if (active.some((q) => q.state.status === "error")) return;
    cancelFrame = frame(() => (cancelFrame = frame(done)));
  };
  const unsubscribe = cache.subscribe(check);
  // Nothing may change at all (no versioned query on screen).
  const first = setTimeout(check, 0);
  return () => {
    stop();
    cancelFrame?.();
  };
}

/** A failed read worth trying again at the next answer (a server error, a rate limit, a
 * timeout or no network); a 4xx such as a spot that no longer exists is final. */
function retryable(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status >= 500 || error.status === 408 || error.status === 429;
}

/**
 * Reads every public forecast read on screen under version `id` into the query cache (a
 * read already cached and fresh is not read again). Resolves true when the page can switch
 * to `id`: every read succeeded or failed for good (a 4xx, which the page then shows as a
 * first load would); false when one failed in a way worth trying again.
 */
export async function readUnder(client: QueryClient, id: number | null): Promise<boolean> {
  const reads = client
    .getQueryCache()
    .findAll({ type: "active", predicate: (q) => isVersionedKey(q.queryKey) && keyVersion(q.queryKey) !== id })
    .flatMap((q) => {
      const queryFn = q.options.queryFn;
      return typeof queryFn === "function" ? [client.fetchQuery({ queryKey: withKeyVersion(q.queryKey, id), queryFn })] : [];
    });
  const results = await Promise.allSettled(reads);
  return !results.some((r) => r.status === "rejected" && retryable(r.reason));
}

const HOUR_MS = 3_600_000;

/** The UTC hour of `ms` as a number, YYYYMMDDHH (the hour the server computes its public
 * snapshots for: app.services.public_cache). */
export function utcHourParam(ms: number): number {
  const d = new Date(ms);
  return d.getUTCFullYear() * 1_000_000 + (d.getUTCMonth() + 1) * 10_000 + d.getUTCDate() * 100 + d.getUTCHours();
}

/** The provider's reaction to a new version or map run (exported for tests). */
export function connectStore(store: ForecastVersionStore, client: QueryClient, report: (id: number) => void = sendSeenBeacon): () => void {
  const reported = new Set<number>();
  // Staleness that comes from age (a spot not updated for hours) changes without a new
  // version: the server recomputes the public snapshots each UTC hour. When an answer of the
  // poll finds a new hour, the public reads on screen are read again, under URLs that name
  // the hour (`&h=`), so neither the browser nor the CDN hands back last hour's copy.
  //
  // The re-read works like a version switch: the data on screen stays until the new copy
  // arrives, and a read that fails (every open tab re-reads at the top of the hour) neither
  // puts the page in an error state nor waits for the next hour: the next answer tries again.
  let hour = Math.floor(store.clock() / HOUR_MS);
  let hourBusy = false;
  /** The hour of a new version's reads, which name it themselves (no separate re-read). */
  const noteHour = () => {
    const now = store.clock();
    const current = Math.floor(now / HOUR_MS);
    if (current === hour) return;
    hour = current;
    store.hourParam = utcHourParam(now);
  };
  const rereadHour = async () => {
    const now = store.clock();
    const current = Math.floor(now / HOUR_MS);
    // A hidden tab re-reads when it is shown again (the poll answers at once then).
    if (current === hour || hourBusy || !visible()) return;
    hourBusy = true;
    store.hourParam = utcHourParam(now);
    const reads = client
      .getQueryCache()
      .findAll({ type: "active", predicate: (q) => isVersionedKey(q.queryKey) })
      .flatMap((q) => {
        const queryFn = q.options.queryFn;
        return typeof queryFn === "function" ? [client.fetchQuery({ queryKey: q.queryKey, queryFn, staleTime: 0 })] : [];
      });
    const results = await Promise.allSettled(reads);
    hourBusy = false;
    if (!connected) return;
    if (!results.some((r) => r.status === "rejected" && retryable(r.reason))) hour = Math.max(hour, current);
  };
  let cancelRender: (() => void) | null = null;
  let settleTimer: ReturnType<typeof setTimeout> | null = null;
  // The version waiting for its data (a failed read: tried again at the next answer), and
  // the attempt under way (a newer version, or the disconnect, drops an older attempt).
  let pending: VersionChange | null = null;
  let attempt = 0;
  let busy = false;
  let connected = true;

  const switchTo = async (change: VersionChange) => {
    const token = ++attempt;
    pending = change;
    busy = true;
    const id = versionParam(change.next.version_id);
    const ready = await readUnder(client, id).catch(() => false);
    if (token !== attempt || !connected) return;
    busy = false;
    // Keep the previous version on screen (data and label); the next answer tries again.
    if (!ready) return;
    pending = null;
    const announce = change.previous != null;
    store.show(change.next, announce);
    if (!announce || id == null) return;
    cancelRender?.();
    cancelRender = whenVersionRendered(client, id, () => {
      cancelRender = null;
      store.markRendered(id);
      // Only a tab that stayed visible from the previous answer to this paint measures the
      // publication-to-browser latency (a hidden tab paints when it is shown again).
      if (change.continuous && store.hides === change.hides && !reported.has(id)) {
        reported.add(id);
        report(id);
      }
    });
  };

  store.onChange = (change) => {
    const { previous, next, replaced } = change;
    // A new version is read whole under the new hour's URLs (switchTo); any other answer
    // re-reads the data on screen when the hour changed.
    if (replaced) noteHour();
    else void rereadHour();
    if (previous && previous.map_run_id !== next.map_run_id) void client.invalidateQueries({ queryKey: FIELD_INDEX_KEY });
    if (!replaced) return;
    // The new version's public reads are read first; the keys move once they are cached.
    void switchTo(change);
    if (!previous) return;
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      settleTimer = null;
      void client.invalidateQueries({
        predicate: (q) => SETTLED_LISTS.has(String(q.queryKey[0])),
        // A hidden tab reads them when it is used again, not now.
        refetchType: visible() ? "active" : "none",
      });
    }, SETTLE_MS);
  };
  store.onRepeat = () => {
    if (pending && !busy) {
      // The pending version's reads name the current hour themselves.
      noteHour();
      void switchTo(pending);
    } else void rereadHour();
  };
  return () => {
    connected = false;
    attempt += 1;
    store.onChange = null;
    store.onRepeat = null;
    cancelRender?.();
    if (settleTimer) clearTimeout(settleTimer);
  };
}

// --- React --------------------------------------------------------------------------------

const StoreContext = createContext<ForecastVersionStore | null>(null);
const noSubscribe = () => () => undefined;
/** The mounted provider's store, for reads made outside React (the field index URL). */
let activeStore: ForecastVersionStore | null = null;

/** One version store per tab, wired to the query cache (inside QueryClientProvider). */
export function ForecastVersionProvider({ children, store: given }: { children: ReactNode; store?: ForecastVersionStore }) {
  const client = useQueryClient();
  // Polling stops by itself when the last component that retains the store unmounts.
  const [store] = useState(() => given ?? new ForecastVersionStore());
  useEffect(() => {
    activeStore = store;
    const disconnect = connectStore(store, client);
    return () => {
      disconnect();
      if (activeStore === store) activeStore = null;
    };
  }, [store, client]);
  return createElement(StoreContext.Provider, { value: store }, children);
}

/** GET /api/map/fields with the version and map run the tab knows (`?v=..&m=..`): a new map
 * run is then a new URL, so the refetch after it cannot get the CDN's copy of the old index. */
export function fieldIndexPath(): string {
  const state = activeStore?.getState() ?? DETACHED;
  return withParams("/api/map/fields", { v: keyVersionOf(state), m: state.status === "known" ? state.version?.map_run_id : null });
}

/** The version state; keeps the store polling while the caller is mounted. */
export function useForecastVersion(): VersionState {
  const store = useContext(StoreContext);
  const state = useSyncExternalStore(
    store?.subscribe ?? noSubscribe,
    () => store?.getState() ?? DETACHED,
    () => DETACHED,
  );
  useEffect(() => store?.retain(), [store]);
  return state;
}

export interface VersionedRead {
  /** The version id the query keys carry (null: none known). */
  v: number | null;
  /** `path` with the ?v= of `queryKey` (a versioned key: the provider reads a new version
   * under its own key with the same query function). On a first page load it first waits (at
   * most INIT_TIMEOUT_MS) for the version; a read whose key changes meanwhile is cancelled
   * before it fetches. */
  url: (path: string, queryKey: QueryKey, signal: AbortSignal) => Promise<string>;
}

/** For the public forecast hooks: the version for keys and URLs; keeps the store polling. */
export function useVersionedRead(): VersionedRead {
  const store = useContext(StoreContext);
  const v = useSyncExternalStore(
    store?.subscribe ?? noSubscribe,
    () => (store ? keyVersionOf(store.getState()) : null),
    () => null,
  );
  useEffect(() => store?.retain(), [store]);
  return useMemo(
    () => ({
      v,
      url: async (path, queryKey, signal) => {
        // `&h=`: the hour of an hourly re-read (connectStore), only on a versioned URL.
        const at = (v: number | null) => withParams(path, { v, h: v == null ? null : (store?.hourParam ?? null) });
        if (!store || store.getState().status !== "init") return at(keyVersion(queryKey) ?? null);
        await store.settled(signal);
        // Let React re-render with the version first: the key changes and this read is
        // cancelled, so the data is fetched once, under its versioned key and URL.
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (signal.aborted) throw abortError(signal);
        return at(keyVersionOf(store.getState()));
      },
    }),
    [store, v],
  );
}
