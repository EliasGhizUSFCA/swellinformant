"""Map ocean layers: when fields are produced, and the read side behind /api/map/fields.

Contract: app/schemas/map_fields.py. Storage: models.forecast_engine.MapField (one row per
run, valid time and variable; the encoding is described in app.services.forecasts.fields).

Fields are produced in one of three modes (:func:`map_fields_mode`):

* ``with_noaa``: "noaa_gfs" (the direct NOAA GRIB spot ingester) is a forecast provider
  and MAP_FIELDS_ENABLED is not false. Every NOAA GFS acquisition of a map cycle also stores
  the fields on its own run (the wind and pressure grids come from files that provider
  downloads anyway, plus the wave records per step).
* ``standalone``: no NOAA GRIB spot provider, and MAP_FIELDS_ENABLED is true, or unset
  while a live spot provider is configured (e.g. ``FORECAST_PROVIDERS=open_meteo,
  open_meteo_ecmwf``, the recommended setup). The ``forecasts.map_fields`` job reads only
  the global fields, under the separate source ``noaa_gfs_fields``.
* ``off``: MAP_FIELDS_ENABLED=false, or unset with only the demo provider (synthetic spot
  forecasts download nothing live unless MAP_FIELDS_ENABLED=true asks for it).

Map hours and cycles, in every mode: :func:`map_field_hours` (MAP_FIELD_STEPS, default
3-hourly to f120 then 6-hourly to f240, i.e. 61 hours, capped at MAP_FIELD_MAX_LEAD_HOURS)
of the cycles in MAP_FIELD_CYCLES (:func:`is_map_cycle`, default 00 and 12 UTC).

The read side serves whatever is stored, so it does not depend on the mode: the run of the
newest sync publication when SYNC_PUBLISH_GATE is on and that run is still servable
(the publish stage of a sync sets ``sync_runs.map_run_id``, so the map switches to a new
cycle together with the spot forecasts), otherwise the newest run (by model cycle) with at
least one complete valid time; ``run`` picks another retained run. A valid time is complete
when all six core variables (``CORE_MAP_FIELD_VARIABLES``) exist; the optional partition
variables never make it incomplete, and the index lists a partition only when the served
run stores it.

Database egress (docs/map-layers.md): the index reads aggregates and a few columns, never
field bytes; a field is one row read through a binary cursor (bytea in psycopg's default
text format arrives hex-encoded, two bytes on the wire for every byte stored).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Row, Select, bindparam, func, select
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource
from app.models.forecast_engine import (
    CORE_MAP_FIELD_VARIABLES,
    MAP_FIELD_VARIABLES,
    PARTITION_MAP_FIELD_VARIABLES,
    MapField,
)
from app.models.sync import SyncRun
from app.schemas.map_fields import (
    FieldIndexOut,
    FieldMeta,
    FieldSourceOut,
    FieldVariableOut,
    WaveSystem,
)
from app.services.sync.published import published_map_run_id

logger = logging.getLogger(__name__)

MapFieldsMode = Literal["with_noaa", "standalone", "off"]
NOAA_PROVIDER_NAME = "noaa_gfs"  # the FORECAST_PROVIDERS entry of the direct NOAA ingester
DEMO_PROVIDER_NAME = "demo"

# Published GFS / GFS-Wave forecast hours: hourly to f120, then every 3 h to f384 (the
# cadence noaa.forecast_hours follows). Map hours are always published hours.
GFS_HOURLY_UNTIL = 120
GFS_LATE_STEP = 3
GFS_MAX_LEAD = 384


def map_fields_mode(settings: Settings | None = None) -> MapFieldsMode:
    """How map fields are produced (MAP_FIELDS_ENABLED: None = automatic, see module docs)."""
    s = settings or get_settings()
    if s.map_fields_enabled is False:
        return "off"
    providers = s.forecast_provider_list
    if NOAA_PROVIDER_NAME in providers:
        return "with_noaa"
    if s.map_fields_enabled:
        return "standalone"
    live = [p for p in providers if p != DEMO_PROVIDER_NAME]
    return "standalone" if live else "off"


def _published(hour: int) -> bool:
    return 0 <= hour <= GFS_MAX_LEAD and (hour <= GFS_HOURLY_UNTIL or hour % GFS_LATE_STEP == 0)


def map_field_hours(
    settings: Settings | None = None, max_lead_hours: int | None = None
) -> list[int]:
    """Forecast hours with map fields: MAP_FIELD_STEPS (a boundary hour shared by two ranges
    counts once), up to MAP_FIELD_MAX_LEAD_HOURS (or ``max_lead_hours``), published hours
    only. The default "0-120:3,120-240:6" gives f000..f120 every 3 h and f126..f240 every
    6 h: 61 hours."""
    s = settings or get_settings()
    cap = s.map_field_max_lead_hours if max_lead_hours is None else max_lead_hours
    hours = {
        h for first, last, step in s.map_field_step_ranges for h in range(first, last + 1, step)
    }
    return sorted(h for h in hours if h <= cap and _published(h))


def is_map_cycle(settings: Settings, cycle: datetime) -> bool:
    """True if map fields are read for this GFS cycle (MAP_FIELD_CYCLES, UTC hours)."""
    return _as_utc(cycle).hour in settings.map_field_cycle_hours


def _step_after(settings: Settings, lead_hours: float) -> float:
    """Hours from ``lead_hours`` to the next configured map hour; beyond the last one, the
    last configured step. Used as the tolerance around a run's first and last valid time."""
    hours = map_field_hours(settings, max_lead_hours=GFS_MAX_LEAD)
    later = [h for h in hours if h > lead_hours]
    if later:
        return later[0] - lead_hours
    return float(hours[-1] - hours[-2]) if len(hours) > 1 else float(GFS_LATE_STEP)


