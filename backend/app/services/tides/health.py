"""Tide freshness for the data-health report (called by app.services.health.data_health).

Each problem is a dict with the keys of ``app.services.health.Problem``: ``key`` (stable
while the problem persists, e.g. "tides_stale:noaa_coops"), ``severity`` ("degraded" or
"failing"), ``message`` (public, plain language) and optionally ``detail`` (admin email
only).

Per provider in TIDE_PROVIDERS, over the stations linked to an active spot: a station whose
stored predictions end less than MIN_AHEAD from now is short. Some short stations are
"degraded", no station of the provider with any future prediction is "failing". A station
the daily job has not tried yet (no predictions, no attempt recorded) is not counted as
short. When no station of a provider was tried :data:`NEVER_ACQUIRED_GRACE` after the
tide catalog was loaded, the provider's tides were never acquired (``tides_missing:<p>``,
"degraded"): the daemon's start-up catch-up and Celery beat's start run the daily job at
once, so only a job that cannot run, or a runner that does not catch up, gets there. With
harmonic_model configured, an unreadable constants file is "failing" on its own
(``tides_constants:harmonic_model``, :func:`constants_problem`).

The constants file (data/tides/, TIDE_CONSTANTS_PATH) ships with the processes that run
the tide jobs: the classic images and the sync daemon's (/data). The Vercel API function
(root backend/) has no data/, so there the data-health report takes the daemon's own
result from its heartbeat instead of reading a file that is not deployed
(app.services.health; :func:`constants_file_present`).
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
# Linked stations of a provider that none of them was tried this long after they were
# loaded: the tides were never acquired.
NEVER_ACQUIRED_GRACE = timedelta(hours=3)
PROVIDER_NAMES = {"noaa_coops": "NOAA CO-OPS", "harmonic_model": "the tide model"}


CONSTANTS_KEY = "tides_constants:harmonic_model"


def constants_problem() -> dict[str, Any] | None:
    """The ``tides_constants:harmonic_model`` problem when this process cannot read the
    tide model's constants file; None when it can or harmonic_model is not configured.

    Without its constants file the seed links no model point, so no station would be
    counted by :func:`tide_freshness` and the model spots' tides would vanish without a
    problem.
    """
    if "harmonic_model" not in get_settings().tide_provider_list:
        return None
    try:
        harmonic_model.load_model()
    except (harmonic_model.ConstantsError, OSError) as exc:
        return {
            "key": CONSTANTS_KEY,
            "severity": "failing",
            "message": "The tide model's constants file cannot be read; "
            "model tides are unavailable.",
            "detail": str(exc)[:2000],
        }
    return None


def constants_file_present() -> bool:
    """Whether the configured constants file exists for this process (a file the API
    function, deployed without data/, does not have)."""
    return harmonic_model.resolve_constants_path().is_file()


def tide_freshness(
    db: Session, now: datetime, *, check_constants: bool = True
) -> list[dict[str, Any]]:
    """Problems with the stored tide predictions (e.g. none left for the coming days), and
    with ``check_constants`` the constants file's (:func:`constants_problem`)."""
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
            TideStation.created_at,
        )
        .outerjoin(TidePrediction, TidePrediction.station_id == TideStation.id)
        .where(TideStation.id.in_(linked), TideStation.provider.in_(providers))
        .group_by(TideStation.id)
    ).all()
    problems: list[dict[str, Any]] = []
    if check_constants and (problem := constants_problem()) is not None:
        problems.append(problem)
    for provider in providers:
        mine = [r for r in rows if r[0] == provider]
        tried = [r for r in mine if r[3] is not None or (r[2] or {}).get("last_error")]
        name = PROVIDER_NAMES.get(provider, provider)
        if mine and not tried:
            loaded = min((r[4] for r in mine if r[4] is not None), default=None)
            if loaded is not None and now - loaded > NEVER_ACQUIRED_GRACE:
                problems.append(
                    {
                        "key": f"tides_missing:{provider}",
                        "severity": "degraded",
                        "message": f"No tide predictions from {name} have been acquired yet.",
                        "detail": (
                            f"{len(mine)} stations linked to active spots, loaded "
                            f"{loaded.isoformat()}; no tides.acquire run stored or tried any"
                        ),
                    }
                )
            continue
        short = [r for r in tried if r[3] is None or r[3] < now + MIN_AHEAD]
        if not short:
            continue
        failing = all(r[3] is None or r[3] <= now for r in tried)
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
