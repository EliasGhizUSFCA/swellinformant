"""The sync block of /api/health/data as /status reads it (Upgrade 4 integration).

* After a daemon sync the heartbeat shows the runs it stored as current (it showed the
  pre-sync plan, "New run", until the next check).
* Each sync lists the runs it stored per component with their cycles, so /status names the
  spot run and the map run separately (a catch-up sync stored GFS 06Z spots with the 00Z
  map while its anchor cycle was 00Z).
* Components and stored runs carry the source's name; the report carries the CPU alert
  share, so the page needs no copies of backend names or constants.

Demo spot forecasts and SYNTHETIC map fields, sync rows and heartbeats.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.core.redis import get_redis
from app.models.sync import SyncRun
from app.services.forecasts.registry import KNOWN_SOURCE_NAMES
from app.services.health import CPU_BUDGET_ALERT_FRACTION, data_health
from app.services.sync import state as sync_state
from app.services.sync.daemon import SyncDaemon
from tests.integration.test_data_health import heartbeat, run, source

pytestmark = pytest.mark.integration

C00 = datetime(2026, 10, 22, 0, tzinfo=UTC)
C06 = datetime(2026, 10, 22, 6, tzinfo=UTC)


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("JOB_RUNNER", "daemon")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_after_a_daemon_sync_the_heartbeat_shows_its_runs_current(
    db: Session, few_spots: list[str], daemon_env: pytest.MonkeyPatch
) -> None:
    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    plan = daemon.check()  # the demo run is new: the check syncs it
    before = {c.source_code: c.state for c in plan.components}
    assert plan.due and before["demo"] == "new"
    beat = sync_state.read_heartbeat()
    assert beat is not None and beat.note is not None and beat.note.endswith("success")
    after = {c["source_code"]: c for c in beat.components}
    assert after["demo"]["state"] == "current"
    assert after["demo"]["stored_run_key"] == after["demo"]["available_run_key"]
    assert after["demo"]["detail"].startswith("stored by sync ")
    # Components the sync did not acquire keep their state (the map and ensemble are off).
    assert {k: c["state"] for k, c in after.items() if k != "demo"} == {
        k: v for k, v in before.items() if k != "demo"
    }
    # The next check (nothing new) agrees with what the heartbeat said.
    daemon.check()
    again = sync_state.read_heartbeat()
    assert again is not None
    assert {c["source_code"]: c["state"] for c in again.components} == {
        k: c["state"] for k, c in after.items()
    }


def test_each_sync_lists_what_it_stored_by_component(
    db: Session, daemon_env: pytest.MonkeyPatch
) -> None:
    run(db, "demo", 1)
    heartbeat(db)
    named = source(db, "open_meteo_noaa")
    named.name = "synthetic GFS via Open-Meteo"
    db.commit()
    now = utcnow()
    spots_key = "gfswave016:2026102206|gfs025:2026102206"
    map_key = "noaa-gfs-fields:2026102200"
    db.add(
        SyncRun(
            trigger="daemon",
            status="success",
            started_at=now - timedelta(minutes=3),
            finished_at=now - timedelta(minutes=1),
            anchor_cycle=C00,
            plan={
                "components": [
                    {
                        "kind": "spots",
                        "source_code": "open_meteo_noaa",
                        "available_run_key": spots_key,
                        "available_cycle": C06.isoformat(),
                    }
                ]
            },
            stages=[
                {
                    "name": "spots",
                    "status": "success",
                    "duration_ms": 40000,
                    "cpu_s": 3.1,
                    "detail": {
                        "components": [
                            {
                                "source_code": "open_meteo_noaa",
                                "run_key": spots_key,
                                "outcome": "stored",
                            },
                            {
                                "source_code": "open_meteo_ecmwf",
                                "run_key": "ecmwf_wam025:2026102200",
                                "outcome": "failed",
                            },
                        ]
                    },
                },
                {
                    "name": "map",
                    "status": "success",
                    "duration_ms": 16000,
                    "cpu_s": 29.2,
                    "detail": {
                        "components": [
                            {
                                "source_code": "noaa_gfs_fields",
                                "run_key": map_key,
                                "outcome": "stored",
                            }
                        ]
                    },
                },
                {"name": "publish", "status": "success", "duration_ms": 50, "cpu_s": 0.01},
            ],
            published_at=now - timedelta(minutes=1),
        )
    )
    db.commit()
    sync_state.write_heartbeat(None, runner="daemon", file_problems=[])

    sync = data_health(db).out.sync
    assert sync is not None and sync.last_published is not None
    stored = [
        (s.kind, s.source, s.source_name, s.run_key, s.cycle) for s in sync.last_published.stored
    ]
    assert stored == [
        ("spots", "open_meteo_noaa", "synthetic GFS via Open-Meteo", spots_key, C06),
        # Not in the plan: the cycle is read from the run key; the name is the known one
        # (no forecast_sources row yet).
        ("map", "noaa_gfs_fields", KNOWN_SOURCE_NAMES["noaa_gfs_fields"], map_key, C00),
    ]
    assert sync.last_published.anchor_cycle == C00  # the coalescing cycle, not the spots'
    assert sync.cpu_alert_fraction == CPU_BUDGET_ALERT_FRACTION == 0.8


def test_components_carry_their_source_names(db: Session, daemon_env: pytest.MonkeyPatch) -> None:
    run(db, "demo", 1)
    heartbeat(db)
    raw = [
        {
            "kind": "map",
            "source_code": "noaa_gfs_fields",
            "provider": "map_fields",
            "state": "current",
            "stored_run_key": "noaa-gfs-fields:2026102200",
            "available_run_key": "noaa-gfs-fields:2026102200",
        },
        {
            "kind": "spots",
            "source_code": "demo",
            "provider": "demo",
            "state": "current",
            "stored_run_key": "demo:synthetic",
            "available_run_key": "demo:synthetic",
        },
        {
            "kind": "spots",
            "source_code": "synthetic_unknown",
            "provider": "synthetic",
            "state": "unknown",
        },
    ]
    payload = {"at": utcnow().isoformat(), "runner": "daemon", "components": raw}
    get_redis().set(sync_state.HEARTBEAT_KEY, json.dumps(payload))
    sync = data_health(db).out.sync
    assert sync is not None
    names = {c.source: c.source_name for c in sync.components}
    assert names == {
        "noaa_gfs_fields": KNOWN_SOURCE_NAMES["noaa_gfs_fields"],
        "demo": "synthetic demo",  # forecast_sources.name of the synthetic row
        "synthetic_unknown": None,
    }
