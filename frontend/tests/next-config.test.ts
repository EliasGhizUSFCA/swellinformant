import type { NextConfig } from "next";
import { afterEach, describe, expect, it, vi } from "vitest";

// next.config.ts reads its environment when it is loaded, so each case stubs the variables
// and imports a fresh copy of the module.
const VARIABLES = ["API_ROUTING", "VERCEL", "BACKEND_URL", "API_PROXY_TIMEOUT_MS"] as const;
type Env = Partial<Record<(typeof VARIABLES)[number], string>>;

async function loadConfig(env: Env = {}): Promise<NextConfig> {
  vi.resetModules();
  for (const name of VARIABLES) vi.stubEnv(name, env[name]);
  return (await import("@/next.config")).default;
}

async function rewritesOf(config: NextConfig): Promise<unknown> {
  expect(config.rewrites).toBeTypeOf("function");
  return config.rewrites?.();
}

const PROXY_TO_LOCALHOST = [{ source: "/api/:path*", destination: "http://localhost:8000/api/:path*" }];

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("next.config API_ROUTING", () => {
  it("unset outside Vercel: proxies /api to the localhost backend and builds standalone", async () => {
    const config = await loadConfig();
    expect(await rewritesOf(config)).toEqual(PROXY_TO_LOCALHOST);
    expect(config.output).toBe("standalone");
  });

  it("proxy: rewrites /api to BACKEND_URL (a trailing slash is dropped)", async () => {
    const config = await loadConfig({ API_ROUTING: "proxy", BACKEND_URL: "http://backend:8000/" });
    expect(await rewritesOf(config)).toEqual([
      { source: "/api/:path*", destination: "http://backend:8000/api/:path*" },
    ]);
    expect(config.output).toBe("standalone");
  });

  it("proxy without BACKEND_URL keeps the localhost default", async () => {
    const config = await loadConfig({ API_ROUTING: "proxy" });
    expect(await rewritesOf(config)).toEqual(PROXY_TO_LOCALHOST);
  });

  it("platform: adds no rewrite, so the platform routes /api (local Vercel-style stack)", async () => {
    const config = await loadConfig({ API_ROUTING: "platform", BACKEND_URL: "http://backend:8000" });
    expect(await rewritesOf(config)).toEqual([]);
    // Not on Vercel: the Docker image still needs the standalone server.
    expect(config.output).toBe("standalone");
  });

  it("VERCEL=1 with API_ROUTING unset: platform routing and Vercel's own packaging", async () => {
    const config = await loadConfig({ VERCEL: "1" });
    expect(await rewritesOf(config)).toEqual([]);
    expect(config.output).toBeUndefined();
  });

  it("VERCEL=1 with API_ROUTING=proxy (two-project fallback) proxies to BACKEND_URL", async () => {
    const config = await loadConfig({
      VERCEL: "1",
      API_ROUTING: "proxy",
      BACKEND_URL: "https://swell-api.example.vercel.app",
    });
    expect(await rewritesOf(config)).toEqual([
      { source: "/api/:path*", destination: "https://swell-api.example.vercel.app/api/:path*" },
    ]);
    expect(config.output).toBeUndefined();
  });

  it("warns when proxy routing on Vercel has no BACKEND_URL", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    await loadConfig({ VERCEL: "1", API_ROUTING: "proxy" });
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("API_ROUTING=proxy on Vercel without BACKEND_URL"));
  });

  it("accepts the value case-insensitively and with surrounding spaces", async () => {
    const config = await loadConfig({ API_ROUTING: " Platform " });
    expect(await rewritesOf(config)).toEqual([]);
  });

  it("fails on an unknown value instead of picking a mode", async () => {
    await expect(loadConfig({ API_ROUTING: "edge" })).rejects.toThrow(/API_ROUTING must be "proxy" or "platform"/);
  });

  it("keeps the security headers and CSP identical in both modes", async () => {
    const proxy = await loadConfig({ API_ROUTING: "proxy" });
    const platform = await loadConfig({ API_ROUTING: "platform" });
    expect(proxy.headers).toBeTypeOf("function");
    const proxyHeaders = await proxy.headers?.();
    expect(await platform.headers?.()).toEqual(proxyHeaders);
    const csp = proxyHeaders?.[0]?.headers.find((h) => h.key === "Content-Security-Policy")?.value;
    expect(csp).toContain("connect-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
  });
});
