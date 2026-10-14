#!/usr/bin/env bash
# End-to-end test runner: fresh database → backend (demo mode) → production frontend → Playwright.
#
# Prerequisites: PostgreSQL and Redis reachable (e.g. `docker compose up -d postgres redis`),
# backend virtualenv at backend/.venv, frontend dependencies installed (npm ci).
#
#   ./scripts/run-e2e.sh                # uses the defaults below
#   E2E_DATABASE_URL=... ./scripts/run-e2e.sh
#   E2E_MAILPIT=1 ./scripts/run-e2e.sh  # send every email over SMTP to a throwaway Mailpit
#                                       # container (needs Docker) and read it from its API
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
MAILPIT_CONTAINER=""
cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  rm -rf "$OUTBOX_DIR"
  if [ -n "$MAILPIT_CONTAINER" ]; then docker stop "$MAILPIT_CONTAINER" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

if [ "${E2E_MAILPIT:-0}" = "1" ]; then
  MAILPIT_UI_PORT="${E2E_MAILPIT_UI_PORT:-18125}"
  MAILPIT_SMTP_PORT="${E2E_MAILPIT_SMTP_PORT:-11125}"
  echo "▶ starting Mailpit (UI :$MAILPIT_UI_PORT, SMTP :$MAILPIT_SMTP_PORT)"
  MAILPIT_CONTAINER="$(docker run -d --rm -p "127.0.0.1:${MAILPIT_UI_PORT}:8025" -p "127.0.0.1:${MAILPIT_SMTP_PORT}:1025" axllent/mailpit:v1.27)"
  export EMAIL_PROVIDER=mailpit SMTP_HOST=127.0.0.1 SMTP_PORT="$MAILPIT_SMTP_PORT"
  export DEV_INBOX_URL="http://127.0.0.1:${MAILPIT_UI_PORT}" E2E_MAILPIT_URL="http://127.0.0.1:${MAILPIT_UI_PORT}"
  for _ in $(seq 1 30); do curl -fsS "$E2E_MAILPIT_URL/api/v1/info" >/dev/null 2>&1 && break; sleep 1; done
fi

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
# /api/dev/simulate-swell and run-pipeline run the whole pipeline inside the request (about
# 30 s on a 4-CPU machine): give the /api proxy more than Next.js's default 30 s.
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}" API_PROXY_TIMEOUT_MS="${E2E_API_PROXY_TIMEOUT_MS:-120000}" npx next build >/dev/null
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
