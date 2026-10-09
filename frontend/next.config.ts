import type { NextConfig } from "next";

/**
 * The browser only ever talks to this Next.js server. `/api/*` is proxied server-side to
 * the FastAPI backend, so session cookies stay same-origin and no backend URL or secret is
 * ever shipped to the client. BACKEND_URL is read when the server is built/started.
 */
const backendUrl = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "");
const isDev = process.env.NODE_ENV !== "production";
const tileHosts = (process.env.MAP_TILE_HOSTS ?? "https://*.basemaps.cartocdn.com https://tile.openstreetmap.org")
  .split(/\s+/)
  .filter(Boolean)
  .join(" ");

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
