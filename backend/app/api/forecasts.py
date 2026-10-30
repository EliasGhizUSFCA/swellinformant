"""Forecast versions for browsers (design D11, contract C4; public, no user dependency).

* ``GET /api/forecasts/version``: the published version's small payload, which open tabs
  poll while visible. Answered from this process's memo or Redis
  (app.services.forecasts.serving.version_marker): no database statement while Redis holds
  the marker, one (which seeds Redis again) when it does not. Browsers revalidate every
  time (``Cache-Control: no-cache``); the CDN shares it for FORECAST_VERSION_CDN_SECONDS,
  serving its stale copy as long again while it refreshes, so every open tab of a CDN
  region costs the origin one request per window. The first answer of a version records
  when it was first served (``versions.note_first_served``).
* ``POST /api/forecasts/seen`` ``{"version_id": n}``: a tab rendered a version it learned
  while open (navigator.sendBeacon, so no CSRF header: CSRF-exempt, it touches no account).
  The latency from the version's publication is kept in Redis for the admin dashboard
  (``versions.record_seen``: an unknown or old id is ignored). Always 204 (also for a body
  that is not such an object); rate limited per client IP. Redis only.
* ``GET /api/forecasts/status``: the public freshness summary (the version, each
  component's model run, the stale spots), from the public snapshot cache keyed by version.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import TypeAdapter
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.spots import public_json, version_query
from app.core.config import get_settings
from app.core.database import get_db
from app.core.http_cache import shared_revalidated
from app.core.ratelimit import client_ip, rate_limit
from app.schemas.forecasts import ForecastSeenIn, ForecastStatusOut, ForecastVersionOut
from app.services import public_cache as snapshots
from app.services.forecasts import serving, versions

router = APIRouter(prefix="/api/forecasts", tags=["forecasts"])

# A tab reports each version once; a few tabs behind one address stay far below this.
SEEN_BUCKET = "forecast-seen"
SEEN_LIMIT_PER_HOUR = 60
SEEN_WINDOW_SECONDS = 3600
# {"version_id": 123456789} is about 25 bytes; a longer body is ignored unread.
SEEN_MAX_BODY_BYTES = 256

STATUS = TypeAdapter(ForecastStatusOut)


@router.get("/version", response_model=ForecastVersionOut)
def forecast_version(response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The published forecast version (``version_id`` null: none published yet)."""
    payload = serving.version_marker(db)
    serving.note_served(payload["version_id"])
    seconds = get_settings().forecast_version_cdn_seconds
    shared_revalidated(response, cdn_seconds=seconds, stale_while_revalidate=seconds)
    return payload


async def _bounded_body(request: Request) -> bytes:
    """The request body, or nothing when it is longer than SEEN_MAX_BODY_BYTES."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > SEEN_MAX_BODY_BYTES):
        return b""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > SEEN_MAX_BODY_BYTES:
            return b""
        chunks.append(chunk)
    return b"".join(chunks)


def seen_version_id(body: bytes) -> int | None:
    """The ``version_id`` of a seen report (JSON whatever the content type, as sendBeacon
    sends text/plain for a string), or None when the body is not such an object."""
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return None
    value = data.get("version_id") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


@router.post(
    "/seen",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(rate_limit(SEEN_BUCKET, SEEN_LIMIT_PER_HOUR, SEEN_WINDOW_SECONDS))],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": ForecastSeenIn.model_json_schema()},
                "text/plain": {"schema": ForecastSeenIn.model_json_schema()},
            },
        }
    },
)
async def forecast_seen(request: Request) -> Response:
    """A tab rendered ``version_id`` (see the module docstring)."""
    version_id = seen_version_id(await _bounded_body(request))
    if version_id is not None:
        await run_in_threadpool(versions.record_seen, version_id)
    return Response(status_code=204)


@router.get("/status", response_model=ForecastStatusOut)
def forecast_status(
    request: Request, v: str | None = version_query(), db: Session = Depends(get_db)
) -> Response:
    """The published version, the model run of each component and the stale spots."""
    return public_json(
        snapshots.snapshot(
            db,
            "forecast-status",
            "",
            lambda now: snapshots.render(STATUS, serving.status_payload(db, now)),
            visitor=client_ip(request),
        ),
        request,
    )
