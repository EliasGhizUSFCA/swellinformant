# The classic docker compose image: one image for the API, the Celery worker, Celery beat,
# migrations and the sync daemon (docker-entrypoint.sh roles api | worker | beat | migrate |
# sync). Build context: the repository root (needs backend/ and data/).
#
# Installs [project].dependencies plus the "all" extra (worker: Celery; server:
# uvicorn[standard]; sync: pandas, ecCodes, pyarrow, scikit-learn, joblib) from backend/uv.lock
# into /opt/venv. `uv sync --locked` fails the build when uv.lock does not match
# pyproject.toml (run `uv lock` in backend/ after changing the dependencies). Python 3.13, as
# backend/.python-version, CI and the Vercel function use. The hosted images install less: the
# API only [project].dependencies (docker/api-lean, the Vercel builder), the sync daemon
# [project].dependencies plus "sync" (the repo-root Containerfile).

FROM python:3.13.15-slim-trixie AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
# Optional: behind a TLS-intercepting proxy, pass its CA bundle with
#   docker build --secret id=ca,src=/path/to/ca.pem ...
# The last command fails the build when an extra is missing or the ecCodes C library is not
# installed (the "eccodes" package alone does not ship it; the eccodeslib wheel does).
RUN --mount=type=secret,id=ca \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca; fi; \
    pip install uv==0.11.32 \
    && uv sync --locked --no-dev --extra all --no-install-project \
    && pip uninstall -y uv \
    && /opt/venv/bin/python -c "import celery, pandas, pyarrow, sklearn, joblib, uvloop, eccodes; print('ecCodes', eccodes.codes_get_api_version())"

FROM python:3.13.15-slim-trixie

# The app runs from /app (PYTHONPATH) so alembic.ini and the migrations resolve as in the
# source tree; data/ goes to /data.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY backend/ ./
COPY data/ /data/
RUN useradd --create-home --uid 10001 swell \
    && mkdir -p /tmp/swell-outbox /var/lib/swell-ml \
    && chown swell /tmp/swell-outbox /var/lib/swell-ml \
    && chmod +x /app/docker-entrypoint.sh

USER swell
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["api"]
