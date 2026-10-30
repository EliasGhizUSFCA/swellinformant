#!/usr/bin/env bash
# End-to-end test runner: fresh database → backend (demo mode) → production frontend → Playwright.
#
# Prerequisites: PostgreSQL and Redis reachable (e.g. `docker compose up -d postgres redis`),
# backend virtualenv at backend/.venv, frontend dependencies installed (npm ci).
#
#   ./scripts/run-e2e.sh [playwright-test options] [test files]
#   ./scripts/run-e2e.sh                       # every spec, both projects (chromium, mobile)
#   ./scripts/run-e2e.sh --project=chromium e2e/scenario.spec.ts
#   E2E_DATABASE_URL=... E2E_REDIS_URL=... ./scripts/run-e2e.sh
#   E2E_MAILPIT=1 ./scripts/run-e2e.sh         # send every email over SMTP to a throwaway
#                                              # Mailpit container (needs Docker), read its API
#   E2E_DRY_RUN=1 ./scripts/run-e2e.sh ...     # print the settings and arguments, then stop
#   ./scripts/run-e2e.sh -h | --help           # this text; nothing else runs
#
# What a run resets (so point it at scratch resources, never at data you keep):
# * DROPS and recreates the PostgreSQL database of E2E_DATABASE_URL (scripts/reset_database.py
#   refuses names that do not end in _e2e or _test, optionally followed by _<suffix>), then
#   migrates it, loads the reference data (init-db) and runs the demo pipeline;
# * FLUSHDB on the Redis database of E2E_REDIS_URL;
# * rebuilds frontend/.next-e2e and starts the backend and the frontend on the two ports below
#   (stopped again when the script exits).
#
# Environment (default in brackets):
#   E2E_DATABASE_URL     [postgresql+psycopg://swell:swell@localhost:5432/swell_e2e]
#   E2E_REDIS_URL        [redis://localhost:6379/14]
#   E2E_BACKEND_PORT     [8100]   the backend (uvicorn, demo mode)
#   E2E_FRONTEND_PORT    [3100]   the production frontend (next build, standalone server)
#   E2E_FORECAST_VERSION_POLL_MS [2000]  how often open pages ask for the forecast version
#   E2E_CRON_SECRET      [random per run]  the refresh endpoint's bearer token
#   E2E_API_PROXY_TIMEOUT_MS [120000]  the frontend's /api proxy timeout
#   E2E_MAILPIT          [0]      1: a Mailpit container on E2E_MAILPIT_UI_PORT [18125] and
#                                 E2E_MAILPIT_SMTP_PORT [11125]
#   E2E_DRY_RUN          [0]      1: check the arguments, print what would run, touch nothing
#   PLAYWRIGHT_CHROMIUM_PATH      a Chromium binary to use instead of Playwright's own
#
# Every argument goes to `npx playwright test` (run in frontend/). Options are checked against
# the installed Playwright's own list (`npx playwright test --help` in frontend/) before
# anything is reset: an unknown option stops the script with exit status 64. Its -h/--help
# is this text, not Playwright's.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"; }

# --- arguments: checked before anything is reset or started ---------------------------------
for arg in "$@"; do
  case $arg in
    --) break ;;
    -h | --help) usage; exit 0 ;;
  esac
done

PLAYWRIGHT_BIN="$ROOT/frontend/node_modules/.bin/playwright"
# "name kind" per option of `playwright test --help`: kind is "value" (<arg>), "optional"
# ([arg]) or "flag".
playwright_options() {
  "$PLAYWRIGHT_BIN" test --help | awk '
    /^  -/ {
      spec = $0; sub(/^  /, "", spec); sub(/   .*$/, "", spec)
      kind = spec ~ /</ ? "value" : (spec ~ /\[/ ? "optional" : "flag")
      n = split(spec, parts, /[ ,]+/)
      for (i = 1; i <= n; i++) if (parts[i] ~ /^-/) print parts[i], kind
    }'
}
if [ "$#" -gt 0 ]; then
  if [ ! -x "$PLAYWRIGHT_BIN" ]; then
    echo "✗ $PLAYWRIGHT_BIN not found: run 'npm ci' in frontend/ first" >&2
    exit 2
  fi
  KNOWN="$(playwright_options)" || { echo "✗ could not read 'playwright test --help'" >&2; exit 2; }
  [ -n "$KNOWN" ] || { echo "✗ 'playwright test --help' listed no options" >&2; exit 2; }
  skip_value=0
  for arg in "$@"; do
    if [ "$skip_value" = 1 ]; then skip_value=0; continue; fi
    case $arg in
      --) break ;;
      --*=*) name="${arg%%=*}" ;;
      --*) name="$arg" ;;
      -?) name="$arg" ;;
      -*) name="${arg:0:2}" ;;  # a short option with its value attached (-j4, -gfoo)
      *) continue ;;            # a test file filter
    esac
    kind="$(awk -v n="$name" '$1 == n { print $2; exit }' <<<"$KNOWN")"
    if [ -z "$kind" ]; then
      echo "✗ unknown option: $arg (not a 'playwright test' option; see --help)" >&2
      exit 64
    fi
    # The next argument is this option's value: skip it, even if it starts with "-".
    if [ "$kind" = value ] && [ "$arg" = "$name" ]; then skip_value=1; fi
  done
fi

BACKEND_PORT="${E2E_BACKEND_PORT:-8100}"
FRONTEND_PORT="${E2E_FRONTEND_PORT:-3100}"
export DATABASE_URL="${E2E_DATABASE_URL:-postgresql+psycopg://swell:swell@localhost:5432/swell_e2e}"
export REDIS_URL="${E2E_REDIS_URL:-redis://localhost:6379/14}"
if [ "${E2E_DRY_RUN:-0}" = "1" ]; then
  echo "dry run (E2E_DRY_RUN=1): nothing was reset or started. A run would:"
  echo "  drop and recreate the database of $DATABASE_URL"
  echo "  FLUSHDB $REDIS_URL"
  echo "  start the backend on 127.0.0.1:$BACKEND_PORT and the frontend on 127.0.0.1:$FRONTEND_PORT"
  printf '  run: npx playwright test'
  printf ' %q' "$@"
  printf '\n'
  exit 0
fi
export APP_ENV=development WEATHER_PROVIDER=demo FORECAST_PROVIDERS=demo FLIGHT_PROVIDER=demo EMAIL_PROVIDER=console SMS_PROVIDER=console
export ENABLE_DEV_ENDPOINTS=true CELERY_TASK_ALWAYS_EAGER=true DEMO_NATURAL_SWELLS=false RATE_LIMIT_ENABLED=false
OUTBOX_DIR="$(mktemp -d)"  # assigned first, so a mktemp failure stops the script (set -e)
export OUTBOX_DIR
export APP_BASE_URL="http://127.0.0.1:${FRONTEND_PORT}" API_BASE_URL="http://127.0.0.1:${BACKEND_PORT}"
export CORS_ORIGINS="$APP_BASE_URL"
# Open pages ask for the forecast version this often (baked into the build), so the live-update
# specs see a publication within seconds; production defaults to 20 s.
export NEXT_PUBLIC_FORECAST_VERSION_POLL_MS="${E2E_FORECAST_VERSION_POLL_MS:-2000}"
# The scheduler's secret for POST /api/internal/refresh (live-update.spec.ts): random per run.
CRON_SECRET="${E2E_CRON_SECRET:-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')}"
export CRON_SECRET

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
