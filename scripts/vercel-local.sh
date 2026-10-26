#!/usr/bin/env bash
# The local Vercel-style stack: docker-compose.vercel.yml, docs/local-vercel-stack.md.
#
#   scripts/vercel-local.sh up [--no-build]      build, start and wait until healthy
#   scripts/vercel-local.sh sync [--force] [--only spots,ensemble,map]
#                                                one sync now, in the sync daemon's container
#                                                (waits while the daemon's own sync holds
#                                                the lease, up to VERCEL_LOCAL_SYNC_WAIT_SECONDS)
#   scripts/vercel-local.sh check                the sync plan (provider metadata only)
#   scripts/vercel-local.sh status               containers, images, heartbeat, newest syncs
#   scripts/vercel-local.sh cron                 call the daily watchdog as Vercel Cron does
#   scripts/vercel-local.sh smoke [ARGS]         shell checks, then the Playwright smoke spec
#                                                (ARGS go to Playwright)
#   scripts/vercel-local.sh logs [SERVICE...]    follow the logs
#   scripts/vercel-local.sh psql [--pooled]      psql on the database (direct, or via PgBouncer)
#   scripts/vercel-local.sh bundle [ARGS]        the API bundle size check (scripts/check_api_bundle.py)
#   scripts/vercel-local.sh down [--volumes]     stop; --volumes also deletes the database
#
# Settings come from .env.vercel.local (cp .env.vercel.example .env.vercel.local), or from
# the file VERCEL_LOCAL_ENV_FILE names. Without one the stack runs demo forecasts (synthetic
# data) and no map layers. VERCEL_LOCAL_COMPOSE_FILES adds compose override files
# (space-separated), e.g. for a proxy your builds need.
#
# Needs Docker with Compose v2.24 or newer; smoke also needs curl and the frontend's
# dependencies (cd frontend && npm ci && npx playwright install chromium).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${VERCEL_LOCAL_ENV_FILE:-$ROOT/.env.vercel.local}"
case $ENV_FILE in /*) ;; *) ENV_FILE="$PWD/$ENV_FILE" ;; esac

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

compose() {
  local files=(-f "$ROOT/docker-compose.vercel.yml") extra env_args
  for extra in ${VERCEL_LOCAL_COMPOSE_FILES:-}; do files+=(-f "$extra"); done
  # Always name an env file: without one, compose would read the classic stack's .env.
  if [ -f "$ENV_FILE" ]; then env_args=(--env-file "$ENV_FILE"); else env_args=(--env-file /dev/null); fi
  VERCEL_LOCAL_ENV_FILE="$ENV_FILE" docker compose --project-directory "$ROOT" "${files[@]}" "${env_args[@]}" "$@"
}

# The host port a service's container port is published on (127.0.0.1:PORT -> PORT).
published_port() {
  local addr
  addr="$(compose port "$1" "$2" 2>/dev/null | tail -n 1)"
  [ -n "$addr" ] || { echo "the $1 service is not running (scripts/vercel-local.sh up)" >&2; return 1; }
  echo "${addr##*:}"
}

took() { printf '%s took %d s\n' "$1" "$SECONDS"; }

cmd_up() {
  # A word, never an empty array: macOS's /bin/bash 3.2 treats "${empty[@]}" under set -u
  # as an unbound variable.
  local build=--build
  case "${1:-}" in
    --no-build) build=--no-build ;;
    "") ;;
    *) echo "up: unknown argument $1" >&2; exit 64 ;;
  esac
  if [ -f "$ENV_FILE" ]; then echo "settings: $ENV_FILE"; else echo "settings: none ($ENV_FILE is missing): demo forecasts, no map layers"; fi
  SECONDS=0
  compose up -d "$build" --wait --wait-timeout 900
  local edge mail
  edge="$(published_port edge 8080)"
  mail="$(published_port mailpit 8025)"
  echo
  echo "app      http://localhost:$edge  (edge: /api/* to the api service, the rest to web)"
  echo "inbox    http://localhost:$mail  (Mailpit: every email the app sends)"
  echo "next     scripts/vercel-local.sh sync --force   then   scripts/vercel-local.sh smoke"
  took "up"
}

# `sync run` exits 3 when another sync holds the lease: right after `up` the daemon usually
# runs its own first sync. Waiting is cheap, since run_sync checks the lease before it asks
# any provider; VERCEL_LOCAL_SYNC_WAIT_SECONDS=0 returns at once.
cmd_sync() {
  SECONDS=0
  local rc wait_s="${VERCEL_LOCAL_SYNC_WAIT_SECONDS:-1200}"
  while true; do
    rc=0
    compose exec -T sync python -m app.cli sync run "$@" || rc=$?
    { [ "$rc" -eq 3 ] && [ "$SECONDS" -lt "$wait_s" ]; } || break
    echo "another sync holds the lease (the daemon's own sync); trying again in 15 s" >&2
    sleep 15
  done
  took "sync"
  case $rc in
    0) ;;
    3) echo "another sync still holds the lease after ${wait_s} s: scripts/vercel-local.sh status" >&2 ;;
    *) echo "the sync failed (exit $rc); scripts/vercel-local.sh logs sync" >&2 ;;
  esac
  return "$rc"
}

cmd_check() {
  compose exec -T sync python -m app.cli sync check "$@"
}

cmd_status() {
  compose ps
  echo
  docker image ls --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep '^swell-vercel/' || true
  echo
  compose exec -T sync python -c '
from app.services.sync.state import read_heartbeat
hb = read_heartbeat()
if hb is None:
    print("daemon heartbeat: none yet")
else:
    print(f"daemon heartbeat: {hb.age_seconds():.0f} s ago, runner {hb.runner}, sync due: {hb.due}")
    for reason in hb.reasons:
        print("  -", reason)
'
  compose exec -T sync python -m app.cli sync status --limit "${1:-3}"
  # Upstash bills commands: the stack's Redis counts what the API and the daemon send.
  local commands
  commands="$(compose exec -T redis redis-cli INFO stats | tr -d '\r' | awk -F: '$1 == "total_commands_processed" { print $2 }' || true)"
  [ -z "$commands" ] || echo "redis: $commands commands since the redis container started"
}

cmd_cron() {
  # From the cron container through the edge, as Vercel Cron calls the deployment. The
  # variables expand inside the container.
  # shellcheck disable=SC2016
  compose exec -T cron sh -c 'wget -S -q -O - -T 90 --header "Authorization: Bearer $CRON_SECRET" "http://edge:8080$CRON_PATH"'
  echo
}

cmd_logs() {
  compose logs -f --tail 200 "$@"
}

cmd_psql() {
  case "${1:-}" in
    --pooled) compose exec -e PGPASSWORD=swell postgres psql -h pgbouncer -p 6432 -U swell swell ;;
    "") compose exec postgres psql -U swell swell ;;
    *) echo "psql: unknown argument $1" >&2; exit 64 ;;
  esac
}

cmd_bundle() {
  local py="$ROOT/backend/.venv/bin/python"
  [ -x "$py" ] || py=python3
  "$py" "$ROOT/scripts/check_api_bundle.py" "$@"
}

cmd_down() {
  case "${1:-}" in
    --volumes) compose down --remove-orphans --volumes ;;
    "") compose down --remove-orphans ;;
    *) echo "down: unknown argument $1" >&2; exit 64 ;;
  esac
}

# ----------------------------------------------------------------------------- smoke
FAILURES=0
pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }

# curl to the stack on this machine, never through an HTTP(S)_PROXY of the environment.
curl_local() { curl --noproxy '*' "$@"; }

# The status code of GET $1 with "Authorization: Bearer $2"; the header goes to curl on
# stdin, so the token is not on a command line (ps).
status_with_token() {
  printf 'Authorization: Bearer %s\n' "$2" | curl_local -s -o /dev/null -w '%{http_code}' --max-time 90 -H @- "$1"
}

# psql on PgBouncer's admin console, from the postgres container.
pgbouncer_admin() {
  compose exec -T -e PGPASSWORD=swell postgres psql -h pgbouncer -p 6432 -U swell -X -A -F '|' -P footer=off pgbouncer -c "$1"
}

# total_xact_count of database "swell" in SHOW STATS (columns found by name).
pgbouncer_xacts() {
  pgbouncer_admin "SHOW STATS" | awk -F'|' 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == "total_xact_count") c = i; next } $1 == "swell" { print $c }'
}

cmd_smoke() {
  SECONDS=0
  local edge mail base
  edge="$(published_port edge 8080)"
  mail="$(published_port mailpit 8025)"
  base="http://localhost:$edge"
  echo "smoke: $base"

  if curl_local -fsS --max-time 10 "$base/api/health/ready" >/dev/null; then pass "the API is ready behind the edge"; else fail "GET $base/api/health/ready"; fi

  # The API image is the lean install, like the Vercel function: no heavy dependency, no data/.
  local module
  for module in pandas eccodes celery kombu sklearn pyarrow scipy joblib; do
    if compose exec -T api python -c "import $module" >/dev/null 2>&1; then fail "the api image imports $module"; else pass "the api image cannot import $module"; fi
  done
  if compose exec -T api test -e /data; then fail "the api image has /data"; else pass "the api image has no data/ directory"; fi

  # The api service's Vercel build command (vercel.json buildCommand) on the lean install: a
  # production build migrates over the direct URL (DATABASE_URL_UNPOOLED); here the database
  # is already at head, so it only checks the revision.
  local built
  if built="$(compose exec -T -e VERCEL_ENV=production api python -m app.cli migrate --on-build 2>&1)" \
    && printf '%s' "$built" | grep -q '"status": "migrated"'; then
    pass "the build command 'migrate --on-build' runs on the lean api image (VERCEL_ENV=production)"
  else
    fail "migrate --on-build on the api image: $(printf '%s' "$built" | tail -n 1)"
  fi

  # PgBouncer in transaction mode, and the API's queries go through it.
  local mode before after
  mode="$(pgbouncer_admin "SHOW CONFIG" | awk -F'|' '$1 == "pool_mode" { print $2 }' || true)"
  if [ "$mode" = "transaction" ]; then pass "PgBouncer pool_mode = transaction"; else fail "PgBouncer pool_mode is '$mode'"; fi
  before="$(pgbouncer_xacts || true)"
  curl_local -fsS --max-time 20 "$base/api/spots" >/dev/null || fail "GET /api/spots"
  after="$(pgbouncer_xacts || true)"
  if [ -n "$before" ] && [ -n "$after" ] && [ "$after" -gt "$before" ]; then
    pass "GET /api/spots ran its transactions through PgBouncer ($before -> $after)"
  else
    fail "PgBouncer saw no API transaction (total_xact_count '$before' -> '$after')"
  fi

  # The client IP: a client's own X-Vercel-Forwarded-For, X-Forwarded-For and X-Real-IP never
  # reach the API. The rate-limit counter of GET /api/health/data (bucket health-data) must
  # belong to the TCP peer the edge saw, the Docker network's gateway for a request from this
  # host. The API keys counters rl:<bucket>:<key>:<window> by a keyed digest of the address
  # (app.core.ratelimit.limit_key, with the API's SECRET_KEY), so the expected keys are
  # computed in the api container.
  local network gateway keys expected peer_key forged_keys forged key
  # shellcheck disable=SC2016  # a Go template, not a shell expansion
  network="$(compose ps -q edge | xargs docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{ print $1 }' || true)"
  gateway="$(docker network inspect -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}' "$network" 2>/dev/null | awk '{ print $1 }' || true)"
  compose exec -T redis sh -c 'redis-cli --scan --pattern "rl:health-data:*" | xargs -r redis-cli del' >/dev/null || true
  curl_local -fsS --max-time 20 -o /dev/null \
    -H 'X-Vercel-Forwarded-For: 203.0.113.77' -H 'X-Forwarded-For: 198.51.100.9' -H 'X-Real-IP: 192.0.2.5' \
    "$base/api/health/data" || fail "GET /api/health/data"
  keys="$(compose exec -T redis redis-cli --scan --pattern 'rl:health-data:*' | tr -d '\r' | tr '\n' ' ' || true)"
  expected="$(compose exec -T api python - "${gateway:-none}" 203.0.113.77 198.51.100.9 192.0.2.5 <<'PY' 2>/dev/null || true
import sys

from app.core import ratelimit

for address in sys.argv[1:]:
    print(f"rl:health-data:{ratelimit.limit_key('health-data', address)}:")
PY
)"
  peer_key="$(printf '%s\n' "$expected" | sed -n 1p | tr -d '\r')"
  forged_keys="$(printf '%s\n' "$expected" | sed -n '2,$p' | tr -d '\r')"
  forged=""
  while IFS= read -r key; do
    if [ -n "$key" ] && printf '%s' "$keys" | grep -Fq "$key"; then forged="$forged $key"; fi
  done <<< "$forged_keys"
  if [ -z "$gateway" ] || [ -z "$peer_key" ]; then
    fail "could not work out the edge's peer address or its rate-limit key (network '$network')"
  elif [ -n "$forged" ]; then
    fail "a spoofed client-IP header reached the API (rate-limit key${forged})"
  elif printf '%s' "$keys" | grep -Fq "$peer_key"; then
    pass "spoofed client-IP headers are overwritten by the edge (the counter is the peer $gateway's: ${peer_key%:})"
  else
    fail "no rate-limit counter for the peer address $gateway (keys: '${keys% }')"
  fi

  # Vercel's routing sends /api/* to the API: the web service itself has no /api route.
  local web_api
  web_api="$(compose exec -T edge wget -q -O /dev/null http://web:3000/api/health 2>&1 || true)"
  if printf '%s' "$web_api" | grep -q '404'; then
    pass "the web service answers 404 for /api/health (no Next.js rewrite, API_ROUTING=platform)"
  else
    fail "the web service answered /api/health itself: ${web_api:-200}"
  fi

  # The daemon is alive and a sync was published.
  if [ "$(compose exec -T redis redis-cli EXISTS sync:heartbeat | tr -d '\r')" = "1" ]; then pass "the sync daemon's heartbeat is in Redis"; else fail "no sync:heartbeat in Redis"; fi
  local published
  if published="$(compose exec -T sync python -c '
import sys
from app.core.database import SessionLocal
from app.services.sync.published import latest_publication
with SessionLocal() as db:
    row = latest_publication(db)
    if row is None:
        sys.exit("no published sync")
    print(f"sync {row.id} ({row.trigger}, {row.status}) published {row.published_at:%Y-%m-%d %H:%M} UTC")
' 2>&1)"; then pass "$published"; else fail "$published (run scripts/vercel-local.sh sync --force)"; fi

  # The daily watchdog: 401 without the token, 200 with it.
  local secret code
  secret="$(compose exec -T api printenv CRON_SECRET | tr -d '\r' || true)"
  code="$(curl_local -s -o /dev/null -w '%{http_code}' --max-time 20 "$base/api/internal/cron/daily")"
  if [ "$code" = "401" ]; then pass "cron without a token: 401"; else fail "cron without a token: $code"; fi
  code="$(status_with_token "$base/api/internal/cron/daily" "$secret")"
  if [ "$code" = "200" ]; then pass "cron with the token: 200"; else fail "cron with the token: $code"; fi

  # Not a check of the stack: the data-health verdict of the API (GET /api/health/data), the
  # one the daily watchdog alerts on, as the deployed function would report it.
  local verdict
  verdict="$(curl_local -fsS --max-time 20 "$base/api/health/data" | compose exec -T api python -c '
import json, sys
d = json.load(sys.stdin)
keys = ", ".join(p["key"] for p in d.get("problems") or [])
print(d["status"] + (f" ({keys})" if keys else ""))
' 2>/dev/null | tr -d '\r' || true)"
  printf '  note  data health reported by the API: %s\n' "${verdict:-unknown}"

  local map_fields
  map_fields="$(compose exec -T sync printenv MAP_FIELDS_ENABLED | tr -d '\r' || true)"
  echo "shell checks: $FAILURES failed ($SECONDS s)"

  echo "playwright: frontend/playwright.vercel.config.ts"
  local rc=0
  (
    cd "$ROOT/frontend"
    VERCEL_SMOKE_BASE_URL="$base" \
      VERCEL_SMOKE_MAILPIT_URL="http://localhost:$mail" \
      VERCEL_SMOKE_CRON_SECRET="$secret" \
      VERCEL_SMOKE_MAP_FIELDS="$([ "$map_fields" = "true" ] && echo 1 || echo 0)" \
      npx playwright test -c playwright.vercel.config.ts "$@"
  ) || rc=$?
  took "smoke"
  [ "$FAILURES" -eq 0 ] && [ "$rc" -eq 0 ]
}

command="${1:-}"
[ $# -gt 0 ] && shift
case $command in
  up) cmd_up "$@" ;;
  sync) cmd_sync "$@" ;;
  check) cmd_check "$@" ;;
  status) cmd_status "$@" ;;
  cron) cmd_cron "$@" ;;
  smoke) cmd_smoke "$@" ;;
  logs) cmd_logs "$@" ;;
  psql) cmd_psql "$@" ;;
  bundle) cmd_bundle "$@" ;;
  down) cmd_down "$@" ;;
  -h | --help | help | "") usage ;;
  *) echo "unknown command: $command" >&2; usage >&2; exit 64 ;;
esac
