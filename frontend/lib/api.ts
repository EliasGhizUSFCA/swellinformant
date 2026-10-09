/**
 * Browser API client. All requests go to the same-origin `/api/*` proxy, carry the
 * HttpOnly session cookie automatically, and echo the readable `sta_csrf` cookie in the
 * `X-CSRF-Token` header on state-changing requests (double-submit CSRF protection).
 */
import type { ApiErrorBody } from "@/types/api";

export const CSRF_COOKIE = "sta_csrf";
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

export interface RequestOptions {
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

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.details.length) return error.details.map((d) => d.message).join(" ");
    return error.message;
  }
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}
