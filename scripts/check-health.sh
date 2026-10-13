#!/bin/sh
# Check a running stack from outside: API readiness and data health.
#
#   scripts/check-health.sh                          # http://localhost:8000
#   scripts/check-health.sh https://swell.example.com
#   API_URL=http://127.0.0.1:18000 scripts/check-health.sh --quiet
#
# Exit status: 0 ok, 1 degraded, 2 failing, 3 the API is unreachable or not ready.
# Use it from cron or an external monitor: the ops.data_health job emails admins, but a
# stopped worker cannot send its own alert, so something outside the stack must look too.
# Prints the reasons (one per line) unless --quiet. Needs curl; uses no credentials.
set -eu

URL=${API_URL:-http://localhost:8000}
QUIET=0
for arg in "$@"; do
  case $arg in
    --quiet | -q) QUIET=1 ;;
    -h | --help) awk 'NR == 1 || /^# shellcheck/ { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    http://* | https://*) URL=$arg ;;
    *) echo "unknown argument: $arg" >&2; exit 64 ;;
  esac
done
URL=${URL%/}

say() { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }

if ! ready=$(curl -fsS --max-time 10 "$URL/api/health/ready" 2>&1); then
  say "UNREACHABLE: $URL/api/health/ready ($ready)"
  exit 3
fi
if ! body=$(curl -fsS --max-time 20 "$URL/api/health/data" 2>&1); then
  say "UNREACHABLE: $URL/api/health/data ($body)"
  exit 3
fi
status=$(printf '%s' "$body" | sed -n 's/^{"status":"\([a-z]*\)".*/\1/p')
# Best effort (no JSON parser needed): one reason per line; the exit status is what counts.
reasons=$(printf '%s' "$body" | sed -n 's/.*"reasons":\[\([^]]*\)\].*/\1/p' |
  awk '{ gsub(/","/, "\n"); sub(/^"/, ""); sub(/"$/, ""); print }')
case $status in
  ok) say "OK: every data check passes ($URL)"; exit 0 ;;
  degraded) say "DEGRADED ($URL)"; [ -z "$reasons" ] || say "$reasons"; exit 1 ;;
  failing) say "FAILING ($URL)"; [ -z "$reasons" ] || say "$reasons"; exit 2 ;;
  *) say "UNKNOWN response from $URL/api/health/data"; exit 3 ;;
esac
