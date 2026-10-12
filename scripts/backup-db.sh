#!/bin/sh
# Back up the PostgreSQL database with pg_dump (custom format), then apply retention.
#
#   scripts/backup-db.sh                    # DATABASE_URL (or libpq PG* variables) -> ./backups
#   scripts/backup-db.sh --compose          # the compose "postgres" service (docker compose exec)
#   BACKUP_DIR=/srv/swell-backups BACKUP_KEEP=14 BACKUP_MAX_AGE_DAYS=30 scripts/backup-db.sh
#
# Options (environment variable in brackets):
#   --dir DIR            where backups go [BACKUP_DIR, default ./backups]
#   --keep N             keep at most the N newest backups, 0 = no count limit [BACKUP_KEEP, 14]
#   --max-age-days D     delete backups older than D days, 0 = no age limit
#                        [BACKUP_MAX_AGE_DAYS, 30]
#   --prefix NAME        file name prefix [BACKUP_PREFIX, swell]
#   --compose            dump from the compose postgres service; no local pg_dump needed
#                        [COMPOSE_CMD, default "docker compose"; COMPOSE_FILE etc. are honoured]
#
# Each backup is DIR/PREFIX-DB-YYYYMMDDTHHMMSSZ.dump plus a .sha256 file. It is written to a
# .partial file and only renamed once pg_dump succeeded and `pg_restore --list` can read the
# archive, so a failed run never leaves a file that looks like a backup. Retention only
# considers files of the same prefix and database and never deletes the backup just made.
# Restore with scripts/restore-db.sh; rehearse with scripts/restore-drill.sh.
#
# Credentials: the DATABASE_URL password is passed to pg_dump through PGPASSWORD, never on
# the command line. Nothing secret is logged.
# shellcheck disable=SC2016  # $POSTGRES_* in single quotes expand inside the postgres container
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(dirname -- "$SCRIPT_DIR")
# shellcheck source=SCRIPTDIR/lib/pg-env.sh
. "$SCRIPT_DIR/lib/pg-env.sh"

DIR=${BACKUP_DIR:-./backups}
KEEP=${BACKUP_KEEP:-14}
MAX_AGE_DAYS=${BACKUP_MAX_AGE_DAYS:-30}
PREFIX=${BACKUP_PREFIX:-swell}
COMPOSE=0
COMPOSE_CMD=${COMPOSE_CMD:-docker compose}

usage() {
  awk 'NR == 1 || /^# shellcheck/ { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

while [ $# -gt 0 ]; do
  case $1 in
    --dir) DIR=$2; shift 2 ;;
    --keep) KEEP=$2; shift 2 ;;
    --max-age-days) MAX_AGE_DAYS=$2; shift 2 ;;
    --prefix) PREFIX=$2; shift 2 ;;
    --compose) COMPOSE=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

case $KEEP in '' | *[!0-9]*) die "--keep must be a whole number" ;; esac
case $MAX_AGE_DAYS in '' | *[!0-9]*) die "--max-age-days must be a whole number" ;; esac
case $PREFIX in '' | *[!A-Za-z0-9._-]*) die "--prefix may only contain letters, digits, . _ -" ;; esac

compose() {
  # shellcheck disable=SC2086  # COMPOSE_CMD is a command plus arguments on purpose
  (cd "$ROOT" && $COMPOSE_CMD "$@")
}

if [ "$COMPOSE" = 1 ]; then
  DB=$(compose exec -T postgres sh -c 'printf %s "$POSTGRES_DB"') || die "the compose postgres service is not running"
  TARGET="compose service postgres, database $DB"
else
  if [ -n "${DATABASE_URL:-}" ]; then pg_env_from_url "$DATABASE_URL"; fi
  command -v pg_dump >/dev/null 2>&1 || die "pg_dump not found (install the PostgreSQL 16 client, or use --compose)"
  DB=${PGDATABASE:-${PGUSER:-$(id -un)}}
  TARGET=$(describe_target)
fi
case $DB in '' | *[!A-Za-z0-9._-]*) SAFE_DB=database ;; *) SAFE_DB=$DB ;; esac

# Backups hold every account (emails, password hashes): readable by their owner only.
umask 077
mkdir -p "$DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE="$DIR/$PREFIX-$SAFE_DB-$STAMP.dump"
PARTIAL="$FILE.partial"
trap 'rm -f "$PARTIAL"' EXIT INT TERM

log "backing up $TARGET -> $FILE"
START=$(date +%s)
if [ "$COMPOSE" = 1 ]; then
  compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' >"$PARTIAL"
  compose exec -T postgres pg_restore --list >/dev/null <"$PARTIAL" || die "the new archive cannot be read back"
else
  pg_dump --format=custom --file="$PARTIAL"
  pg_restore --list "$PARTIAL" >/dev/null || die "the new archive cannot be read back"
fi
mv "$PARTIAL" "$FILE"
trap - EXIT INT TERM
printf '%s  %s\n' "$(sha256_of "$FILE")" "$(basename -- "$FILE")" >"$FILE.sha256"
BYTES=$(wc -c <"$FILE" | tr -d ' ')
log "backup complete: $BYTES bytes in $(($(date +%s) - START)) s"

# ------------------------------------------------------------------ retention
PATTERN="$PREFIX-$SAFE_DB-*.dump"
count() { find "$DIR" -maxdepth 1 -type f -name "$PATTERN" | wc -l | tr -d ' '; }
remove() {
  while IFS= read -r old; do
    [ "$old" = "$FILE" ] && continue
    rm -f "$old" "$old.sha256"
    log "retention: removed $(basename -- "$old")"
  done
}
before=$(count)
if [ "$MAX_AGE_DAYS" -gt 0 ]; then
  find "$DIR" -maxdepth 1 -type f -name "$PATTERN" -mtime +"$MAX_AGE_DAYS" | remove
fi
if [ "$KEEP" -gt 0 ]; then
  # Newest first: the UTC timestamp in the name sorts chronologically.
  find "$DIR" -maxdepth 1 -type f -name "$PATTERN" | sort -r | awk -v keep="$KEEP" 'NR > keep' | remove
fi
after=$(count)
log "retention: $((before - after)) removed, $after kept (keep $KEEP, max age $MAX_AGE_DAYS days)"
printf '%s\n' "$FILE"
