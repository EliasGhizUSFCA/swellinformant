#!/bin/sh
# Restore drill: back up a database, restore the backup into a new scratch database on the
# same server, and compare exact row counts per table. Exit 0 only when every table matches.
#
#   DATABASE_URL=postgresql://swell:...@db:5432/swell scripts/restore-drill.sh
#   scripts/restore-drill.sh --keep            # keep the scratch database for inspection
#   scripts/restore-drill.sh --archive FILE    # drill an existing backup instead of a new one
#
# Needs pg_dump, pg_restore, psql and createdb/dropdb (PostgreSQL 16 client) and a role that
# may create databases. The scratch database is named <source>_drill_<UTC time> and is
# dropped at the end unless --keep. Backups go to BACKUP_DIR (default ./backups) with the
# prefix "drill", so the regular backups' retention is not affected.
#
# Row counts are exact (count(*)). On a database that is written to while the drill runs,
# tables written meanwhile (e.g. background_job_logs) can differ by those rows: stop the
# worker and beat, or drill a recent copy, for an exact comparison.
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=SCRIPTDIR/lib/pg-env.sh
. "$SCRIPT_DIR/lib/pg-env.sh"

KEEP=0
ARCHIVE=
while [ $# -gt 0 ]; do
  case $1 in
    --keep) KEEP=1; shift ;;
    --archive) ARCHIVE=$2; shift 2 ;;
    -h | --help) awk 'NR == 1 || /^# shellcheck/ { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

if [ -n "${DATABASE_URL:-}" ]; then pg_env_from_url "$DATABASE_URL"; fi
unset DATABASE_URL  # the scripts below use the PG* variables set here
SOURCE_DB=${PGDATABASE:-${PGUSER:-$(id -un)}}
case $SOURCE_DB in *[!A-Za-z0-9_]*) die "source database name must be letters, digits and _" ;; esac
SCRATCH="${SOURCE_DB}_drill_$(date -u +%Y%m%d%H%M%S)"
WORK=$(mktemp -d)

cleanup() {
  rm -rf "$WORK"
  if [ "$KEEP" != 1 ]; then
    dropdb --if-exists --maintenance-db=postgres "$SCRATCH" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

START=$(date +%s)
if [ -z "$ARCHIVE" ]; then
  log "1/4 backing up $(describe_target)"
  ARCHIVE=$(BACKUP_PREFIX=drill BACKUP_KEEP=${BACKUP_KEEP:-3} "$SCRIPT_DIR/backup-db.sh")
fi
log "2/4 counting rows in the source"
row_counts -d "$SOURCE_DB" >"$WORK/source.raw"  # a file, not a pipe: psql errors must stop the drill
LC_ALL=C sort "$WORK/source.raw" >"$WORK/source.txt"

log "3/4 restoring $(basename -- "$ARCHIVE") into $SCRATCH"
RESTORE_START=$(date +%s)
"$SCRIPT_DIR/restore-db.sh" --yes --create --target-db "$SCRATCH" "$ARCHIVE"
RESTORE_SECONDS=$(($(date +%s) - RESTORE_START))

log "4/4 comparing exact row counts per table"
row_counts -d "$SCRATCH" >"$WORK/restored.raw"
LC_ALL=C sort "$WORK/restored.raw" >"$WORK/restored.txt"
TABLES=$(wc -l <"$WORK/source.txt" | tr -d ' ')
ROWS=$(awk '{s += $2} END {print s + 0}' "$WORK/source.txt")
printf '%-34s %12s %12s\n' table source restored
LC_ALL=C join -a 1 -a 2 -e missing -o 0,1.2,2.2 "$WORK/source.txt" "$WORK/restored.txt" |
  awk '{ printf "%-34s %12s %12s%s\n", $1, $2, $3, ($2 == $3 ? "" : "   MISMATCH") }'
if ! cmp -s "$WORK/source.txt" "$WORK/restored.txt"; then
  log "DRILL FAILED: row counts differ (see MISMATCH above)"
  exit 1
fi
SIZE=$(psql -X -A -t -d "$SCRATCH" -c "SELECT pg_size_pretty(pg_database_size(current_database()))")
log "DRILL PASSED: $TABLES tables, $ROWS rows identical; archive $(wc -c <"$ARCHIVE" | tr -d ' ') bytes; restore ${RESTORE_SECONDS} s; restored database $SIZE; total $(($(date +%s) - START)) s"
if [ "$KEEP" = 1 ]; then log "kept scratch database $SCRATCH (drop it with: dropdb $SCRATCH)"; fi
