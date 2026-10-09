#!/bin/sh
# Container roles: api | worker | beat | migrate | <any command>
set -e

wait_for() {
  python - <<'PY'
import os, sys, time
from sqlalchemy import create_engine, text
import redis
deadline = time.time() + 90
while True:
    try:
        create_engine(os.environ["DATABASE_URL"]).connect().execute(text("SELECT 1"))
        redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0")).ping()
        break
    except Exception as exc:  # noqa: BLE001
        if time.time() > deadline:
            sys.exit(f"dependencies not ready: {exc}")
        time.sleep(2)
PY
}

case "$1" in
  api)
    wait_for
    if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
      python -m app.cli init-db
    fi
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips="*" --workers "${API_WORKERS:-2}"
    ;;
  worker)
    wait_for
    exec celery -A app.workers.celery_app worker --loglevel="${LOG_LEVEL:-INFO}" --concurrency="${WORKER_CONCURRENCY:-2}"
    ;;
  beat)
    wait_for
    exec celery -A app.workers.celery_app beat --loglevel="${LOG_LEVEL:-INFO}" --schedule=/tmp/celerybeat-schedule
    ;;
  migrate)
    wait_for
    exec python -m app.cli init-db
    ;;
  *)
    exec "$@"
    ;;
esac
