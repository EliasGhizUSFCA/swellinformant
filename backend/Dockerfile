# One image for the API, the Celery worker and the Celery beat scheduler.
# Build context: repository root (needs backend/ and data/).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY backend/pyproject.toml ./
# Install dependencies only (cached layer); the app itself runs from /app via PYTHONPATH so
# alembic.ini and migrations resolve relative to the source tree.
# Optional: behind a TLS-intercepting proxy, pass its CA bundle with
#   docker build --secret id=ca,src=/path/to/ca.pem ...
RUN --mount=type=secret,id=ca \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    mkdir -p app && touch app/__init__.py && pip install . && pip uninstall -y swell-travel-agent \
    && rm -rf app build *.egg-info

COPY backend/ ./
COPY data/ /data/
RUN useradd --create-home --uid 10001 swell \
    && mkdir -p /tmp/swell-outbox && chown swell /tmp/swell-outbox \
    && chmod +x /app/docker-entrypoint.sh

USER swell
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["api"]