@dataclass(frozen=True)
class VariableInfo:
    label: str
    display_min: float
    display_max: float
    system: WaveSystem | None = None


# Labels say what the number is: offshore model Hs, not the breaking (surf) height. WW3
# numbers swell partitions by height in each cell, hence "the largest swell partition";
# the partition periods are labelled "period" (NCEP lists them as mean periods).
VARIABLES: dict[str, VariableInfo] = {
    "hs": VariableInfo("Significant wave height (offshore model, not surf height)", 0.0, 10.0),
    "tp": VariableInfo("Peak wave period", 0.0, 20.0),
    "wave_dir": VariableInfo("Peak wave direction (degrees from)", 0.0, 360.0),
    "wind_u": VariableInfo("10 m wind, eastward component", -30.0, 30.0),
    "wind_v": VariableInfo("10 m wind, northward component", -30.0, 30.0),
    "pressure": VariableInfo("Mean sea-level pressure", 960.0, 1045.0),
    "swell1_hs": VariableInfo(
        "Primary swell height (largest swell partition here and now; offshore model)",
        0.0,
        10.0,
        "primary",
    ),
    "swell1_tp": VariableInfo("Primary swell period", 0.0, 20.0, "primary"),
    "swell1_dir": VariableInfo("Primary swell direction (degrees from)", 0.0, 360.0, "primary"),
    "swell2_hs": VariableInfo(
        "Secondary swell height (second-largest swell partition; offshore model)",
        0.0,
        10.0,
        "secondary",
    ),
    "swell2_tp": VariableInfo("Secondary swell period", 0.0, 20.0, "secondary"),
    "swell2_dir": VariableInfo("Secondary swell direction (degrees from)", 0.0, 360.0, "secondary"),
    "windsea_hs": VariableInfo("Wind sea height (offshore model)", 0.0, 10.0, "windsea"),
    "windsea_tp": VariableInfo("Wind sea period", 0.0, 20.0, "windsea"),
    "windsea_dir": VariableInfo("Wind sea direction (degrees from)", 0.0, 360.0, "windsea"),
}
assert set(VARIABLES) == set(MAP_FIELD_VARIABLES)
CORE_NAMES = tuple(CORE_MAP_FIELD_VARIABLES)
PARTITION_NAMES = tuple(PARTITION_MAP_FIELD_VARIABLES)
# Partition variables by wave system, in PARTITION_MAP_FIELD_VARIABLES order.
SYSTEM_VARIABLES: dict[WaveSystem, tuple[str, ...]] = {}
for _name in PARTITION_NAMES:
    _system = VARIABLES[_name].system
    assert _system is not None
    SYSTEM_VARIABLES[_system] = (*SYSTEM_VARIABLES.get(_system, ()), _name)


class RunNotRetained(LookupError):
    """The requested model run has no servable map fields (pruned, unknown or incomplete)."""


