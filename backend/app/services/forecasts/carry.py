"""Component refreshes in the database: carry a component forward, validate each spot.

A refresh that fetches only one component of a spot source (design D6: a new wind run, the
waves unchanged) stores the fetched rows with the other component's columns empty, then
:func:`carry_component` fills them from the run each spot is served from, in ONE ``UPDATE
... FROM`` on the server: no forecast row travels through Python. Rows are matched on
``(spot_id, valid_time)``; an hour the source run does not have stays empty (it is listed in
``missing_fields``, never filled in). The carried values keep their own model run: the new
run records the carried component's ORIGINAL run key, init times and retrieval time
(``ingestion.refresh_source``).

:func:`validate_spots` then checks every spot of the new run (design D9): at least
FORECAST_MIN_VALID_FRACTION of its hours carry both a wave height and a wind speed, and no
value is outside :data:`PHYSICAL_RANGES`. A spot that fails is removed from the run
(:func:`drop_spots`) and keeps its previous run in the version.

Lean imports only (the refresh Function runs this): SQLAlchemy and the forecast field list.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY, INTEGER, TEXT
from sqlalchemy.orm import Session

from app.models import ForecastRun
from app.services.forecasts.base import ATMOSPHERE, COMPONENT_FIELDS, FORECAST_FIELDS, WAVE

# Inclusive physical bounds of the stored values (SI-ish units of FORECAST_FIELDS): wider
# than any real sea state or wind, narrow enough to catch unit and decoding errors (a
# pressure in Pa, a height in mm, a direction in radians is not caught: it is in range).
PHYSICAL_RANGES: Mapping[str, tuple[float, float]] = {
    **{f: (0.0, 30.0) for f in FORECAST_FIELDS if f.endswith("_height_m")},
    **{f: (0.0, 40.0) for f in FORECAST_FIELDS if f.endswith("_period_s")},
    **{f: (0.0, 360.0) for f in FORECAST_FIELDS if f.endswith("_direction_deg")},
    "wind_speed_kmh": (0.0, 400.0),
    "wind_gust_kmh": (0.0, 500.0),
    "pressure_msl_hpa": (850.0, 1100.0),
    "sea_level_m": (-30.0, 30.0),
}
# ForecastRecord.extra keys that belong to one component (copied with it when carried;
# ``pressure_kind`` is the Windy adapter's label of its pressure series).
COMPONENT_EXTRA_KEYS: Mapping[str, tuple[str, ...]] = {
    WAVE: ("wave_cell", "wave_model", "native_step_seconds"),
    ATMOSPHERE: ("gust_kind", "wind_cell", "pressure_kind"),
}
# Columns derived from the wave component.
WAVE_DERIVED = ("wave_power_kw_m", "wave_period_kind")
# A model cycle inside a run key: every run key format has ":YYYYMMDDHH" stamps
# (open_meteo "gfswave016:2026101912|gfs013:...", "noaa-gfs-fields:2026101912", ...).
_CYCLE_STAMP = re.compile(r"(?<!\d)(\d{10})(?!\d)")


def newest_cycle(run_key: str | None) -> datetime | None:
    """The newest model cycle named in a run key (its ``YYYYMMDDHH`` stamps), or None."""
    if not run_key:
        return None
    stamps = _CYCLE_STAMP.findall(run_key)
    if not stamps:
        return None
    try:
        return max(datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=UTC) for s in stamps)
    except ValueError:
        return None


def domain_cycles(run_key: str | None) -> dict[str, datetime] | None:
    """The cycle of each model domain of a multi-domain run key (Open-Meteo
    ``"gfswave016:2026102118|gfs025:2026102200"``), or None for any other format."""
    if not run_key:
        return None
    out: dict[str, datetime] = {}
    for part in run_key.split("|"):
        name, sep, stamp = part.rpartition(":")
        if not sep or not name or not _CYCLE_STAMP.fullmatch(stamp):
            return None
        try:
            out[name] = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            return None
    return out


def domains_behind(available: str | None, stored: str | None) -> bool:
    """True when the available run has the stored run's model domains, none on a newer
    cycle and at least one on an older one: a lagging metadata server (Open-Meteo's servers
    can disagree for minutes) offers part of the previous run, which is never new."""
    have, kept = domain_cycles(available), domain_cycles(stored)
    if not have or not kept or have.keys() != kept.keys():
        return False
    return all(have[k] <= kept[k] for k in have) and any(have[k] < kept[k] for k in have)


def older_than_stored(available: str | None, stored: str | None) -> bool:
    """Monotonic detection: the available key names an older run than the stored one (an
    older newest cycle, or some domains behind and none ahead). Never new."""
    if not available or not stored or available == stored:
        return False
    have, kept = newest_cycle(available), newest_cycle(stored)
    if have is not None and kept is not None and have < kept:
        return True
    return domains_behind(available, stored)


def component_part(run_key: str | None, like: str | None) -> str | None:
    """The part of a whole-source run key that names the model domains of the component key
    ``like``, in ``like``'s order (a run stored before component keys existed, Upgrade 4:
    ``"gfswave016:2026102700|gfs013:2026102706"`` with ``like`` ``"gfswave016:2026102712"``
    gives ``"gfswave016:2026102700"``). ``run_key`` itself when it names exactly ``like``'s
    domains; None when it does not name all of them, or either is not a multi-domain key."""
    have, want = domain_cycles(run_key), domain_cycles(like)
    if not run_key or not have or not want or not want.keys() <= have.keys():
        return None
    if want.keys() == have.keys():
        return run_key
    parts = {part.rpartition(":")[0]: part for part in run_key.split("|")}
    return "|".join(parts[name] for name in want)


def combined_run_key(keys: Mapping[str, str]) -> str:
    """The run key of data made of these component keys: one key when both components share
    it, else ``"<wave>|<atmosphere>"`` (the Open-Meteo order: wave domains first)."""
    wave, atmosphere = keys[WAVE], keys[ATMOSPHERE]
    return wave if wave == atmosphere else f"{wave}|{atmosphere}"


def run_component_keys(details: Mapping[str, Any] | None) -> dict[str, str]:
    """The component keys a stored run records (``details.component_keys``); empty for a run
    stored before components existed."""
    raw = (details or {}).get("component_keys")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k in COMPONENT_FIELDS and v}


def component_keys_of_runs(db: Session, run_ids: Iterable[int]) -> dict[int, dict[str, str]]:
    """{run id: its component keys} (one statement reading one JSON key per run)."""
    ids = sorted({int(r) for r in run_ids})
    if not ids:
        return {}
    rows = db.execute(
        select(ForecastRun.id, ForecastRun.details["component_keys"]).where(ForecastRun.id.in_(ids))
    ).all()
    return {int(rid): run_component_keys({"component_keys": keys}) for rid, keys in rows}


def _new_value(name: str, carried: Collection[str]) -> str:
    return f"o.{name}" if name in carried else f"n.{name}"


def carry_component(db: Session, *, run_id: int, component: str, sources: Mapping[int, int]) -> int:
    """Fill ``component``'s columns of run ``run_id`` from the source run of each spot
    (``sources``: spot id -> run id), matching rows on the valid time. ``missing_fields``,
    the wave-derived columns (for the wave component) and the component's ``extra`` keys
    follow. Returns the rows updated; the caller commits."""
    if component not in COMPONENT_FIELDS:
        raise ValueError(f"unknown component {component!r}")
    if not sources:
        return 0
    fields = COMPONENT_FIELDS[component]
    assignments = [f"{f} = o.{f}" for f in fields]
    if component == WAVE:
        assignments += [f"{c} = o.{c}" for c in WAVE_DERIVED]
    missing = ", ".join(
        f"CASE WHEN {_new_value(f, fields)} IS NULL THEN '{f}' END" for f in FORECAST_FIELDS
    )
    assignments.append(f"missing_fields = to_jsonb(array_remove(ARRAY[{missing}]::text[], NULL))")
    keys = ", ".join(f"'{k}', o.extra->'{k}'" for k in COMPONENT_EXTRA_KEYS[component])
    # The interpolated flags: the fetched row's own, plus the carried row's for the carried
    # component's fields.
    interpolated = (
        "(SELECT jsonb_agg(DISTINCT u.x ORDER BY u.x) FROM ("
        " SELECT jsonb_array_elements_text(COALESCE(n.extra->'interpolated_fields', '[]'))"
        " AS x UNION SELECT y FROM jsonb_array_elements_text("
        "COALESCE(o.extra->'interpolated_fields', '[]')) AS y WHERE y = ANY(:fields)) AS u)"
    )
    # Every name in these statements is a fixed column or key name of this module.
    assignments.append(
        "extra = (n.extra - 'interpolated' - 'interpolated_fields')"  # noqa: S608
        f" || jsonb_strip_nulls(jsonb_build_object({keys}))"
        f" || COALESCE((SELECT jsonb_build_object('interpolated', true,"
        f" 'interpolated_fields', i.v) FROM (SELECT {interpolated} AS v) AS i"
        " WHERE i.v IS NOT NULL), '{}'::jsonb)"
    )
    stmt = text(
        f"UPDATE wave_forecasts AS n SET {', '.join(assignments)}"  # noqa: S608 (fixed names)
        " FROM unnest(:spots, :runs) AS m(spot_id, run_id)"
        " JOIN wave_forecasts AS o ON o.run_id = m.run_id AND o.spot_id = m.spot_id"
        " WHERE n.run_id = :run_id AND n.spot_id = m.spot_id AND n.valid_time = o.valid_time"
    ).bindparams(
        bindparam("spots", type_=ARRAY(INTEGER)),
        bindparam("runs", type_=ARRAY(INTEGER)),
        bindparam("fields", type_=ARRAY(TEXT)),
    )
    pairs = sorted(sources.items())
    result = db.execute(
        stmt,
        {
            "spots": [s for s, _ in pairs],
            "runs": [r for _, r in pairs],
            "fields": list(fields),
            "run_id": run_id,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


def validate_spots(
    db: Session, run_id: int, min_valid_fraction: float, spot_ids: Iterable[int] | None = None
) -> dict[int, str | None]:
    """{spot id: None when valid, else the reason} for the spots of run ``run_id`` (D9).

    A spot is valid when at least ``min_valid_fraction`` of the run's hours carry both a
    wave height and a wind speed (the hours: the most rows any spot of the run has, so a
    spot with a short or gappy series fails too) and every value is inside
    :data:`PHYSICAL_RANGES`. ``spot_ids``: spots expected in the run; one without any row
    fails ("no data"). One statement."""
    checks = ", ".join(
        f"count(*) FILTER (WHERE {f} < {lo} OR {f} > {hi}) AS bad_{f}"
        for f, (lo, hi) in PHYSICAL_RANGES.items()
    )
    rows = db.execute(
        text(
            "SELECT spot_id, count(*) AS n,"  # noqa: S608 (fixed column names)
            " count(*) FILTER (WHERE sig_wave_height_m IS NOT NULL"
            " AND wind_speed_kmh IS NOT NULL) AS ok,"
            f" {checks} FROM wave_forecasts WHERE run_id = :run_id GROUP BY spot_id"
        ),
        {"run_id": run_id},
    ).mappings()
    stats = {int(r["spot_id"]): dict(r) for r in rows}
    hours = max((int(s["n"]) for s in stats.values()), default=0)
    out: dict[int, str | None] = {}
    for spot_id in sorted(set(stats) | {int(s) for s in spot_ids or ()}):
        s = stats.get(spot_id)
        if s is None or not hours:
            out[spot_id] = "no data"
            continue
        bad = [f for f in PHYSICAL_RANGES if int(s[f"bad_{f}"] or 0)]
        fraction = int(s["ok"]) / hours
        if bad:
            out[spot_id] = "values outside physical ranges: " + ", ".join(bad)
        elif fraction < min_valid_fraction:
            out[spot_id] = (
                f"only {fraction:.0%} of {hours} hours carry a wave height and a wind speed "
                f"(FORECAST_MIN_VALID_FRACTION {min_valid_fraction:g})"
            )
        else:
            out[spot_id] = None
    return out


def drop_spots(db: Session, run_id: int, spot_ids: Collection[int]) -> int:
    """Delete the rows of these spots from run ``run_id`` (their data failed); the caller
    commits."""
    if not spot_ids:
        return 0
    result = db.execute(
        text("DELETE FROM wave_forecasts WHERE run_id = :run_id AND spot_id = ANY(:spots)"),
        {"run_id": run_id, "spots": sorted(int(s) for s in spot_ids)},
    )
    return int(getattr(result, "rowcount", 0) or 0)
