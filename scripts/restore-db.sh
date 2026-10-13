#!/bin/sh
# Restore a backup made by scripts/backup-db.sh (pg_dump custom format).
#
#   scripts/restore-db.sh backups/swell-swell-20261012T033000Z.dump        # into DATABASE_URL
#   scripts/restore-db.sh --create --target-db swell_restored FILE.dump     # into a new database
#   scripts/restore-db.sh --compose FILE.dump                               # the compose database
#
# Options:
#   --yes             do not ask for confirmation (for scripts and drills)
#   --create          create the target database first; refuses if it already exists
#   --target-db NAME  restore into this database instead of the one named by the connection
#   --compose         restore into the compose "postgres" service (its POSTGRES_DB unless
#                     --target-db is given) through docker compose exec
#
# Without --create, the target must exist. Every object in the archive is dropped and
# re-created (pg_restore --clean --if-exists) in ONE transaction: on any error nothing is
# changed. Objects that are not in the archive are left alone. Stop the app processes that
# write to the target first (docker compose stop backend worker beat), then start them again:
# the backend applies any newer migrations on start (RUN_MIGRATIONS=true).
#
# Without --create, the target and the backup must be at the same Alembic schema revision
# (an older backup over a migrated database would leave the newer tables behind); if they
# differ the script refuses: restore into a new database with --create and swap it in
# (docs/operations.md, "Restore").
#
# Safety: the archive is read (pg_restore --list) and its .sha256 checked, if present, before
# anything is touched; then the target is shown and you must type its name, unless --yes.
# Without a terminal and without --yes the script refuses.
# shellcheck disable=SC2016  # $POSTGRES_* in single quotes expand inside the postgres container
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(dirname -- "$SCRIPT_DIR")
# shellcheck source=SCRIPTDIR/lib/pg-env.sh
. "$SCRIPT_DIR/lib/pg-env.sh"

YES=0
CREATE=0
COMPOSE=0
TARGET_DB=
FILE=
COMPOSE_CMD=${COMPOSE_CMD:-docker compose}

usage() {
  awk 'NR == 1 || /^# shellcheck/ { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

while [ $# -gt 0 ]; do
  case $1 in
    --yes | -y) YES=1; shift ;;
    --create) CREATE=1; shift ;;
    --target-db) TARGET_DB=$2; shift 2 ;;
    --compose) COMPOSE=1; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *)
      [ -z "$FILE" ] || die "only one archive can be restored at a time"
      FILE=$1
      shift
      ;;
  esac
done

[ -n "$FILE" ] || die "no archive given (see --help)"
[ -f "$FILE" ] || die "archive not found: $FILE"
if [ -n "$TARGET_DB" ]; then
  case $TARGET_DB in *[!A-Za-z0-9_]*) die "--target-db may only contain letters, digits and _" ;; esac
fi

compose() {
  # shellcheck disable=SC2086  # COMPOSE_CMD is a command plus arguments on purpose
  (cd "$ROOT" && $COMPOSE_CMD "$@")
}

# ------------------------------------------------------------------ check the archive
if [ -f "$FILE.sha256" ]; then
  expected=$(awk '{print $1}' "$FILE.sha256")
  [ "$(sha256_of "$FILE")" = "$expected" ] || die "checksum mismatch: $FILE does not match $FILE.sha256"
  log "checksum ok"
fi
if [ "$COMPOSE" = 1 ]; then
  HEADER=$(compose exec -T postgres pg_restore --list <"$FILE") || die "not a readable pg_dump archive: $FILE"
else
  command -v pg_restore >/dev/null 2>&1 || die "pg_restore not found (install the PostgreSQL 16 client, or use --compose)"
  HEADER=$(pg_restore --list "$FILE") || die "not a readable pg_dump archive: $FILE"
fi
SOURCE_DB=$(printf '%s\n' "$HEADER" | sed -n 's/^;[[:space:]]*dbname: //p' | head -n 1)
ARCHIVED=$(printf '%s\n' "$HEADER" | sed -n 's/^;[[:space:]]*Archive created at //p' | head -n 1)
ENTRIES=$(printf '%s\n' "$HEADER" | grep -c '^[0-9]' || true)

# ------------------------------------------------------------------ target
if [ "$COMPOSE" = 1 ]; then
  if [ -z "$TARGET_DB" ]; then
    TARGET_DB=$(compose exec -T postgres sh -c 'printf %s "$POSTGRES_DB"') || die "the compose postgres service is not running"
  fi
  TARGET="compose service postgres, database $TARGET_DB"
