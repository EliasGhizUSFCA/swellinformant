#!/bin/sh
# Writes PgBouncer's runtime configuration from the environment, then runs it in the
# foreground (logs on stderr). Used by docker/pgbouncer/Dockerfile.
#
#   PGBOUNCER_UPSTREAM_HOST  Postgres host (default postgres)
#   PGBOUNCER_UPSTREAM_PORT  Postgres port (default 5432)
#   PGBOUNCER_USER           the one user clients log in as (required; letters, digits, _ . -)
#   PGBOUNCER_PASSWORD       its password (required); PgBouncer uses it for SCRAM with the
#                            clients and with Postgres
set -eu

: "${PGBOUNCER_USER:?PGBOUNCER_USER is required}"
: "${PGBOUNCER_PASSWORD:?PGBOUNCER_PASSWORD is required}"
host=${PGBOUNCER_UPSTREAM_HOST:-postgres}
port=${PGBOUNCER_UPSTREAM_PORT:-5432}

fail() {
  echo "pgbouncer-entrypoint: $*" >&2
  exit 64
}

# The values go into PgBouncer's own file formats, which have no escaping for these.
case $PGBOUNCER_USER in
  *[!A-Za-z0-9_.-]*) fail "PGBOUNCER_USER may hold only letters, digits, _ . and -" ;;
esac
case $PGBOUNCER_PASSWORD in
  *'"'* | *' '* | *'
'*) fail "PGBOUNCER_PASSWORD may not hold double quotes, spaces or newlines" ;;
esac
case $host$port in
  *[!A-Za-z0-9_.:-]*) fail "PGBOUNCER_UPSTREAM_HOST/PORT hold unexpected characters" ;;
esac

dir=/tmp/pgbouncer
mkdir -p "$dir"
umask 077
sed "s/@ADMIN_USER@/$PGBOUNCER_USER/g" /etc/pgbouncer/pgbouncer.ini > "$dir/pgbouncer.ini"
printf '"%s" "%s"\n' "$PGBOUNCER_USER" "$PGBOUNCER_PASSWORD" > "$dir/userlist.txt"
printf '[databases]\n* = host=%s port=%s\n' "$host" "$port" > "$dir/databases.ini"

exec pgbouncer "$dir/pgbouncer.ini"
