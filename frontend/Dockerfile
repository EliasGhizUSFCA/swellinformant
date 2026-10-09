FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# Optional corporate-proxy CA: docker build --secret id=ca,src=/path/to/ca.pem ...
RUN --mount=type=secret,id=ca \
    if [ -f /run/secrets/ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca npm_config_cafile=/run/secrets/ca; fi; \
    npm ci --no-audit --no-fund

FROM node:22-alpine AS build
WORKDIR /app
# The browser only talks to this server; /api/* is proxied to BACKEND_URL (baked at build time).
ARG BACKEND_URL=http://backend:8000
# Map tiles (see lib/map-tiles.ts). Empty = OpenStreetMap. Baked into the bundle and the CSP.
ARG NEXT_PUBLIC_CARTO_API_KEY
ARG NEXT_PUBLIC_MAP_TILE_URL
ARG NEXT_PUBLIC_MAP_ATTRIBUTION
ARG MAP_TILE_HOSTS
ENV BACKEND_URL=${BACKEND_URL} NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_CARTO_API_KEY=${NEXT_PUBLIC_CARTO_API_KEY} \
    NEXT_PUBLIC_MAP_TILE_URL=${NEXT_PUBLIC_MAP_TILE_URL} \
    NEXT_PUBLIC_MAP_ATTRIBUTION=${NEXT_PUBLIC_MAP_ATTRIBUTION} \
    MAP_TILE_HOSTS=${MAP_TILE_HOSTS}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S -g 10001 swell && adduser -S -u 10001 -G swell swell
COPY --from=build --chown=swell:swell /app/.next/standalone ./
COPY --from=build --chown=swell:swell /app/.next/static ./.next/static
COPY --from=build --chown=swell:swell /app/public ./public
USER swell
EXPOSE 3000
CMD ["node", "server.js"]
