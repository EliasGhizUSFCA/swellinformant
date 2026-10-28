/**
 * Browser API client. All requests go to the same-origin `/api/*` proxy, carry the
 * HttpOnly session cookie automatically, and echo the readable `sta_csrf` cookie in the
 * `X-CSRF-Token` header on state-changing requests (double-submit CSRF protection).
 */
import type { ApiErrorBody, ForecastVersionOut } from "@/types/api";

const CSRF_COOKIE = "sta_csrf";
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class ApiError extends Error {
  status: number;
  code: string;
  details: Array<{ field: string; message: string }>;

  constructor(status: number, code: string, message: string, details: ApiError["details"] = []) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** Field-level messages keyed by the last path segment (e.g. "travel.max_price" → "max_price"). */
  fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const d of this.details) {
      const key = d.field.split(".").pop() || "_";
      out[key] = d.message;
    }
    return out;
  }
}

export function readCookie(name: string, source: string = typeof document === "undefined" ? "" : document.cookie): string | null {
  for (const part of source.split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}

let csrfPromise: Promise<string> | null = null;

async function ensureCsrf(): Promise<string> {
  const existing = readCookie(CSRF_COOKIE);
  if (existing) return existing;
  csrfPromise ??= fetch("/api/auth/csrf", { credentials: "same-origin" })
    .then((r) => r.json() as Promise<{ csrf_token: string }>)
    .then((d) => d.csrf_token)
    .finally(() => {
      csrfPromise = null;
    });
  return csrfPromise;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
}

export async function api<T>(path: string, { method = "GET", body, signal }: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method)) headers["X-CSRF-Token"] = await ensureCsrf();
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    signal,
  });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown;
  try {
    data = text ? (JSON.parse(text) as unknown) : undefined;
  } catch {
    data = undefined; // e.g. the proxy's plain-text error page when the API is down
  }
  if (!res.ok) {
    const err = (data as ApiErrorBody | undefined)?.error;
    const fallback =
      res.status >= 500
        ? "The service is temporarily unavailable. Please try again in a moment."
        : `Request failed (${res.status})`;
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? fallback, err?.details ?? []);
  }
  if (text && data === undefined) throw new ApiError(res.status, "bad_response", "Unexpected response from the server.", []);
  return data as T;
}

export const get = <T>(path: string, signal?: AbortSignal) => api<T>(path, { signal });
export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body });
export const put = <T>(path: string, body?: unknown) => api<T>(path, { method: "PUT", body });
export const patch = <T>(path: string, body?: unknown) => api<T>(path, { method: "PATCH", body });
export const del = <T>(path: string, body?: unknown) => api<T>(path, { method: "DELETE", body });

// --- forecast versions (design D11, contract C4) --------------------------------------------

/** GET /api/forecasts/version (contract C4): the published forecast version, the generated
 * ForecastVersionOut. `version_id` null = nothing published yet (the data served then comes
 * from the run selection used before versions existed); `map_run_id` is the published
 * map-layer run (its own pointer: a change means new ocean layers). Times are ISO 8601 UTC. */
export type ForecastVersion = ForecastVersionOut;

export const FORECAST_VERSION_PATH = "/api/forecasts/version";
export const FORECAST_SEEN_PATH = "/api/forecasts/seen";

/** An id usable as `?v=`: a positive integer, else null (0, null and NaN mean "none published",
 * and the API treats them as such). */
export function versionParam(id: number | null | undefined): number | null {
  return typeof id === "number" && Number.isSafeInteger(id) && id > 0 ? id : null;
}

/** `path` with each positive-integer param of `params` appended (`?v=12`, `&m=7`); the rest
 * are left out, so without a known version the path is unchanged. The public forecast GETs
 * take `v` (the version shown): a new publication is then a new URL for the browser cache
 * and the CDN, and a `v` the server does not serve is answered with no-store headers. */
export function withParams(path: string, params: Record<string, number | null | undefined>): string {
  let out = path;
  for (const [name, value] of Object.entries(params)) {
    const v = versionParam(value);
    if (v != null) out += `${out.includes("?") ? "&" : "?"}${name}=${v}`;
  }
  return out;
}

export const withVersion = (path: string, versionId: number | null | undefined) => withParams(path, { v: versionId });

/** Tells the API this tab rendered forecast version `versionId` (publication-to-browser
 * latency, admin dashboard). navigator.sendBeacon (a JSON string goes as text/plain, which the
 * endpoint reads; no CSRF header, the route is exempt), else a keepalive fetch. Never throws;
 * returns whether the browser accepted the report. */
export function sendSeenBeacon(versionId: number): boolean {
  const body = JSON.stringify({ version_id: versionId });
  try {
    if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
      if (navigator.sendBeacon(FORECAST_SEEN_PATH, body)) return true;
    }
    if (typeof fetch === "function") {
      void fetch(FORECAST_SEEN_PATH, { method: "POST", body, keepalive: true, credentials: "same-origin", headers: { "Content-Type": "text/plain" } }).catch(() => undefined);
      return true;
    }
  } catch {
    // A report is best effort: the page works the same without it.
  }
  return false;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.details.length) return error.details.map((d) => d.message).join(" ");
    return error.message;
  }
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}
