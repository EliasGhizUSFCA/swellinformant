"""SYNTHETIC map-field settings and files for tests, on top of tests.synthetic_gfs.

Everything here is made up for testing (constant fields with recognisable numbers in the
real NODD layout); nothing is real model output. ``field_settings`` gives the map-field
settings for the short synthetic cycles (f000, f012, f024 by default) instead of the
production MAP_FIELD_STEPS, and ``add_map_hours`` publishes what the map fields read for
the fields alone: the map's own atmosphere file (``pgrb2.0p50``) and ``global.0p25`` with
the wave records 1-19 (core records and partitions).
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.core.config import Settings
from tests.synthetic_gfs import PATHS, SyntheticGfs, map_settings


def field_settings(
    lead: int = 24,
    step: int = 12,
    *,
    map_lead: int = 24,
    map_step: int = 12,
    providers: str = "noaa_gfs",
    partitions: str = "off",
    cycles: str = "00,06,12,18",
    max_bytes: int = 600_000_000,
    **kw: object,
) -> Settings:
    """map_settings with MAP_FIELD_STEPS = f000..``map_lead`` every ``map_step``.

    ``partitions`` is MAP_FIELD_PARTITIONS ("off" keeps the six core variables), ``cycles``
    MAP_FIELD_CYCLES (every cycle by default, so the synthetic 18Z cycle is a map cycle),
    ``max_bytes`` MAP_FIELD_MAX_BYTES_PER_RUN. Applied after validation, as in map_settings.
    """
    base = map_settings(lead, step, map_lead=map_lead, providers=providers, **kw)
    return base.model_copy(
        update={
            "map_field_steps": f"0-{max(map_lead, map_step)}:{map_step}",
            "map_field_partitions": partitions,
            "map_field_cycles": cycles,
            "map_field_max_bytes_per_run": max_bytes,
        }
    )


def map_atmos_url(cycle: datetime, fhour: int) -> str:
    """The map's own atmosphere file (MAP_FIELD_ATMOS_PATH_TEMPLATE, grid 0p50)."""
    return PATHS.map_atmos(cycle, fhour)[0]


def add_map_atmos(world: SyntheticGfs, cycle: datetime, fhour: int) -> None:
    """The synthetic atmosphere file (a 0.5 deg grid) at the pgrb2.0p50 path, not at the
    pgrb2.0p25 one."""
    world.add_atmos(cycle, fhour)
    src = httpx.URL(PATHS.atmos(cycle, fhour)).path
    dst = httpx.URL(map_atmos_url(cycle, fhour)).path
    for suffix in ("", ".idx"):
        world.files[dst + suffix] = world.files.pop(src + suffix)


def add_map_hours(
    world: SyntheticGfs,
    cycle: datetime,
    hours: list[int],
    *,
    values: dict[str, float] | None = None,
) -> None:
    """What the map fields need for the fields alone: the 0p50 atmosphere file and
    global.0p25 (``values`` replaces the wave records, see SyntheticGfs.add_wave)."""
    for h in hours:
        add_map_atmos(world, cycle, h)
        world.add_wave(cycle, "global.0p25", h, values=values)