def _unavailable_message(settings: Settings) -> str:
    mode = map_fields_mode(settings)
    if mode == "off" and settings.map_fields_enabled is False:
        return (
            "Ocean layers are switched off (MAP_FIELDS_ENABLED=false). To show NOAA GFS-Wave "
            "and GFS model fields, set MAP_FIELDS_ENABLED=true, or leave it unset with a "
            "live FORECAST_PROVIDERS setup."
        )
    if mode == "off":
        return (
            "Ocean layers need live NOAA model data, and FORECAST_PROVIDERS is the demo "
            "provider: set MAP_FIELDS_ENABLED=true to fetch only the global NOAA GFS-Wave "
            "and GFS fields (python -m app.cli engine map-fields), or configure a live "
            "FORECAST_PROVIDERS setup."
        )
    return (
        "No ocean-layer fields are stored yet. They appear after the next NOAA GFS map "
        "cycle (MAP_FIELD_CYCLES) has been read; python -m app.cli engine map-fields "
        "fetches them now."
    )


@dataclass(frozen=True)
class _CurrentRun:
    run_id: int
    cycle_time: datetime
    valid_times: list[datetime]  # times at which all six core variables exist


@dataclass(frozen=True)
class ServableRun:
    """A run whose map fields can be served: at least one complete valid time."""

    run_id: int
    cycle_time: datetime
    complete_times: int  # valid times at which all six core variables exist


def servable_runs_query(limit: int | None = None) -> Select[Any]:
    """(run_id, cycle_time, complete valid times) of the servable runs, best first.

    Order: newest model cycle first; among runs of one cycle (e.g. the NOAA spot run and a
    fields-only run after a change of mode) the one with the most complete valid times,
    then the newer run. The read side serves the first; retention keeps the first two, so
    it never deletes what is served. Aggregates only: no field bytes are read.
    """
    per_time = (
        select(
            MapField.run_id,
            func.max(MapField.cycle_time).label("cycle"),
            func.count(func.distinct(MapField.variable))
            .filter(MapField.variable.in_(CORE_NAMES))
            .label("n"),
        )
        .group_by(MapField.run_id, MapField.valid_time)
        .subquery()
    )
    complete = func.count().filter(per_time.c.n == len(CORE_NAMES))
    stmt = (
        select(per_time.c.run_id, func.max(per_time.c.cycle), complete)
        .group_by(per_time.c.run_id)
        .having(complete > 0)
        .order_by(func.max(per_time.c.cycle).desc(), complete.desc(), per_time.c.run_id.desc())
    )
    return stmt if limit is None else stmt.limit(limit)


def servable_runs(db: Session, limit: int | None = None) -> list[ServableRun]:
    """Runs with at least one complete valid time, best first (see servable_runs_query)."""
    return [ServableRun(r, c, n) for r, c, n in db.execute(servable_runs_query(limit)).all()]


def _publication(db: Session) -> Row[int, datetime | None, int | None] | None:
    """(id, published_at, map_run_id) of the newest sync publication: the row
    ``app.services.sync.published.latest_publication`` returns (same filter and order),
    without its plan and stages."""
    return db.execute(
        select(SyncRun.id, SyncRun.published_at, SyncRun.map_run_id)
        .where(SyncRun.published_at.is_not(None))
        .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
        .limit(1)
    ).one_or_none()


def _served(db: Session, run_key: str | None, published_run_id: int | None) -> ServableRun | None:
    """The run to serve: with ``run_key`` the best retained run of that key
    (RunNotRetained if there is none); otherwise ``published_run_id`` when it is servable,
    else the best servable run (see servable_runs)."""
    if run_key is not None:
        ids = set(db.scalars(select(ForecastRun.id).where(ForecastRun.run_key == run_key)))
        best = [r for r in servable_runs(db) if r.run_id in ids][:1] if ids else []
        if not best:
            raise RunNotRetained(run_key)
        return best[0]
    if published_run_id is not None:
        runs = servable_runs(db)
        published = [r for r in runs if r.run_id == published_run_id]
        chosen = published or runs
        return chosen[0] if chosen else None
    found = servable_runs(db, limit=1)
    return found[0] if found else None


