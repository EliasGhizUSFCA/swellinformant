"""The sync cadence of the free-database profile (round 2 of the Upgrade 4 egress
integration; docs/map-layers.md, "Free database budget").

On Neon Free the profile syncs only for a new map cycle (SYNC_TRIGGER_SOURCES=map) of the
06Z and 18Z cycles (MAP_FIELD_CYCLES=06,18): two syncs a day, each acquiring the newest spot
runs of both families with the map layers. The full-retention profile restores a sync for
every new model run. The runs below are SYNTHETIC test inputs of tests/unit/test_sync_detect.py
(made-up run keys and cycles), planned without a database as the daemon's check does.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import Settings
from app.core.profiles import FREE_DATABASE_PROFILE, FULL_RETENTION_PROFILE, profile_env
from app.services.sync.detect import plan_sync
from tests.unit.test_sync_detect import (  # noqa: F401 (world is a fixture)
    C06,
    C18,
    ECMWF,
    GFS,
    MAP,
    World,
    comp,
    ecmwf_run,
    everything_current,
    gfs_run,
    map_run,
    settings,
    state,
    stored_06,
    world,  # noqa: F401
)

T18 = C18 + timedelta(hours=5, minutes=20)  # Open-Meteo's GFS waves of 18Z appear ~5.3 h on


def _hosted_free() -> Settings:
    env = {k.lower(): v for k, v in profile_env("hosted", "free-database").items()}
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_the_free_database_profile_syncs_for_the_06z_and_18z_map_cycles_only() -> None:
    s = _hosted_free()
    assert s.sync_trigger_list == frozenset({"map"})
    assert s.map_field_cycle_hours == [6, 18]
    assert s.map_fields_enabled is True  # the hosted profile's map layers, the trigger
    # Restored after upgrading the database: a sync for every new model run.
    assert FULL_RETENTION_PROFILE["SYNC_TRIGGER_SOURCES"] == "spots,map"
    assert FULL_RETENTION_PROFILE["MAP_FIELD_CYCLES"] == "00,12"
    assert set(FREE_DATABASE_PROFILE) == set(FULL_RETENTION_PROFILE)


def free(**update: object) -> Settings:
    return settings(sync_trigger_sources="map", map_field_cycles="06,18", **update)


def test_a_new_spot_run_alone_waits_for_the_next_map_cycle(world: World) -> None:  # noqa: F811
    everything_current(world)
    world.runs[ECMWF] = ecmwf_run(C06 + timedelta(hours=6))  # the 12Z ECMWF run
    k = stored_06()
    plan = plan_sync(None, settings=free(), now=T18 - timedelta(hours=4), known=k)
    assert not plan.due and state(plan, ECMWF) == "new"
    assert any(f"{ECMWF}: new run" in r and "(not a trigger)" in r for r in plan.reasons)
    # The same run with spot runs as a trigger (the full-retention cadence) syncs at once.
    assert plan_sync(None, settings=settings(map_field_cycles="06,18"), now=T18, known=k).due


def test_a_map_cycle_brings_the_newest_spot_runs_of_both_families(world: World) -> None:  # noqa: F811
    everything_current(world)
    world.runs[ECMWF] = ecmwf_run(C06 + timedelta(hours=6))  # waiting since its 12Z run
    world.runs[GFS] = gfs_run(C18)
    k = stored_06()
    # The 18Z GFS run is the spot run of a map cycle: it waits for the map, no trigger.
    early = plan_sync(None, settings=free(), now=T18, known=k)
    assert not early.due and state(early, GFS) == "waiting"
    world.runs[MAP] = map_run(C18)
    plan = plan_sync(None, settings=free(), now=T18 + timedelta(minutes=10), known=k)
    assert plan.due and plan.anchor_cycle == C18
    assert plan.due_kinds() == frozenset({"spots", "map"})
    assert state(plan, GFS) == state(plan, MAP) == state(plan, ECMWF) == "new"
    assert comp(plan, ECMWF).available_run_key == ecmwf_run(C06 + timedelta(hours=6)).run_key


def test_00z_and_12z_are_not_map_cycles_of_the_free_profile(world: World) -> None:  # noqa: F811
    everything_current(world)
    world.runs[GFS] = gfs_run(C06 + timedelta(hours=6))  # 12Z: not a map cycle here
    k = stored_06()
    plan = plan_sync(None, settings=free(), now=T18 - timedelta(hours=1), known=k)
    assert not plan.due and plan.anchor_cycle is None
    assert state(plan, GFS) == "new"  # not anchored to a map cycle, and not a trigger
