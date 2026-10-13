# Shared helpers for the database scripts (POSIX sh; sourced, not executed).
#
# pg_env_from_url URL   Export PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE/PGSSLMODE from a
#                       postgresql:// URL (also the app's postgresql+psycopg:// form), so the
#                       password never appears on a command line (`ps` shows arguments, not
#                       the environment). Percent-encoded characters are decoded. IPv6 hosts
#                       must be in brackets: postgresql://u:p@[::1]:5432/db.
# describe_target       "user@host:port/db" for messages (never the password).
# log MSG / die MSG     Timestamped messages on stderr.

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

urldecode() {
  printf '%s' "$1" | LC_ALL=C awk '
    BEGIN { for (i = 1; i < 256; i++) hex[sprintf("%02X", i)] = sprintf("%c", i) }
    {
      s = $0; out = ""
      while (match(s, /%[0-9A-Fa-f][0-9A-Fa-f]/)) {
        out = out substr(s, 1, RSTART - 1) hex[toupper(substr(s, RSTART + 1, 2))]
        s = substr(s, RSTART + 3)
      }
      printf "%s", out s
    }'
}

pg_env_from_url() {
  _url=$1
  case $_url in
    postgres://* | postgresql://* | postgresql+*://*) ;;
    *) die "DATABASE_URL must start with postgresql:// (got another scheme)" ;;
  esac
  _rest=${_url#*://}
  _query=
  case $_rest in *\?*)
    _query=${_rest#*\?}
    _rest=${_rest%%\?*}
    ;;
  esac
  _userinfo=
  case $_rest in *@*)
    _userinfo=${_rest%@*}
    _rest=${_rest##*@}
    ;;
  esac
  _hostport=${_rest%%/*}
  _db=
  case $_rest in */*) _db=${_rest#*/} ;; esac
  _user=${_userinfo%%:*}
  _pass=
  case $_userinfo in *:*) _pass=${_userinfo#*:} ;; esac
  _host=$_hostport
  _port=
  case $_hostport in
    \[*\]:*)
      _host=${_hostport%%]:*}
      _host=${_host#\[}
      _port=${_hostport##*]:}
      ;;
    \[*\])
      _host=${_hostport#\[}
      _host=${_host%\]}
      ;;
    *:*)
      _host=${_hostport%:*}
      _port=${_hostport##*:}
      ;;
  esac
  if [ -n "$_host" ]; then PGHOST=$(urldecode "$_host"); export PGHOST; fi
  if [ -n "$_port" ]; then PGPORT=$_port; export PGPORT; fi
  if [ -n "$_user" ]; then PGUSER=$(urldecode "$_user"); export PGUSER; fi
  if [ -n "$_pass" ]; then PGPASSWORD=$(urldecode "$_pass"); export PGPASSWORD; fi
  if [ -n "$_db" ]; then PGDATABASE=$(urldecode "$_db"); export PGDATABASE; fi
  if [ -n "$_query" ]; then
    _old_ifs=$IFS
    set -f
    IFS='&'
    for _kv in $_query; do
      case $_kv in sslmode=*)
        PGSSLMODE=${_kv#sslmode=}
        export PGSSLMODE
        ;;
      esac
    done
    set +f
    IFS=$_old_ifs
  fi
  unset _url _rest _query _userinfo _hostport _db _user _pass _host _port _kv _old_ifs
}

describe_target() {
  printf '%s@%s:%s/%s' "${PGUSER:-$(id -un)}" "${PGHOST:-local socket}" "${PGPORT:-5432}" "${PGDATABASE:-${PGUSER:-$(id -un)}}"
}

# Exact row count of every table in the public schema, one "table count" line each.
# Uses the current PG* connection settings; extra psql arguments (e.g. -d NAME) are passed on.
row_counts() {
  psql -X -A -t -F ' ' -v ON_ERROR_STOP=1 "$@" -c "
    SELECT table_name,
           (xpath('/row/c/text()',
                  query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
                               false, true, '')))[1]::text::bigint
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}
