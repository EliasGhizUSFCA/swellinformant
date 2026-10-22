#!/bin/sh
# Container roles: api | worker | beat | migrate | sync | <any command>
#   sync: the sync daemon (python -m app.cli sync daemon; docs/sync.md), the job runner when
#         JOB_RUNNER=daemon. It replaces the Celery worker and beat: run one, never both.
set -e

# Waits until PostgreSQL and Redis answer, reaching them as the app does (app settings:
# DATABASE_URL normalised, DATABASE_URL_UNPOOLED when DB_USE_UNPOOLED, REDIS_URL or KV_URL).
# An invalid setting stops the container with the setting's name and the reason only:
# pydantic's own message quotes the rejected value, which can be a secret (a CRON_SECRET or
# SECRET_KEY that is too short).
wait_for() {
  python - <<'PY'
import sys, time
import redis
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from app.core.config import get_settings
from app.core.logging import redact

try:
    settings = get_settings()
except ValidationError as exc:
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'settings'}: {error['msg']}"
        for error in exc.errors(include_input=False, include_url=False)
    )
    sys.exit(f"invalid configuration: {problems}")
deadline = time.time() + 90
while True:
    try:
        engine = create_engine(settings.engine_database_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=5, socket_timeout=5
        ).ping()
        break
    except Exception as exc:  # noqa: BLE001
        if time.time() > deadline:
            sys.exit(f"dependencies not ready: {redact(str(exc))}")
        time.sleep(2)
PY
}

case "$1" in
  api)
    wait_for
    if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
      python -m app.cli init-db
    fi
    # --no-proxy-headers: uvicorn would otherwise replace the client address with the
    # left-most X-Forwarded-For entry, which the client writes itself. Client IPs (rate
    # limits, sessions) come from app.core.ratelimit.client_ip and TRUSTED_PROXY_COUNT.
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers --workers "${API_WORKERS:-2}"
    ;;
  worker)
    wait_for
    # WORKER_QUEUES: "celery" (emails, alerts, matching) and "bulk" (data downloads); a
    # worker that consumes only one of them needs a second worker for the other.
    exec celery -A app.workers.celery_app worker --loglevel="${LOG_LEVEL:-INFO}" --concurrency="${WORKER_CONCURRENCY:-2}" --queues="${WORKER_QUEUES:-celery,bulk}"
    ;;
  beat)
    wait_for
    exec celery -A app.workers.celery_app beat --loglevel="${LOG_LEVEL:-INFO}" --schedule=/tmp/celerybeat-schedule
    ;;
  migrate)
    wait_for
    exec python -m app.cli init-db
    ;;
  sync)
    # The daemon runs the jobs itself and sends email and SMS inline: JOB_RUNNER=daemon
    # unless set. The API containers need the same JOB_RUNNER, so that their kicks reach it.
    JOB_RUNNER="${JOB_RUNNER:-daemon}"
    export JOB_RUNNER
    if [ "$JOB_RUNNER" != "daemon" ]; then
      echo "warning: JOB_RUNNER=$JOB_RUNNER: Celery beat also schedules the jobs; run the sync role with JOB_RUNNER=daemon and without worker and beat" >&2
    fi
    wait_for
    # It initialises the database at start (SYNC_DAEMON_INIT_DB, default true) and stops
    # after the current stage on SIGTERM.
    exec python -m app.cli sync daemon
    ;;
  *)
    exec "$@"
    ;;
esac