def _current_run(
    db: Session, run_key: str | None = None, published_run_id: int | None = None
) -> _CurrentRun | None:
    """The run served (see :func:`_served`; ``published_run_id`` is the gate's run, None
    when SYNC_PUBLISH_GATE is off or nothing was published) and its complete valid times.

    Ties within one cycle as in :func:`servable_runs`. Reads only (run, valid time,
    variable count) aggregates: no field bytes are loaded.
    """
    best = _served(db, run_key, published_run_id)
    if best is None:
        return None
    counts = db.execute(
        select(
            MapField.valid_time,
            func.count(func.distinct(MapField.variable)).filter(MapField.variable.in_(CORE_NAMES)),
        )
        .where(MapField.run_id == best.run_id)
        .group_by(MapField.valid_time)
        .order_by(MapField.valid_time)
    ).all()
    complete = [t for t, n in counts if n == len(CORE_NAMES)]
    incomplete = [t for t, n in counts if n != len(CORE_NAMES)]
    if incomplete:
        logger.info(
            "map fields run %s: %d valid time(s) lack a core variable and are not served "
            "(first %s)",
            best.run_id,
            len(incomplete),
            incomplete[0].isoformat(),
        )
    if not complete:
        if run_key is not None:
            raise RunNotRetained(run_key)
        return None
    return _CurrentRun(best.run_id, best.cycle_time, complete)


def _gated_run_id(db: Session, settings: Settings) -> int | None:
    """The map run of the newest publication when SYNC_PUBLISH_GATE is on, else None."""
    return published_map_run_id(db) if settings.sync_publish_gate else None


def served_run(db: Session) -> ServableRun | None:
    """The run the index and run-less field requests serve: the newest publication's map
    run while SYNC_PUBLISH_GATE is on and it is servable, else the best servable run. For
    readers that judge what the map shows (e.g. its freshness in the data-health check)."""
    return _served(db, None, _gated_run_id(db, get_settings()))


def stored_systems(db: Session, run_id: int, times: list[datetime]) -> list[WaveSystem]:
    """Wave systems whose three partition variables the run stores together at one or more
    of ``times`` (its complete valid times), in SYSTEM_VARIABLES order."""
    rows = db.execute(
        select(MapField.valid_time, MapField.variable)
        .where(
            MapField.run_id == run_id,
            MapField.variable.in_(PARTITION_NAMES),
            MapField.valid_time.in_(times),
        )
        .distinct()
    ).all()
    have: dict[datetime, set[str]] = {}
    for t, name in rows:
        have.setdefault(t, set()).add(name)
    return [
        system
        for system, names in SYSTEM_VARIABLES.items()
        if any(set(names) <= got for got in have.values())
    ]


def _variable_out(name: str) -> FieldVariableOut:
    info = VARIABLES[name]
    return FieldVariableOut(
        name=name,
        label=info.label,
        unit=MAP_FIELD_VARIABLES[name],
        display_min=info.display_min,
        display_max=info.display_max,
        group="partition" if name in PARTITION_MAP_FIELD_VARIABLES else "core",
        system=info.system,
    )


def field_index(db: Session, now: datetime | None = None) -> FieldIndexOut:
    """Which fields exist for the served model run, their valid times and their freshness.

    The served run is the newest publication's map run (SYNC_PUBLISH_GATE) while it is
    servable, else the newest servable run. ``published_at`` and ``sync_run_id`` name the
    newest publication when the served run is the one it published (None otherwise, e.g.
    before the first sync or after a fallback).

    ``age_hours`` is ``now - cycle_time`` (the model cycle, not the download); the index is
    ``stale`` beyond MAP_FIELD_STALE_AFTER_HOURS. The layers are still served when stale.
    """
    settings = get_settings()
    limit = settings.map_field_stale_after_hours
    publication = _publication(db)
    published_run_id = publication.map_run_id if publication is not None else None
    current = _current_run(db, None, published_run_id if settings.sync_publish_gate else None)
    run = (
        db.execute(
            select(
                ForecastRun.run_key,
                ForecastRun.completed_at,
                ForecastSource.code,
                ForecastSource.name,
                ForecastSource.attribution,
                ForecastSource.is_demo,
            )
            .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
            .where(ForecastRun.id == current.run_id)
        ).one_or_none()
        if current is not None
        else None
    )
    if current is None or run is None:
        return FieldIndexOut(
            available=False, message=_unavailable_message(settings), stale_after_hours=limit
        )
    now = _as_utc(now) if now is not None else utcnow()
    age = (now - current.cycle_time).total_seconds() / 3600.0
    systems = stored_systems(db, current.run_id, current.valid_times)
    names = [*CORE_NAMES, *(v for s in systems for v in SYSTEM_VARIABLES[s])]
    published = publication is not None and publication.map_run_id == current.run_id
    return FieldIndexOut(
        available=True,
        source=FieldSourceOut(
            code=run.code, name=run.name, attribution=run.attribution, is_demo=run.is_demo
        ),
        run_key=run.run_key,
        cycle_time=current.cycle_time,
        valid_times=current.valid_times,
        variables=[_variable_out(name) for name in names],
        updated_at=run.completed_at,
        age_hours=round(age, 2),
        stale_after_hours=limit,
        stale=age > limit,
        published_at=publication.published_at if publication is not None and published else None,
        sync_run_id=publication.id if publication is not None and published else None,
    )


