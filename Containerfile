# The sync daemon image (docs/local-vercel-stack.md, docs/sync.md).
#
# Vercel builds this file for the daemon "sync" (vercel.json: daemons.sync, root "./",
# entrypoint "Containerfile"); docker-compose.vercel.yml builds the same file for its "sync"
# and "migrate" services. Build context: the repository root (needs backend/ and data/).
#
#   docker build -f Containerfile -t swell-sync .
#
# Vercel passes every environment variable of the deployment to the image build as a build
# argument, so this file declares no ARG: no secret can reach a layer. The secrets arrive at
# run time as environment variables.
#
# Installs the API dependencies plus the "sync" extra (pandas, ecCodes, pyarrow,
# scikit-learn, joblib) from backend/uv.lock; no Celery. The app runs from /app (PYTHONPATH)
# so alembic.ini and the migrations resolve as in the source tree; data/ goes to /data.

FROM python:3.13.15-slim-trixie AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src
# uv.loc[k]: the lock is optional for this COPY (the glob may match nothing), and the RUN
# below says so loudly when it is missing.
COPY backend/pyproject.toml backend/uv.loc[k] ./
# Optional: behind a TLS-intercepting proxy, pass its CA bundle with
#   docker build --secret id=ca,src=/path/to/ca.pem ...
RUN --mount=type=secret,id=ca \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca; fi; \
    pip install uv==0.11.32 \
    && if [ -f uv.lock ]; then \
         uv sync --locked --no-dev --extra sync --no-install-project; \
       else \
         echo "WARNING: backend/uv.lock is missing; installing .[sync] without a lock" >&2; \
         uv venv /opt/venv \
         && uv pip install --python /opt/venv/bin/python ".[sync]" \
         && uv pip uninstall --python /opt/venv/bin/python swell-travel-agent; \
       fi \
    && pip uninstall -y uv \
    && /opt/venv/bin/python -c "import eccodes; print('ecCodes', eccodes.codes_get_api_version())"
# The last line fails the build when the ecCodes C library is missing: the map layers are
# GRIB2 files, and the "eccodes" package alone does not ship the library (the eccodeslib
# wheel does).

FROM python:3.13.15-slim-trixie

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_USE_UNPOOLED=true \
    JOB_RUNNER=daemon

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY backend/pyproject.toml backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY backend/ml_models ./ml_models
COPY data/ /data/

RUN useradd --create-home --uid 10001 swell \
    && mkdir -p /tmp/swell-outbox /tmp/swell-ml \
    && chown swell /tmp/swell-outbox /tmp/swell-ml

USER swell
# SIGTERM (a rollout, `docker stop`) stops the daemon after the current sync stage; Vercel
# waits terminationGracePeriodSeconds (vercel.json) before it kills the process.
CMD ["python", "-m", "app.cli", "sync", "daemon"]
