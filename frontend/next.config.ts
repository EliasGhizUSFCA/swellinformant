import type { NextConfig } from "next";

import { cartoKeyMissing, tileConfig, tileCspSources } from "./lib/map-tiles";

/**
 * The browser only ever talks to one origin, so session cookies stay same-origin and no
 * backend URL or secret is ever shipped to the client. How `/api/*` reaches the FastAPI
 * backend is API_ROUTING, read when the server is built/started:
 * - "proxy" (the default, except on Vercel): this Next.js server proxies `/api/*` to
 *   BACKEND_URL (default http://localhost:8000). Classic docker compose, `next dev`, e2e.
 * - "platform" (the default when VERCEL is set, i.e. in Vercel builds): the platform's router
 *   sends `/api/*` to the API before a request reaches Next.js (vercel.json rewrites to the
 *   "api" service; the edge of docker-compose.vercel.yml locally), so no rewrite is added.
 * Any other value fails the build instead of silently picking a mode.
 */
const API_ROUTING_MODES = ["proxy", "platform"] as const;
type ApiRouting = (typeof API_ROUTING_MODES)[number];

const onVercel = Boolean(process.env.VERCEL);
const apiRoutingSetting = process.env.API_ROUTING?.trim().toLowerCase() || (onVercel ? "platform" : "proxy");
if (!(API_ROUTING_MODES as readonly string[]).includes(apiRoutingSetting)) {
  throw new Error(`API_ROUTING must be "proxy" or "platform", not "${process.env.API_ROUTING}".`);
}
const apiRouting = apiRoutingSetting as ApiRouting;
const backendUrl = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "");
if (apiRouting === "proxy" && onVercel && !process.env.BACKEND_URL) {
  console.warn(
    "⚠ API_ROUTING=proxy on Vercel without BACKEND_URL: /api/* would be proxied to http://localhost:8000. " +
      "Set BACKEND_URL to the API's URL, or leave API_ROUTING unset (platform) when vercel.json routes /api.",
  );
}
// How long the /api proxy waits for the backend (ms); unset = Next.js's 30 s. The e2e runner
// raises it: its developer endpoints run the whole forecast pipeline inside one request.
const proxyTimeout = Number(process.env.API_PROXY_TIMEOUT_MS) || undefined;
const isDev = process.env.NODE_ENV !== "production";
// The map tile host is derived from NEXT_PUBLIC_MAP_TILE_URL so a custom provider is never
// CSP-blocked; MAP_TILE_HOSTS adds more sources. Both are read at build time.
const tileHosts = tileCspSources(
  process.env.NEXT_PUBLIC_MAP_TILE_URL,
  process.env.MAP_TILE_HOSTS,
  process.env.NEXT_PUBLIC_CARTO_API_KEY,
).join(" ");
const tileUrl = tileConfig(process.env.NEXT_PUBLIC_MAP_TILE_URL, undefined, process.env.NEXT_PUBLIC_CARTO_API_KEY).url;
if (cartoKeyMissing(tileUrl)) {
  console.warn(
    "⚠ NEXT_PUBLIC_MAP_TILE_URL points at CARTO without ?key=…; CARTO will serve 'API KEY REQUIRED' tiles. " +
      "Set NEXT_PUBLIC_CARTO_API_KEY or leave the URL empty for OpenStreetMap.",
  );
}

const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  `img-src 'self' data: blob: ${tileHosts}`,
  "font-src 'self' data:",
  `connect-src 'self'${isDev ? " ws: wss:" : ""}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const nextConfig: NextConfig = {
  // The standalone server bundle is for the Docker image (frontend/Dockerfile) and the e2e
  // runner; Vercel's Next.js builder packages the app into its own functions instead.
  ...(onVercel ? {} : { output: "standalone" as const }),
  // Separate build directories let the e2e build (pointed at a test backend) coexist with dev.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  poweredByHeader: false,
  // `next dev` (16.4) writes an AGENTS.md rules block when it detects a coding agent; the repo
  // keeps its own docs, so the generated file is off (and ignored in .gitignore).
  agentRules: false,
  // `next dev` blocks dev resources for origins other than localhost; allow the loopback IP too.
  allowedDevOrigins: ["127.0.0.1"],
  reactStrictMode: true,
  ...(proxyTimeout ? { experimental: { proxyTimeout } } : {}),
  async rewrites() {
    if (apiRouting === "platform") return [];
    return [{ source: "/api/:path*", destination: `${backendUrl}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: csp },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "geolocation=(self), camera=(), microphone=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
