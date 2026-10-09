#!/usr/bin/env bash
# End-to-end test runner: fresh database → backend (demo mode) → production frontend → Playwright.
#
# Prerequisites: PostgreSQL and Redis reachable (e.g. `docker compose up -d postgres redis`),
# backend virtualenv at backend/.venv, frontend dependencies installed (npm ci).
#
#   ./scripts/run-e2e.sh                # uses the defaults below
#   E2E_DATABASE_URL=... ./scripts/run-e2e.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PORT="${E2E_BACKEND_PORT:-8100}"
FRONTEND_PORT="${E2E_FRONTEND_PORT:-3100}"
export DATABASE_URL="${E2E_DATABASE_URL:-postgresql+psycopg://swell:swell@localhost:5432/swell_e2e}"
export REDIS_URL="${E2E_REDIS_URL:-redis://localhost:6379/14}"
export APP_ENV=development FORECAST_PROVIDERS=demo FLIGHT_PROVIDER=demo EMAIL_PROVIDER=console SMS_PROVIDER=console
export ENABLE_DEV_ENDPOINTS=true CELERY_TASK_ALWAYS_EAGER=true DEMO_NATURAL_SWELLS=false RATE_LIMIT_ENABLED=false
export OUTBOX_DIR="$(mktemp -d)"
export APP_BASE_URL="http://127.0.0.1:${FRONTEND_PORT}" API_BASE_URL="http://127.0.0.1:${BACKEND_PORT}"
export CORS_ORIGINS="$APP_BASE_URL"

PIDS=()
cleanup() { for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done; rm -rf "$OUTBOX_DIR"; }
trap cleanup EXIT

PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "▶ preparing database ($DATABASE_URL)"
"$PY" "$ROOT/scripts/reset_database.py"
redis-cli -u "$REDIS_URL" flushdb >/dev/null 2>&1 || "$PY" -c "import redis,os; redis.Redis.from_url(os.environ['REDIS_URL']).flushdb()"
(cd "$ROOT/backend" && "$PY" -m app.cli init-db >/dev/null && "$PY" -m app.cli pipeline >/dev/null)

echo "▶ starting backend on :$BACKEND_PORT"
(cd "$ROOT/backend" && exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --log-level warning) &
PIDS+=($!)

echo "▶ building frontend against the e2e backend"
cd "$ROOT/frontend"
export NEXT_DIST_DIR=".next-e2e" NEXT_TELEMETRY_DISABLED=1
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}" npx next build >/dev/null
rm -rf .next-e2e/standalone/.next-e2e/static .next-e2e/standalone/public
mkdir -p .next-e2e/standalone/.next-e2e && cp -r .next-e2e/static .next-e2e/standalone/.next-e2e/static && cp -r public .next-e2e/standalone/public
(cd .next-e2e/standalone && PORT="$FRONTEND_PORT" HOSTNAME=127.0.0.1 exec node server.js) &
PIDS+=($!)

for url in "http://127.0.0.1:${BACKEND_PORT}/api/health" "http://127.0.0.1:${FRONTEND_PORT}/"; do
  for _ in $(seq 1 60); do curl -fsS "$url" >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS "$url" >/dev/null || { echo "✗ $url did not come up"; exit 1; }
done

echo "▶ running Playwright"
E2E_BASE_URL="http://127.0.0.1:${FRONTEND_PORT}" npx playwright test "$@"
