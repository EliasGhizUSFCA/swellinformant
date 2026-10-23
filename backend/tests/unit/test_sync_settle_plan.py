"""What the daemon's heartbeat says after a sync (Upgrade 4 integration).

The daemon rewrote its heartbeat with the plan from before the sync, so a run the sync had
just stored read "New run" on /status until the next check. :func:`settle_plan` gives the
components as the sync left them. Run keys and outcomes are SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.services.forecasts.ensemble import GefsWaveEnsembleProvider
from app.services.forecasts.noaa import NoaaGfsFieldsProvider
from app.services.forecasts.registry import GEFS_WAVE_CODE, KNOWN_SOURCE_NAMES
from app.services.sync.detect import settle_plan
from app.services.sync.types import ComponentStatus, StageResult, SyncPlan, SyncResult

T0 = datetime(2026, 10, 22, 15, 3, tzinfo=UTC)
C00, C06 = datetime(2026, 10, 22, 0, tzinfo=UTC), datetime(2026, 10, 22, 6, tzinfo=UTC)


def comp(kind: Any, source: str, state: Any, stored: str | None, available: str) -> ComponentStatus:
    return ComponentStatus(
        kind=kind,
        source_code=source,
        provider=source,
        state=state,
        stored_run_key=stored,
        available_run_key=available,
        available_cycle=C06 if "06" in available else C00,
        detail="waited 90 min for noaa_gfs_fields 2026-10-22 06Z" if state == "new" else None,
        attempts=2 if state == "new" else 0,
    )


def result(*components: dict[str, Any], plan: SyncPlan) -> SyncResult:
    stage = StageResult("spots", "success", T0, 10, 0.1, {"components": list(components)})
    return SyncResult(5, "partial", plan, [stage], T0)


def test_stored_runs_are_current_and_the_rest_says_what_happened() -> None:
    plan = SyncPlan(
        checked_at=T0,
        due=True,
        components=(
            comp("spots", "open_meteo_noaa", "new", "gfs:2026102200", "gfs:2026102206"),
            comp("spots", "open_meteo_ecmwf", "new", "ecmwf:2026102112", "ecmwf:2026102200"),
            comp("map", "noaa_gfs_fields", "new", "fields:2026102118", "fields:2026102200"),
            comp("ensemble", GEFS_WAVE_CODE, "disabled", None, "gefs:2026102200"),
            comp("spots", "open_meteo_other", "new", None, "other:2026102200"),
        ),
        anchor_cycle=C00,
        reasons=("synthetic",),
    )
    settled = settle_plan(
        plan,
        result(
            {"source_code": "open_meteo_noaa", "run_key": "gfs:2026102206", "outcome": "stored"},
            {
                "source_code": "open_meteo_ecmwf",
                "run_key": "ecmwf:2026102200",
                "outcome": "failed",
                "message": "ConnectTimeout: synthetic",
            },
            {
                "source_code": "noaa_gfs_fields",
                "run_key": "fields:2026102200",
                "outcome": "current",
            },
            {
                "source_code": "open_meteo_other",
                "run_key": "other:2026102200",
                "outcome": "deferred",
            },
            plan=plan,
        ),
    )
    by = {c.source_code: c for c in settled.components}
    # The check's decision stays (it was due: the heartbeat's note names the sync).
    assert settled.due and settled.reasons == plan.reasons and settled.anchor_cycle == C00
    gfs = by["open_meteo_noaa"]
    assert (gfs.state, gfs.stored_run_key, gfs.detail) == (
        "current",
        "gfs:2026102206",
        "stored by sync 5",
    )
    assert gfs.attempts == 0 and gfs.available_run_key == "gfs:2026102206"
    assert by["noaa_gfs_fields"].state == "current" and by["noaa_gfs_fields"].detail is None
    assert by["noaa_gfs_fields"].stored_run_key == "fields:2026102200"
    ecmwf = by["open_meteo_ecmwf"]
    assert ecmwf.state == "failed" and ecmwf.stored_run_key == "ecmwf:2026102112"
    assert ecmwf.detail == "sync 5 failed: ConnectTimeout: synthetic"
    assert by["open_meteo_other"].state == "waiting"
    assert by[GEFS_WAVE_CODE] == plan.components[3]  # not acquired: unchanged


def test_known_source_names_are_the_providers_own() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    fields = NoaaGfsFieldsProvider(settings, client=httpx.Client())
    gefs = GefsWaveEnsembleProvider(settings, client=httpx.Client())
    assert {
        fields.code: fields.source_info().name,
        gefs.code: gefs.source_info().name,
    } == KNOWN_SOURCE_NAMES