else
  if [ -n "${DATABASE_URL:-}" ]; then pg_env_from_url "$DATABASE_URL"; fi
  if [ -n "$TARGET_DB" ]; then PGDATABASE=$TARGET_DB; export PGDATABASE; fi
  TARGET_DB=${PGDATABASE:-${PGUSER:-$(id -un)}}
  TARGET=$(describe_target)
fi

log "archive: $FILE (database ${SOURCE_DB:-?}, created ${ARCHIVED:-?}, $ENTRIES entries)"
if [ "$CREATE" = 1 ]; then
  log "target: $TARGET (will be CREATED)"
else
  log "target: $TARGET (every object in the archive will be DROPPED and re-created)"
fi

# ------------------------------------------------------------------ schema revision
# --clean drops and re-creates only what the archive holds. Over a database that later
# migrations have changed, the tables they added stay behind: they block the drop of the
# tables they reference (the restore then fails and changes nothing) or make the next
# `alembic upgrade` fail on "already exists". So an in-place restore needs the same Alembic
# revision on both sides; otherwise restore into a new database and swap it in.
target_sql() {
  if [ "$COMPOSE" = 1 ]; then
    compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$1" -X -A -t -v ON_ERROR_STOP=1 -c "$2"' sh "$TARGET_DB" "$1"
  else
    psql -X -A -t -v ON_ERROR_STOP=1 -d "$TARGET_DB" -c "$1"
  fi
}
archive_revision() {
  _data=$(mktemp)
  if [ "$COMPOSE" = 1 ]; then
    compose exec -T postgres pg_restore --data-only --table=alembic_version --file=- <"$FILE" >"$_data"
  else
    pg_restore --data-only --table=alembic_version --file=- "$FILE" >"$_data"
  fi || { rm -f "$_data"; die "cannot read the schema revision from $FILE"; }
  awk '/^COPY public\.alembic_version /{ on = 1; next } /^\\\.$/{ on = 0 } on' "$_data" | LC_ALL=C sort | paste -sd, -
  rm -f "$_data"
}
if [ "$CREATE" != 1 ]; then
  has_revision=$(target_sql "SELECT to_regclass('public.alembic_version') IS NOT NULL") ||
    die "cannot read the target database ($TARGET): does it exist? --create restores into a new one"
  TARGET_REVISION=
  if [ "$has_revision" = t ]; then
    TARGET_REVISION=$(target_sql "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version") ||
      die "cannot read the schema revision of the target database"
  fi
  ARCHIVE_REVISION=$(archive_revision)
  log "schema revision: backup ${ARCHIVE_REVISION:-none}, target ${TARGET_REVISION:-none}"
  if [ -n "$ARCHIVE_REVISION" ] && [ -n "$TARGET_REVISION" ] && [ "$ARCHIVE_REVISION" != "$TARGET_REVISION" ]; then
    if [ "$COMPOSE" = 1 ]; then how="--compose --create"; else how="--create"; fi
    die "the target database is at schema revision $TARGET_REVISION and the backup at $ARCHIVE_REVISION; a restore over it would leave behind every object the backup does not have. Nothing was changed. Restore into a new database and swap it in instead: $0 $how --target-db NEW_NAME $FILE (docs/operations.md, \"Restore\")"
  fi
fi

if [ "$YES" != 1 ]; then
  [ -t 0 ] || die "refusing to restore without a terminal; pass --yes to confirm non-interactively"
  printf 'Type the target database name (%s) to continue: ' "$TARGET_DB" >&2
  read -r answer
  [ "$answer" = "$TARGET_DB" ] || die "confirmation did not match; nothing was changed"
fi

# ------------------------------------------------------------------ restore
START=$(date +%s)
RESTORE_FLAGS="--no-owner --no-privileges --exit-on-error --single-transaction"
if [ "$CREATE" != 1 ]; then RESTORE_FLAGS="$RESTORE_FLAGS --clean --if-exists"; fi

if [ "$COMPOSE" = 1 ]; then
  if [ "$CREATE" = 1 ]; then
    compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$TARGET_DB"
  fi
  # shellcheck disable=SC2086
  compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$1" '"$RESTORE_FLAGS" sh "$TARGET_DB" <"$FILE"
else
  if [ "$CREATE" = 1 ]; then
    # createdb connects to the maintenance database; PGDATABASE names the new one here.
    createdb --maintenance-db=postgres "$TARGET_DB"
  fi
  # shellcheck disable=SC2086
  pg_restore $RESTORE_FLAGS --dbname="$TARGET_DB" "$FILE"
fi
log "restore complete in $(($(date +%s) - START)) s: $TARGET"
