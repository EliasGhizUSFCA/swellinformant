"""Internal routes for the platform, not for browsers (foundation contract F8).

``GET /api/internal/cron/daily`` is the daily watchdog Vercel Cron calls with
``Authorization: Bearer $CRON_SECRET``:

* 404 (the same body as an unknown route) when CRON_SECRET is not set;
* 401 when the bearer token does not match (constant-time comparison);
* 200 ``{status, problems, alerts_sent, heartbeat_age_minutes}`` otherwise, with
  ``Cache-Control: no-store``.

The watchdog itself (the data-health check, the daemon heartbeat, admin alerts) is built by
the api-serverless package; until then an authorised call answers 503 with
``status: "unavailable"`` and nothing is checked or sent.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.http_cache import PRIVATE_NO_STORE

CRON_DAILY_PATH = "/api/internal/cron/daily"

router = APIRouter(tags=["internal"], include_in_schema=False)


def _authorize(authorization: str | None) -> None:
    secret = get_settings().cron_secret
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.strip().encode(), secret.encode()
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get(CRON_DAILY_PATH)
def cron_daily(authorization: str | None = Header(default=None)) -> JSONResponse:
    _authorize(authorization)
    return JSONResponse(
        {
            "status": "unavailable",
            "problems": [],
            "alerts_sent": [],
            "heartbeat_age_minutes": None,
            "detail": "The daily watchdog is not implemented yet: nothing was checked.",
        },
        status_code=503,
        headers={"Cache-Control": PRIVATE_NO_STORE},
    )
