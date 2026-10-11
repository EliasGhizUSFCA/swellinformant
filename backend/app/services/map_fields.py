"""Map ocean layers: when fields are produced, and the read side behind /api/map/fields.

Contract: app/schemas/map_fields.py. Storage: models.forecast_engine.MapField (one row per
run, valid time and variable; the encoding is described in app.services.forecasts.fields).

Fields are produced in one of three modes (:func:`map_fields_mode`):

* ``with_noaa``: "noaa_gfs" is a forecast provider and MAP_FIELDS_ENABLED is not false.
  Every NOAA GFS acquisition also stores the fields on its own run (the grids come from
  files that provider downloads anyway, plus three wave records per step).
* ``standalone``: MAP_FIELDS_ENABLED=true without the NOAA provider (e.g. demo spot
  forecasts with real ocean layers). The ``forecasts.map_fields`` job reads only the
  global fields, under the separate source ``noaa_gfs_fields``.
* ``off``: neither (the default with the demo provider), or MAP_FIELDS_ENABLED=false.

The read side serves whatever is stored, from the newest run (by model cycle) with at
least one complete valid time, so it does not depend on the mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import ForecastRun
from app.models.forecast_engine import MAP_FIELD_VARIABLES, MapField
from app.schemas.map_fields import FieldIndexOut, FieldMeta, FieldSourceOut, FieldVariableOut

logger = logging.getLogger(__name__)

MapFieldsMode = Literal["with_noaa", "standalone", "off"]
NOAA_PROVIDER_NAME = "noaa_gfs"  # the FORECAST_PROVIDERS entry of the direct NOAA ingester
# Runs looked at when the newest one has no complete valid time.
RUNS_TO_TRY = 5


def map_fields_mode(settings: Settings | None = None) -> MapFieldsMode:
    """How map fields are produced (MAP_FIELDS_ENABLED: None = automatic)."""
    s = settings or get_settings()
    if s.map_fields_enabled is False:
        return "off"
    if NOAA_PROVIDER_NAME in s.forecast_provider_list:
        return "with_noaa"
    return "standalone" if s.map_fields_enabled else "off"


@dataclass(frozen=True)
class VariableInfo:
    label: str
    display_min: float
    display_max: float


# Labels say what the number is: offshore model Hs, not the breaking (surf) height.
VARIABLES: dict[str, VariableInfo] = {
    "hs": VariableInfo("Significant wave height (offshore model, not surf height)", 0.0, 10.0),
    "tp": VariableInfo("Peak wave period", 0.0, 20.0),
    "wave_dir": VariableInfo("Peak wave direction (degrees from)", 0.0, 360.0),
    "wind_u": VariableInfo("10 m wind, eastward component", -30.0, 30.0),
    "wind_v": VariableInfo("10 m wind, northward component", -30.0, 30.0),
    "pressure": VariableInfo("Mean sea-level pressure", 960.0, 1045.0),
}
assert set(VARIABLES) == set(MAP_FIELD_VARIABLES)


def _unavailable_message(settings: Settings) -> str:
    mode = map_fields_mode(settings)
    if mode == "off" and settings.map_fields_enabled is False:
        return (
            "Ocean layers are switched off (MAP_FIELDS_ENABLED=false). To show NOAA GFS-Wave "
            "and GFS model fields, set MAP_FIELDS_ENABLED=true, or leave it unset and add "
            "noaa_gfs to FORECAST_PROVIDERS."
        )
    if mode == "off":
        return (
            "Ocean layers need live NOAA model data: add noaa_gfs to FORECAST_PROVIDERS, or "
            "set MAP_FIELDS_ENABLED=true to fetch only the global NOAA GFS-Wave and GFS "
            "fields (python -m app.cli engine map-fields)."
        )
    return (
        "No ocean-layer fields are stored yet. They appear after the next NOAA GFS cycle "
        "has been read; python -m app.cli engine map-fields fetches them now."
    )


@dataclass(frozen=True)
class _CurrentRun:
    run: ForecastRun
    cycle_time: datetime
    valid_times: list[datetime]  # times at which all six variables exist


def _current_run(db: Session) -> _CurrentRun | None:
    """The newest run (by model cycle) that has at least one complete valid time.

    Several runs can hold the same cycle (e.g. the NOAA spot run and a fields-only run
    after a change of mode); then the one with the most complete valid times wins, and on
    a tie the newer run. Reads only (run, valid time, variable count) aggregates: no field
    bytes are loaded.
    """
    runs = db.execute(
        select(MapField.run_id, func.max(MapField.cycle_time).label("cycle"))
        .group_by(MapField.run_id)
        .order_by(func.max(MapField.cycle_time).desc(), MapField.run_id.desc())
        .limit(RUNS_TO_TRY)
    ).all()
    chosen: _CurrentRun | None = None
    for run_id, cycle in runs:
        if chosen is not None and cycle < chosen.cycle_time:
            break  # an older cycle: the newest cycle with a complete time is decided
        counts = db.execute(
            select(MapField.valid_time, func.count(func.distinct(MapField.variable)))
            .where(MapField.run_id == run_id)
            .group_by(MapField.valid_time)
            .order_by(MapField.valid_time)
        ).all()
        complete = [t for t, n in counts if n == len(MAP_FIELD_VARIABLES)]
        incomplete = [t for t, n in counts if n != len(MAP_FIELD_VARIABLES)]
        if incomplete:
            logger.info(
                "map fields run %s: %d valid time(s) lack a variable and are not served (first %s)",
                run_id,
                len(incomplete),
                incomplete[0].isoformat(),
            )
        if not complete or (chosen is not None and len(complete) <= len(chosen.valid_times)):
            continue
        run = db.get(ForecastRun, run_id)
        if run is not None:
            chosen = _CurrentRun(run, cycle, complete)
    return chosen


def field_index(db: Session) -> FieldIndexOut:
    """Which fields exist for the newest model run, and their valid times."""
    current = _current_run(db)
    if current is None:
        return FieldIndexOut(available=False, message=_unavailable_message(get_settings()))
    source = current.run.source
    return FieldIndexOut(
        available=True,
        source=FieldSourceOut(
            code=source.code,
            name=source.name,
            attribution=source.attribution,
            is_demo=source.is_demo,
        ),
        run_key=current.run.run_key,
        cycle_time=current.cycle_time,
        valid_times=current.valid_times,
        variables=[
            FieldVariableOut(
                name=name,
                label=VARIABLES[name].label,
                unit=unit,
                display_min=VARIABLES[name].display_min,
                display_max=VARIABLES[name].display_max,
            )
            for name, unit in MAP_FIELD_VARIABLES.items()
        ],
    )


def _as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def field_bytes(db: Session, variable: str, at: datetime | None) -> tuple[FieldMeta, bytes] | None:
    """One field of the current run at the stored valid time nearest to ``at``.

    ``at`` defaults to now; a naive value is taken as UTC. Only the run's complete valid
    times (the ones the index lists) are candidates; on a tie the earlier time wins.
    Returns None when ``at`` lies more than one field step (MAP_FIELD_STEP_HOURS) before
    the run's first or after its last valid time, or the variable is unknown.
    """
    if variable not in MAP_FIELD_VARIABLES:
        return None
    current = _current_run(db)
    if current is None:
        return None
    when = _as_utc(at) if at is not None else utcnow()
    times = current.valid_times
    step = timedelta(hours=get_settings().map_field_step_hours)
    if when < times[0] - step or when > times[-1] + step:
        return None
    nearest = min(times, key=lambda t: (abs(t - when), t))
    # Metadata first, then the bytes of this one row only.
    row = db.execute(
        select(
            MapField.id,
            MapField.nx,
            MapField.ny,
            MapField.lat0,
            MapField.lon0,
            MapField.dlat,
            MapField.dlon,
            MapField.scale,
            MapField.offset,
            MapField.valid_time,
            MapField.run_key,
        ).where(
            MapField.run_id == current.run.id,
            MapField.variable == variable,
            MapField.valid_time == nearest,
        )
    ).one_or_none()
    if row is None:
        return None
    data = db.scalar(select(MapField.data).where(MapField.id == row.id))
    if data is None:
        return None
    meta = FieldMeta(
        variable=variable,
        unit=MAP_FIELD_VARIABLES[variable],
        valid_time=row.valid_time,
        run_key=row.run_key,
        nx=row.nx,
        ny=row.ny,
        lat0=row.lat0,
        lon0=row.lon0,
        dlat=row.dlat,
        dlon=row.dlon,
        scale=row.scale,
        offset=row.offset,
    )
    return meta, bytes(data)
