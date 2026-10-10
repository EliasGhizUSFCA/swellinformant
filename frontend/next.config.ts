import type { NextConfig } from "next";

import { cartoKeyMissing, tileConfig, tileCspSources } from "./lib/map-tiles";

/**
 * The browser only ever talks to this Next.js server. `/api/*` is proxied server-side to
 * the FastAPI backend, so session cookies stay same-origin and no backend URL or secret is
 * ever shipped to the client. BACKEND_URL is read when the server is built/started.
 */
const backendUrl = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "");
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
  output: "standalone",
  // Separate build directories let the e2e build (pointed at a test backend) coexist with dev.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  poweredByHeader: false,
  // `next dev` blocks dev resources for origins other than localhost; allow the loopback IP too.
  allowedDevOrigins: ["127.0.0.1"],
  reactStrictMode: true,
  async rewrites() {
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
          { key: "Permissions-Policy", value: "geolocation=(), camera=(), microphone=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
