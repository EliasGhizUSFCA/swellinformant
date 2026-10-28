"""Admin freshness dashboard API (spec section 16, design D12, contract C5).

``GET /api/admin/freshness``: every section-16 metric for admin accounts (the session
cookie of a user with ``is_admin``; 401 signed out, 403 for other users). Always
``Cache-Control: private, no-store``: the body carries redacted error text and quota
counters, so neither the CDN nor the browser keeps it. Read-only; a bounded number of
indexed queries and Redis reads (app.services.admin_freshness), never a provider call.
The public /status page and /api/health/data stay as they are.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.database import get_db
from app.core.errors import Forbidden
from app.core.http_cache import PRIVATE_NO_STORE
from app.models import User
from app.schemas.admin import AdminFreshnessOut
from app.services.admin_freshness import freshness_report

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise Forbidden("The freshness dashboard is for admin accounts.")
    return user


@router.get("/freshness", response_model=AdminFreshnessOut)
def admin_freshness(
    response: Response,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminFreshnessOut:
    """Forecast freshness, provider and API usage, processing times and latencies, refresh
    jobs and the admin problems, for the admin dashboard (/admin/freshness)."""
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return freshness_report(db)