def _as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def same_instant(at: datetime, valid_time: datetime) -> bool:
    """True if ``at`` (naive = UTC, as field_bytes reads it) is the instant ``valid_time``."""
    try:
        return _as_utc(at) == _as_utc(valid_time)
    except OverflowError:
        return False


# One field row, metadata and bytes, read through a binary cursor (see _field_row).
_FIELD_ROW = select(
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
    MapField.data,
).where(
    MapField.run_id == bindparam("run_id"),
    MapField.variable == bindparam("variable"),
    MapField.valid_time == bindparam("valid_time"),
)
_FIELD_ROW_SQL: dict[str, str] = {}


def _field_row_sql(dialect: Dialect) -> str:
    sql = _FIELD_ROW_SQL.get(dialect.name)
    if sql is None:
        sql = _FIELD_ROW_SQL[dialect.name] = str(_FIELD_ROW.compile(dialect=dialect))
    return sql


def _field_row(
    db: Session, run_id: int, variable: str, valid_time: datetime
) -> tuple[Any, ...] | None:
    """The stored field (``_FIELD_ROW`` columns) through a psycopg binary cursor on the
    session's own connection and transaction: bytea comes back as its raw bytes instead of
    hex text, so the database sends about the field's size, not twice it."""
    connection = db.connection()
    driver = connection.connection.driver_connection
    params = {"run_id": run_id, "variable": variable, "valid_time": valid_time}
    with driver.cursor(binary=True) as cursor:  # type: ignore[union-attr]
        cursor.execute(_field_row_sql(connection.dialect), params)
        row: tuple[Any, ...] | None = cursor.fetchone()
    return row


def field_bytes(
    db: Session, variable: str, at: datetime | None, run: str | None = None
) -> tuple[FieldMeta, bytes] | None:
    """One field of the served run (or of the retained run ``run``, a run_key) at the
    stored valid time nearest to ``at``.

    Without ``run`` the served run is the index's (the newest publication's run while
    SYNC_PUBLISH_GATE is on and it is servable, else the newest servable run). ``at``
    defaults to now; a naive value is taken as UTC. Only the run's complete valid times
    (the ones the index lists) are candidates; on a tie the earlier time wins. Model values
    are never interpolated in time. Returns None when ``at`` lies further before the run's
    first or after its last valid time than one configured map step there (MAP_FIELD_STEPS:
    3 h near the start, 6 h after f120 by default), when the variable is unknown, or when
    the run does not store it at that time (e.g. a partition). Raises RunNotRetained when
    ``run`` names no retained run.
    """
    if variable not in MAP_FIELD_VARIABLES:
        return None
    settings = get_settings()
    gated = _gated_run_id(db, settings) if run is None else None
    current = _current_run(db, run, gated)
    if current is None:
        return None
    try:
        when = _as_utc(at) if at is not None else utcnow()
    except OverflowError:
        return None  # e.g. 0001-01-01T00:00+05:00: no stored field is anywhere near it
    times = current.valid_times

    def lead(t: datetime) -> float:
        return (t - current.cycle_time).total_seconds() / 3600.0

    before = timedelta(hours=_step_after(settings, lead(times[0])))
    after = timedelta(hours=_step_after(settings, lead(times[-1])))
    if when < times[0] - before or when > times[-1] + after:
        return None
    nearest = min(times, key=lambda t: (abs(t - when), t))
    row = _field_row(db, current.run_id, variable, nearest)
    if row is None or row[-1] is None:
        return None
    nx, ny, lat0, lon0, dlat, dlon, scale, offset, valid_time, run_key, data = row
    meta = FieldMeta(
        variable=variable,
        group="partition" if variable in PARTITION_MAP_FIELD_VARIABLES else "core",
        unit=MAP_FIELD_VARIABLES[variable],
        valid_time=valid_time,
        run_key=run_key,
        nx=nx,
        ny=ny,
        lat0=lat0,
        lon0=lon0,
        dlat=dlat,
        dlon=dlon,
        scale=scale,
        offset=offset,
    )
    return meta, bytes(data)
