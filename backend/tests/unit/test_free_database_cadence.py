"""The sync cadence of the free-database profile and the map-cycle cadence (round 2 of the
Upgrade 4 egress integration; docs/map-layers.md, "Free database budget").

Swell 2.0 (design B, spec section 9): the free-database profile syncs for every new spot
model run (SYNC_TRIGGER_SOURCES=spots) and keeps the 06Z and 18Z map cycles
(MAP_FIELD_CYCLES=06,18) for the optional overlay. The Upgrade 4 cadence it replaced, a sync
only for a new map cycle (SYNC_TRIGGER_SOURCES=map: two syncs a day, each acquiring the
newest spot runs of both families with the map layers), stays available and is tested
below with that setting. The runs below are SYNTHETIC test inputs of
tests/unit/test_sync_detect.py (made-up run keys and cycles), planned without a database as
the daemon's check does.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.core.config import Settings
from app.core.profiles import FREE_DATABASE_PROFILE, FULL_RETENTION_PROFILE, profile_env
from app.services.sync import detect
from app.services.sync.detect import Probe, plan_sync
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
    world,
)

T18 = C18 + timedelta(hours=5, minutes=20)  # Open-Meteo's GFS waves of 18Z appear ~5.3 h on


def _hosted_free() -> Settings:
    env = {k.lower(): v for k, v in profile_env("hosted", "free-database").items()}
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_the_free_database_profile_syncs_for_every_spot_run_and_keeps_two_map_cycles() -> None:
    # Swell 2.0 changed this profile from SYNC_TRIGGER_SOURCES=map (Upgrade 4: a sync only
    # for a new map cycle) to spots: the website follows every new spot model run.
    s = _hosted_free()
    assert s.sync_trigger_list == frozenset({"spots"})
    assert s.map_field_cycle_hours == [6, 18]
    assert s.map_fields_enabled is True  # the hosted profile's optional map overlay
    # Restored after upgrading the database: a sync for every new model run.
    assert FULL_RETENTION_PROFILE["SYNC_TRIGGER_SOURCES"] == "spots,map"
    assert FULL_RETENTION_PROFILE["MAP_FIELD_CYCLES"] == "00,12"
    assert set(FREE_DATABASE_PROFILE) == set(FULL_RETENTION_PROFILE)


def free(**update: object) -> Settings:
    """The map-cycle cadence (the Upgrade 4 free-database profile, still available)."""
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
    # The 18Z GFS run is new but no trigger here: it is taken by the next map sync. (Swell
    # 2.0 removed the spot/map coalescing wait, design D6: it no longer waits for the map.)
    early = plan_sync(None, settings=free(), now=T18, known=k)
    assert not early.due and state(early, GFS) == "new"
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


def test_without_a_map_component_spot_runs_trigger_the_syncs(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SYNC_TRIGGER_SOURCES=map while the map layers are off (MAP_FIELDS_ENABLED=false) or come
    # with the noaa_gfs spot run: the map component is disabled and could never trigger, so
    # new spot runs do, and the plan says why.
    everything_current(world)
    world.runs[ECMWF] = ecmwf_run(C06 + timedelta(hours=6))
    probes = detect.build_probes

    def without_map(s: Settings) -> list[Probe]:
        return [
            replace(p, current_run=None, disabled="map fields are off (MAP_FIELDS_ENABLED)")
            if p.kind == "map"
            else p
            for p in probes(s)
        ]

    monkeypatch.setattr(detect, "build_probes", without_map)
    plan = plan_sync(None, settings=free(), now=T18 - timedelta(hours=4), known=stored_06())
    assert plan.due and state(plan, ECMWF) == "new" and state(plan, MAP) == "disabled"
    assert any("SYNC_TRIGGER_SOURCES=map names no component" in r for r in plan.reasons)
    assert not any("(not a trigger)" in r for r in plan.reasons)
    # With the map component present, the same spot run waits for the next map cycle.
    monkeypatch.setattr(detect, "build_probes", probes)
    assert not plan_sync(None, settings=free(), now=T18 - timedelta(hours=4), known=stored_06()).due
