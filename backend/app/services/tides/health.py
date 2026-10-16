"""Tide freshness for the data-health report (called by app.services.health.data_health).

Each problem is a dict with the keys of ``app.services.health.Problem``: ``key`` (stable
while the problem persists, e.g. "tides_stale:noaa_coops"), ``severity`` ("degraded" or
"failing"), ``message`` (public, plain language) and optionally ``detail`` (admin email
only).

Per provider in TIDE_PROVIDERS, over the stations linked to an active spot: a station whose
stored predictions end less than MIN_AHEAD from now is short. Some short stations are
"degraded", no station of the provider with any future prediction is "failing". A station
the daily job has not tried yet (no predictions, no attempt recorded) is not counted: a new
deployment is covered by the job schedule check (tides.acquire overdue) instead. With
harmonic_model configured, an unreadable constants file is "failing" on its own
(``tides_constants:harmonic_model``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import SpotTideStation, SurfSpot, TidePrediction, TideStation
from app.services.tides import harmonic_model

# The daily job stores TIDE_HORIZON_DAYS + 1 day; two days left means it missed two runs.
MIN_AHEAD = timedelta(days=2)
PROVIDER_NAMES = {"noaa_coops": "NOAA CO-OPS", "harmonic_model": "the tide model"}


def tide_freshness(db: Session, now: datetime) -> list[dict[str, Any]]:
    """Problems with the stored tide predictions (e.g. none left for the coming days)."""
    providers = get_settings().tide_provider_list
    if not providers:
        return []
    linked = (
        select(SpotTideStation.station_id)
        .join(SurfSpot, SurfSpot.id == SpotTideStation.spot_id)
        .where(SurfSpot.is_active)
    )
    rows = db.execute(
        select(
            TideStation.provider,
            TideStation.code,
            TideStation.metadata_,
            func.max(TidePrediction.valid_time),
        )
        .outerjoin(TidePrediction, TidePrediction.station_id == TideStation.id)
        .where(TideStation.id.in_(linked), TideStation.provider.in_(providers))
        .group_by(TideStation.id)
    ).all()
    problems: list[dict[str, Any]] = []
    if "harmonic_model" in providers:
        # Without its constants file the seed links no model point, so no station would be
        # counted below and the model spots' tides would vanish without a problem.
        try:
            harmonic_model.load_model()
        except harmonic_model.ConstantsError as exc:
            problems.append(
                {
                    "key": "tides_constants:harmonic_model",
                    "severity": "failing",
                    "message": "The tide model's constants file cannot be read; "
                    "model tides are unavailable.",
                    "detail": str(exc)[:2000],
                }
            )
    for provider in providers:
        mine = [r for r in rows if r[0] == provider]
        tried = [r for r in mine if r[3] is not None or (r[2] or {}).get("last_error")]
        short = [r for r in tried if r[3] is None or r[3] < now + MIN_AHEAD]
        if not short:
            continue
        failing = all(r[3] is None or r[3] <= now for r in tried)
        name = PROVIDER_NAMES.get(provider, provider)
        detail = "; ".join(
            f"{r[1]}: last prediction {r[3].isoformat() if r[3] else 'none'}"
            + (
                f", last error: {(r[2] or {}).get('last_error')}"
                if (r[2] or {}).get("last_error")
                else ""
            )
            for r in short
        )
        problems.append(
            {
                "key": f"tides_stale:{provider}",
                "severity": "failing" if failing else "degraded",
                "message": (
                    f"No future tide predictions from {name}."
                    if failing
                    else f"Tide predictions from {name} run out within 2 days at "
                    f"{len(short)} of {len(mine)} stations."
                ),
                "detail": detail[:2000],
            }
        )
    return problems
