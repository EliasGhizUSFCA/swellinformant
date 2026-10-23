# The API as Vercel runs it (service "api", root backend/, entrypoint app.main:app), for the
# local Vercel-style stack (docker-compose.vercel.yml). Build context: the repository root.
#
# Like the Vercel Python builder it installs only [project].dependencies, the lean set (no
# Celery, pandas, ecCodes, pyarrow, scikit-learn, scipy or joblib), from backend/uv.lock. Like
# the function bundle it holds backend/ without what vercel.json's excludeFiles leaves out
# (tests/, ml_models/) and has no data/ directory, so an API path that needs a heavy
# dependency or a file of data/ fails here as it would on Vercel.
# `scripts/vercel-local.sh smoke` checks that the heavy imports fail in this image.

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
# The versions come from backend/uv.lock only (the COPY fails without it; `uv sync --locked`
# fails when it no longer matches pyproject.toml), as the Vercel builder installs them. The
# last command fails the build when a heavy dependency slipped into [project].dependencies.
COPY backend/pyproject.toml backend/uv.lock ./
# Optional: behind a TLS-intercepting proxy, pass its CA bundle with
#   docker build --secret id=ca,src=/path/to/ca.pem ...
RUN --mount=type=secret,id=ca \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca; fi; \
    pip install uv==0.11.32 \
    && uv sync --locked --no-dev --no-install-project \
    && pip uninstall -y uv \
    && /opt/venv/bin/python -c "import importlib.util as u, sys; heavy = [m for m in ('celery', 'kombu', 'pandas', 'eccodes', 'pyarrow', 'sklearn', 'scipy', 'joblib') if u.find_spec(m)]; print('lean install') if not heavy else sys.exit('not lean, installed: ' + ', '.join(heavy))"

FROM python:3.13.15-slim-trixie

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY backend/pyproject.toml backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app

RUN useradd --create-home --uid 10001 swell
USER swell
EXPOSE 8000
# One process, as one function instance. --no-proxy-headers: client IPs come from
# CLIENT_IP_HEADER (x-vercel-forwarded-for, set by the edge), never from uvicorn's reading of
# the client-writable X-Forwarded-For.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
