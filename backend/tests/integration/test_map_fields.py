"""Map ocean layers end to end: ingestion, the standalone job, the API, retention, jobs.

Every field here is built from SYNTHETIC GRIB2 files (tests.synthetic_gfs and
tests.synthetic_map_fields: made-up constant values in the real NODD layout, served by
httpx.MockTransport); nothing is real model output. The live check against NOAA is
documented in docs/map-layers.md.
"""

from __future__ import annotations

import dataclasses
import json
import re
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal, get_engine
from app.models import (
    ForecastRun,
    ForecastSource,
    PredictionArchive,
    SurfQualityPrediction,
    SwellEvent,
    WaveForecast,
)
from app.models.enums import ForecastRunStatus
from app.models.forecast_engine import (
    CORE_MAP_FIELD_VARIABLES,
    PARTITION_MAP_FIELD_VARIABLES,
    MapField,
)
from app.services import map_fields as map_fields_service
from app.services.forecasts.base import FetchResult, RunInfo, SpotPoint
from app.services.forecasts.fields import QUANTISATION, TargetGrid, decode_bytes
from app.services.forecasts.ingestion import acquire_from_provider, acquire_map_fields
from app.services.forecasts.noaa import NoaaGfsFieldsProvider, NoaaGfsProvider
from app.services.forecasts.queries import candidate_runs, forecast_status, latest_runs_by_spot
from app.services.maintenance import cleanup, prune_map_fields
from app.services.pipeline import run_pipeline
from app.services.surf_quality.predict import compute_pending_predictions
from tests.conftest import ApiClient
from tests.integration.test_public_cache_headers import SPOT_LIST_CACHE_CONTROL
from tests.synthetic_gfs import PATHS, SyntheticGfs
from tests.synthetic_map_fields import add_map_hours, field_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 23, 40, tzinfo=UTC)  # nominal cycle with a 5 h lag: 18Z
CYCLE = datetime(2026, 10, 10, 18, tzinfo=UTC)
HOURS = [0, 12, 24]
TIMES = [CYCLE + timedelta(hours=h) for h in HOURS]
KEEP = ["pipeline", "jeffreys-bay"]  # global.0p16 and gsouth.0p25 (not global.0p25)


def fields_only(
    world: SyntheticGfs, map_lead: int = 24, partitions: str = "off", **kw: Any
) -> NoaaGfsFieldsProvider:
    s = field_settings(
        map_lead=map_lead, providers="demo", map_fields_enabled=True, partitions=partitions, **kw
    )
    return NoaaGfsFieldsProvider(s, client=world.client(), backoff_seconds=0)


def standalone_world(cycle: datetime = CYCLE, hours: list[int] = HOURS) -> SyntheticGfs:
    world = SyntheticGfs()
    add_map_hours(world, cycle, hours)
    return world


def stored(db: Session) -> list[MapField]:
    db.expire_all()
    return list(db.scalars(select(MapField).order_by(MapField.valid_time, MapField.variable)).all())


def keep_spots(db: Session, slugs: list[str]) -> None:
    db.execute(text("UPDATE surf_spots SET is_active = (slug = ANY(:keep))"), {"keep": slugs})
    db.commit()


@pytest.fixture
def fields_run(db: Session) -> ForecastRun:
    """A standalone fields run of the synthetic 18Z cycle with f000, f012 and f024."""
    out = acquire_map_fields(db, now=NOW, provider=fields_only(standalone_world()))
    assert out["status"] == "success", out
    run = db.get(ForecastRun, out["run_id"])
    assert run is not None
    return run


# --------------------------------------------------------------------------- ingestion
def test_noaa_acquisition_stores_map_fields_idempotently(db: Session) -> None:
    keep_spots(db, KEEP)
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS)
    provider = NoaaGfsProvider(field_settings(), client=world.client(), backoff_seconds=0)
    assert provider.map_fields  # FORECAST_PROVIDERS=noaa_gfs: the "with_noaa" mode

    outcome = acquire_from_provider(db, provider, now=NOW)

    assert outcome.status == ForecastRunStatus.SUCCESS.value and outcome.records == 6
    run = db.get(ForecastRun, outcome.run_id)
    assert run is not None
    rows = stored(db)
    assert len(rows) == 18 and {r.run_id for r in rows} == {run.id}
    assert run.details["map_fields"] == {
        "count": 18,
        "bytes": sum(len(r.data) for r in rows),
        "valid_times": [t.isoformat() for t in TIMES],
        "partition_times": [],  # MAP_FIELD_PARTITIONS=off here
        "warnings": [],
    }
    assert any(w.startswith("map fields: 18 fields") for w in run.details["warnings"])
    for r in rows:
        q = QUANTISATION[r.variable]
        assert (r.source_code, r.run_key, r.cycle_time) == (
            "noaa_gfs_direct",
            "noaa-gfs:2026101018",
            CYCLE,
        )
        assert (r.nx, r.ny, r.lat0, r.lon0, r.dlat, r.dlon) == (720, 361, 90.0, -180.0, 0.5, 0.5)
        assert (r.scale, r.offset) == (q.scale, q.offset)
        assert len(zlib.decompress(r.data)) == 720 * 361

    # Re-ingesting the same run replaces the fields in place, never duplicates them.
    ids = {(r.valid_time, r.variable): r.id for r in rows}
    again = acquire_from_provider(db, provider, now=NOW + timedelta(minutes=30), force=True)
    assert again.run_id == run.id and again.status == "success"
    rows = stored(db)
    assert {(r.valid_time, r.variable): r.id for r in rows} == ids
    assert db.scalar(select(func.count()).select_from(WaveForecast)) == 6


def test_a_noaa_run_without_map_fields_has_none(db: Session) -> None:
    keep_spots(db, KEEP)
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS)
    provider = NoaaGfsProvider(field_settings(), client=world.client(), backoff_seconds=0)
    provider.map_fields = False
    outcome = acquire_from_provider(db, provider, now=NOW)
    run = db.get(ForecastRun, outcome.run_id)
    assert run is not None and "map_fields" not in run.details
    assert stored(db) == []
    assert not any("global.0p25" in path for path in world.paths_requested())


def test_map_fields_the_database_refuses_never_cost_the_spot_records(db: Session) -> None:
    keep_spots(db, KEEP)
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS)
    provider = NoaaGfsProvider(field_settings(), client=world.client(), backoff_seconds=0)
    fetch = provider.fetch

    def refused(spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        result = fetch(spots, run)
        # The last field gets a variable the map_fields CHECK constraint rejects.
        result.fields[-1] = dataclasses.replace(result.fields[-1], variable="sst")
        return result

    provider.fetch = refused  # type: ignore[method-assign]
    outcome = acquire_from_provider(db, provider, now=NOW)

    assert outcome.status == ForecastRunStatus.SUCCESS.value and outcome.records == 6
    run = db.get(ForecastRun, outcome.run_id)
    assert run is not None and run.status == ForecastRunStatus.SUCCESS
    assert db.scalar(select(func.count()).select_from(WaveForecast)) == 6
    assert stored(db) == []  # none of the 18 (the SAVEPOINT is rolled back as a whole)
    summary = run.details["map_fields"]
    assert (summary["count"], summary["bytes"], summary["valid_times"]) == (0, 0, [])
    assert summary["warnings"][-1].startswith("map fields not stored (IntegrityError")
    assert "\\x" not in summary["warnings"][-1]  # no field bytes in the message


# --------------------------------------------------------------------------- standalone
def test_standalone_job_stores_fields_under_their_own_source(db: Session) -> None:
    world = standalone_world()
    provider = fields_only(world)
    out = acquire_map_fields(db, now=NOW, provider=provider)

    assert out["status"] == "success" and out["run_key"] == "noaa-gfs-fields:2026101018"
    assert (out["fields"], out["complete_hours"], out["map_hours"]) == (18, 3, 3)
    assert out["stored_bytes"] == sum(len(r.data) for r in stored(db))
    assert out["downloaded_bytes"] > 0 and out["requests"] == 15
    assert [s["fhour"] for s in out["steps"]] == HOURS and out["warnings"] == []
    source = db.scalar(select(ForecastSource).where(ForecastSource.code == "noaa_gfs_fields"))
    assert source is not None
    assert (source.kind, source.family, source.priority, source.is_demo) == (
        "fields",
        "ncep_gfs",
        1000,
        False,
    )
    assert source.name == "NOAA GFS-Wave + GFS global fields"
    run = db.get(ForecastRun, out["run_id"])
    assert run is not None and run.status == ForecastRunStatus.SUCCESS
    assert run.details["fields_only"] is True and run.details["spot_ids"] == []
    assert run.details["map_fields"]["count"] == 18
    assert (run.record_count, run.spot_count) == (0, 0)
    assert run.predictions_computed_at is not None  # nothing to score
    assert db.scalar(select(func.count()).select_from(WaveForecast)) == 0
    assert {r.source_code for r in stored(db)} == {"noaa_gfs_fields"}

    # The same cycle again: skipped after the cheap availability probes only.
    before = len(world.requests)
    again = acquire_map_fields(db, now=NOW + timedelta(hours=2), provider=provider)
    assert again["status"] == "skipped" and again["run_id"] == run.id
    assert all(r.method == "HEAD" for r in world.requests[before:])
    # A given cycle needs no probe at all; --force re-fetches and replaces.
    before = len(world.requests)
    assert acquire_map_fields(db, cycle=CYCLE, provider=provider)["status"] == "skipped"
    assert len(world.requests) == before
    forced = acquire_map_fields(db, now=NOW, provider=provider, force=True)
    assert forced["status"] == "success" and len(stored(db)) == 18


def test_a_short_run_of_a_cycle_does_not_stop_the_full_one(db: Session) -> None:
    # E.g. a live check with --max-lead-hours 0, then the scheduled job for the same cycle.
    world = standalone_world()
    short = acquire_map_fields(db, cycle=CYCLE, provider=fields_only(world, 0))
    assert short["status"] == "success" and short["map_hours"] == 1
    full = acquire_map_fields(db, cycle=CYCLE, provider=fields_only(world, 24))
    assert full["status"] == "success" and full["run_id"] == short["run_id"]
    assert (full["map_hours"], full["complete_hours"], full["fields"]) == (3, 3, 18)
    assert map_fields_service.field_index(db).valid_times == TIMES
    # Now every requested hour was read: skipped, and so is a shorter request.
    before = len(world.requests)
    for lead in (24, 12):
        again = acquire_map_fields(db, cycle=CYCLE, provider=fields_only(world, lead))
        assert again["status"] == "skipped" and again["message"] == "already stored"
    assert len(world.requests) == before


def test_standalone_job_skips_a_cycle_the_noaa_run_already_stored(db: Session) -> None:
    keep_spots(db, KEEP)
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS)
    noaa = NoaaGfsProvider(field_settings(), client=world.client(), backoff_seconds=0)
    acquire_from_provider(db, noaa, now=NOW)
    out = acquire_map_fields(db, now=NOW, provider=fields_only(world))
    assert out["status"] == "skipped" and out["message"] == "fields of this cycle already stored"
    fields_source = db.scalar(
        select(ForecastSource).where(ForecastSource.code == "noaa_gfs_fields")
    )
    assert fields_source is not None
    runs = db.scalars(select(ForecastRun).where(ForecastRun.source_id == fields_source.id))
    assert runs.all() == []


def test_standalone_job_partial_and_failed(db: Session) -> None:
    world = standalone_world()
    world.fail_url(PATHS.wave(CYCLE, "global.0p25", 12), 404)
    out = acquire_map_fields(db, now=NOW, provider=fields_only(world))
    assert out["status"] == "partial" and out["complete_hours"] == 2
    assert any(w.startswith("f012 map fields wave global.0p25:") for w in out["warnings"])
    run = db.get(ForecastRun, out["run_id"])
    assert run is not None and run.status == ForecastRunStatus.PARTIAL

    empty = acquire_map_fields(db, now=NOW, provider=fields_only(SyntheticGfs()))
    assert empty["status"] == "failed" and "no complete NOAA GFS cycle" in empty["message"]


# --------------------------------------------------------------------------- isolation
def _snapshot(db: Session, client: ApiClient) -> dict[str, Any]:
    """Everything spot-facing: run selection, status, API pages, scored rows."""
    db.expire_all()

    def count(model: type) -> int | None:
        return db.scalar(select(func.count()).select_from(model))

    return {
        "latest_runs": {k: v.id for k, v in latest_runs_by_spot(db).items()},
        "candidates": [r.id for r in candidate_runs(db, require_predictions=False)],
        "status": forecast_status(db, now=NOW),
        "spots": client.get("/api/spots").json(),
        "detail": client.get("/api/spots/pipeline").json(),
        "forecast": client.get("/api/spots/pipeline/forecast").json(),
        "map": client.get("/api/map/conditions").json(),
        "events": client.get("/api/events").json(),
        "accuracy": client.get("/api/accuracy/overview").json(),
        "predictions": count(SurfQualityPrediction),
        "archive": count(PredictionArchive),
        "swell_events": count(SwellEvent),
        "wave_forecasts": count(WaveForecast),
    }


def _differences(a: Any, b: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Leaf paths where two JSON-like values differ (readable assertion output)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [
            d
            for k in sorted(set(a) | set(b))
            for d in _differences(a.get(k), b.get(k), f"{path}.{k}")
        ]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for d in _differences(x, y, f"{path}[{i}]")
        ]
    return [] if a == b else [(path, a, b)]


def _generic(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


def test_a_fields_only_run_never_changes_spot_forecasts(
    db: Session, few_spots: list[str], app_client: ApiClient
) -> None:
    run_pipeline(db)
    first = _snapshot(db, app_client)
    run_pipeline(db, acquire=False)  # control: re-running the stages alone
    before = _snapshot(db, app_client)
    assert before["latest_runs"] and before["predictions"] > 0

    out = acquire_map_fields(db, now=NOW, provider=fields_only(standalone_world()))
    assert out["status"] == "success"
    fields_run = db.get(ForecastRun, out["run_id"])
    assert fields_run is not None
    # Every stage after acquisition runs again with the fields-only run present.
    stages = run_pipeline(db, acquire=False)
    assert stages["predictions"]["runs"] == 0  # the fields run is never scored
    after = _snapshot(db, app_client)

    # Re-running detection alone bumps each event's version and last_updated_at; those
    # fields (and only those) are bookkeeping and ignored. Everything else is identical.
    bookkeeping = {_generic(path) for path, _, _ in _differences(first, before)}
    assert {path.rsplit(".", 1)[-1] for path in bookkeeping} <= {"version", "last_updated_at"}
    changed = [d for d in _differences(before, after) if _generic(d[0]) not in bookkeeping]
    assert changed == []
    assert fields_run.id not in after["candidates"]
    assert "noaa_gfs_fields" not in {s["code"] for s in after["status"]["sources"]}
    assert compute_pending_predictions(db)["runs"] == 0


# --------------------------------------------------------------------------- API
def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_index_without_fields_says_what_to_set(app_client: ApiClient) -> None:
    resp = app_client.get("/api/map/fields")
    assert resp.status_code == 200
    # The endpoint's own header wins: the security middleware adds "no-store" only to API
    # responses that set no Cache-Control themselves (otherwise browsers never cache fields).
    assert resp.headers.get_list("cache-control") == ["public, max-age=300"]
    assert app_client.get("/api/spots").headers.get_list("cache-control") == [
        SPOT_LIST_CACHE_CONTROL
    ]
    body = resp.json()
    assert body["available"] is False and body["valid_times"] == [] and body["source"] is None
    assert "FORECAST_PROVIDERS" in body["message"] and "MAP_FIELDS_ENABLED" in body["message"]
    assert app_client.get("/api/map/fields/hs").status_code == 404


def test_index_lists_the_complete_valid_times(
    db: Session, fields_run: ForecastRun, app_client: ApiClient
) -> None:
    body = app_client.get("/api/map/fields").json()
    assert body["available"] is True and body["message"] is None
    assert body["source"] == {
        "code": "noaa_gfs_fields",
        "name": "NOAA GFS-Wave + GFS global fields",
        "attribution": fields_run.source.attribution,
        "is_demo": False,
    }
    assert "NODD" in body["source"]["attribution"]
    assert body["run_key"] == fields_run.run_key and _iso(body["cycle_time"]) == CYCLE
    assert [_iso(t) for t in body["valid_times"]] == TIMES
    variables = {v["name"]: v for v in body["variables"]}
    assert list(variables) == list(CORE_MAP_FIELD_VARIABLES)
    assert variables["hs"]["unit"] == "m" and "not surf height" in variables["hs"]["label"]
    ranges = {k: (v["display_min"], v["display_max"]) for k, v in variables.items()}
    assert ranges == {
        "hs": (0.0, 10.0),
        "tp": (0.0, 20.0),
        "wave_dir": (0.0, 360.0),
        "wind_u": (-30.0, 30.0),
        "wind_v": (-30.0, 30.0),
        "pressure": (960.0, 1045.0),
    }

    # A valid time missing one variable is not listed (and not served).
    db.execute(
        delete(MapField).where(MapField.variable == "pressure", MapField.valid_time == TIMES[2])
    )
    db.commit()
    body = app_client.get("/api/map/fields").json()
    assert [_iso(t) for t in body["valid_times"]] == TIMES[:2]
    late = app_client.get("/api/map/fields/hs", params={"at": TIMES[2].isoformat()})
    assert late.status_code == 404
    # An instant that cannot be expressed in UTC has no field: 404, never a 500 (SEC-4).
    for at in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
        far = app_client.get("/api/map/fields/hs", params={"at": at})
        assert far.status_code == 404, (at, far.text)


def test_field_bytes_and_their_metadata(
    db: Session, fields_run: ForecastRun, app_client: ApiClient
) -> None:
    resp = app_client.get("/api/map/fields/hs", params={"at": TIMES[0].isoformat()})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    # Without ``run`` the current run is served, which the next cycle replaces: 5 minutes
    # (a URL with ``run`` never changes and is cached for an hour, see the run tests).
    assert resp.headers.get_list("cache-control") == ["public, max-age=300"]
    assert resp.headers["access-control-expose-headers"] == "X-Field-Meta"
    row = db.scalar(
        select(MapField).where(
            MapField.run_id == fields_run.id,
            MapField.variable == "hs",
            MapField.valid_time == TIMES[0],
        )
    )
    assert row is not None and resp.content == row.data
    meta = json.loads(resp.headers["x-field-meta"])
    assert _iso(meta.pop("valid_time")) == row.valid_time
    assert meta == {
        "variable": "hs",
        "group": "core",
        "unit": "m",
        "run_key": row.run_key,
        "nx": row.nx,
        "ny": row.ny,
        "lat0": row.lat0,
        "lon0": row.lon0,
        "dlat": row.dlat,
        "dlon": row.dlon,
        "scale": row.scale,
        "offset": row.offset,
        "nodata": 255,
    }
    values = decode_bytes(resp.content, meta["nx"], meta["ny"], meta["scale"], meta["offset"])
    node = TargetGrid(0.5).node(58.5, -3.5)  # in the synthetic global.0p25 band
    assert values[node] == pytest.approx(2.0, abs=0.05)  # synthetic HTSGW at f000
    wind = app_client.get("/api/map/fields/wind_u", params={"at": TIMES[0].isoformat()})
    wind_meta = json.loads(wind.headers["x-field-meta"])
    assert (wind_meta["unit"], wind_meta["offset"], wind_meta["scale"]) == ("m/s", -63.5, 0.5)
    assert app_client.get("/api/map/fields/sst").status_code == 422


@pytest.mark.parametrize(
    ("offset_hours", "expected_hour"),
    [
        (-2, 0),  # before the run, within one field step (3 h)
        (5, 0),
        (6, 0),  # a tie goes to the earlier time
        (7, 12),
        (26, 24),  # after the run, within one field step
        (-4, None),  # more than one step before the first time
        (28, None),  # more than one step after the last time
    ],
)
def test_the_nearest_valid_time_is_served(
    fields_run: ForecastRun, app_client: ApiClient, offset_hours: int, expected_hour: int | None
) -> None:
    at = CYCLE + timedelta(hours=offset_hours)
    resp = app_client.get("/api/map/fields/tp", params={"at": at.isoformat()})
    if expected_hour is None:
        assert resp.status_code == 404
        return
    assert resp.status_code == 200
    meta = json.loads(resp.headers["x-field-meta"])
    assert _iso(meta["valid_time"]) == CYCLE + timedelta(hours=expected_hour)


@pytest.mark.parametrize(
    ("offset_hours", "expected_hour"),
    [
        (126 + 5, 126),  # after f120 the default MAP_FIELD_STEPS step is 6 h, not 3 h
        (126 + 7, None),
        (120 - 2, 120),
    ],
)
def test_the_tolerance_beyond_the_last_time_is_the_map_step_there(
    db: Session,
    app_client: ApiClient,
    monkeypatch: pytest.MonkeyPatch,
    offset_hours: int,
    expected_hour: int | None,
) -> None:
    provider = fields_only(standalone_world(hours=[120, 126]), map_lead=240)
    monkeypatch.setattr(provider, "map_hours", lambda: [120, 126])  # two long-lead hours
    assert acquire_map_fields(db, now=NOW, provider=provider)["status"] == "success"
    at = CYCLE + timedelta(hours=offset_hours)
    resp = app_client.get("/api/map/fields/hs", params={"at": at.isoformat()})
    if expected_hour is None:
        assert resp.status_code == 404
        return
    assert resp.status_code == 200
    meta = json.loads(resp.headers["x-field-meta"])
    assert _iso(meta["valid_time"]) == CYCLE + timedelta(hours=expected_hour)


def test_naive_and_default_times_are_utc(
    fields_run: ForecastRun, app_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    naive = app_client.get("/api/map/fields/hs", params={"at": "2026-10-11T07:00:00"})
    assert _iso(json.loads(naive.headers["x-field-meta"])["valid_time"]) == TIMES[1]
    monkeypatch.setattr(map_fields_service, "utcnow", lambda: CYCLE + timedelta(hours=20))
    default = app_client.get("/api/map/fields/hs")
    assert _iso(json.loads(default.headers["x-field-meta"])["valid_time"]) == TIMES[2]


# --------------------------------------------------------------------------- partitions
@pytest.fixture
def partition_run(db: Session) -> ForecastRun:
    """A standalone fields run of the synthetic 18Z cycle with MAP_FIELD_PARTITIONS=on."""
    provider = fields_only(standalone_world(), partitions="on")
    out = acquire_map_fields(db, now=NOW, provider=provider)
    assert out["status"] == "success" and out["fields"] == 3 * 15, out
    run = db.get(ForecastRun, out["run_id"])
    assert run is not None
    return run


def test_a_six_variable_run_lists_no_partitions(
    fields_run: ForecastRun, app_client: ApiClient
) -> None:
    body = app_client.get("/api/map/fields").json()
    assert [v["name"] for v in body["variables"]] == list(CORE_MAP_FIELD_VARIABLES)
    assert {(v["group"], v["system"]) for v in body["variables"]} == {("core", None)}
    assert [_iso(t) for t in body["valid_times"]] == TIMES
    resp = app_client.get("/api/map/fields/swell1_dir", params={"at": TIMES[0].isoformat()})
    assert resp.status_code == 404


def test_partitions_are_listed_and_served_when_stored(
    db: Session, partition_run: ForecastRun, app_client: ApiClient
) -> None:
    summary = partition_run.details["map_fields"]
    assert summary["partition_times"] == summary["valid_times"] == [t.isoformat() for t in TIMES]
    body = app_client.get("/api/map/fields").json()
    variables = {v["name"]: v for v in body["variables"]}
    assert list(variables) == list(CORE_MAP_FIELD_VARIABLES) + list(PARTITION_MAP_FIELD_VARIABLES)
    systems = {v["name"]: (v["group"], v["system"]) for v in body["variables"]}
    assert systems["swell1_hs"] == ("partition", "primary")
    assert systems["swell2_tp"] == ("partition", "secondary")
    assert systems["windsea_dir"] == ("partition", "windsea")
    assert systems["hs"] == ("core", None)
    assert variables["swell1_dir"]["unit"] == "deg" and "from" in variables["swell1_dir"]["label"]
    # Partition periods are "period" (NCEP: mean periods), never "peak period".
    assert variables["swell1_tp"]["label"] == "Primary swell period"
    assert "largest swell partition" in variables["swell1_hs"]["label"]

    resp = app_client.get("/api/map/fields/swell1_dir", params={"at": TIMES[0].isoformat()})
    assert resp.status_code == 200
    meta = json.loads(resp.headers["x-field-meta"])
    assert (meta["variable"], meta["group"], meta["unit"]) == ("swell1_dir", "partition", "deg")
    assert (meta["scale"], meta["offset"], meta["nodata"]) == (1.5, 0.0, 255)
    values = decode_bytes(resp.content, meta["nx"], meta["ny"], meta["scale"], meta["offset"])
    t = TargetGrid(0.5)
    assert values[t.node(58.5, -3.5)] == pytest.approx(290.0, abs=0.75)  # synthetic SWDIR 1
    raw = zlib.decompress(resp.content)
    assert raw[t.node(58.5, 20.0)[0] * t.nx + t.node(58.5, 20.0)[1]] == 255  # no wave data

    # A valid time missing only a partition stays complete (core) and listed; that
    # partition is then not found at that time.
    db.execute(
        delete(MapField).where(
            MapField.variable.in_(["swell2_hs", "swell2_tp", "swell2_dir"]),
            MapField.valid_time == TIMES[1],
        )
    )
    db.commit()
    body = app_client.get("/api/map/fields").json()
    assert [_iso(t) for t in body["valid_times"]] == TIMES
    assert "swell2_dir" in {v["name"] for v in body["variables"]}  # still stored elsewhere
    gone = app_client.get("/api/map/fields/swell2_dir", params={"at": TIMES[1].isoformat()})
    assert gone.status_code == 404
    kept = app_client.get("/api/map/fields/swell1_dir", params={"at": TIMES[1].isoformat()})
    assert kept.status_code == 200
    # A system stored nowhere any more is not listed.
    db.execute(delete(MapField).where(MapField.variable == "windsea_tp"))
    db.commit()
    names = {v["name"] for v in app_client.get("/api/map/fields").json()["variables"]}
    assert not names & {"windsea_hs", "windsea_tp", "windsea_dir"}


def test_the_byte_budget_leaves_a_partial_run(db: Session) -> None:
    probe = fields_only(standalone_world(), grib_download_workers=1)
    probe.fetch([], probe.run_for_cycle(CYCLE))
    budget = probe.last_step_bytes[0] + probe.last_step_bytes[12] // 2
    provider = fields_only(standalone_world(), grib_download_workers=1, max_bytes=budget)
    out = acquire_map_fields(db, now=NOW, provider=provider)
    assert out["status"] == "partial" and out["complete_hours"] == 1
    assert out["downloaded_bytes"] <= budget
    assert any("MAP_FIELD_MAX_BYTES_PER_RUN" in w for w in out["warnings"])
    run = db.get(ForecastRun, out["run_id"])
    assert run is not None and run.status == ForecastRunStatus.PARTIAL
    assert map_fields_service.field_index(db).valid_times == [CYCLE]


# --------------------------------------------------------------------------- run parameter
def test_the_run_parameter_serves_a_retained_run(
    db: Session, fields_run: ForecastRun, app_client: ApiClient
) -> None:
    newer = CYCLE + timedelta(hours=6)
    out = acquire_map_fields(db, cycle=newer, provider=fields_only(standalone_world(newer, [0]), 0))
    assert out["status"] == "success"
    assert app_client.get("/api/map/fields").json()["run_key"] == "noaa-gfs-fields:2026101100"

    # The older run is still retained (two runs are kept): its own bytes are served.
    params = {"at": TIMES[1].isoformat(), "run": fields_run.run_key}
    resp = app_client.get("/api/map/fields/hs", params=params)
    assert resp.status_code == 200
    meta = json.loads(resp.headers["x-field-meta"])
    assert meta["run_key"] == fields_run.run_key and _iso(meta["valid_time"]) == TIMES[1]
    row = db.scalar(
        select(MapField.data).where(
            MapField.run_id == fields_run.id,
            MapField.variable == "hs",
            MapField.valid_time == TIMES[1],
        )
    )
    assert resp.content == row
    # A run's field never changes: cached for an hour, and public.
    assert resp.headers.get_list("cache-control") == ["public, max-age=3600"]
    # Without run, the current run: its only valid time is the nearest one.
    current = app_client.get("/api/map/fields/hs", params={"at": newer.isoformat()})
    assert json.loads(current.headers["x-field-meta"])["run_key"] == out["run_key"]

    # An unknown run is "not retained", never another run's field.
    unknown = app_client.get("/api/map/fields/hs", params={"run": "noaa-gfs-fields:2026100100"})
    assert unknown.status_code == 404
    error = unknown.json()["error"]
    assert error["code"] == "run_not_retained" and "run not retained" in error["message"]
    assert unknown.headers.get_list("cache-control") == ["no-store"]

    # Once a third cycle arrives, the oldest run's fields are pruned: no longer retained.
    third = newer + timedelta(hours=6)
    acquire_map_fields(db, cycle=third, provider=fields_only(standalone_world(third, [0]), 0))
    pruned = app_client.get("/api/map/fields/hs", params=params)
    assert pruned.status_code == 404 and pruned.json()["error"]["code"] == "run_not_retained"


def test_a_run_without_a_complete_time_is_not_retained(
    db: Session, fields_run: ForecastRun, app_client: ApiClient
) -> None:
    db.execute(delete(MapField).where(MapField.variable == "tp"))
    db.commit()
    resp = app_client.get("/api/map/fields/hs", params={"run": fields_run.run_key})
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "run_not_retained"


# --------------------------------------------------------------------------- freshness
def test_the_index_reports_its_age_and_staleness(
    db: Session, fields_run: ForecastRun, app_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = map_fields_service.field_index(db, CYCLE + timedelta(hours=6, minutes=30))
    assert (fresh.age_hours, fresh.stale_after_hours, fresh.stale) == (6.5, 21.0, False)
    assert fresh.updated_at == fields_run.completed_at and fresh.updated_at is not None
    # A 00/12Z cycle is normally 17-20 h old before the next one replaces it, so 20 h is
    # not stale with the default MAP_FIELD_STALE_AFTER_HOURS=21.
    normal = map_fields_service.field_index(db, CYCLE + timedelta(hours=20))
    assert (normal.age_hours, normal.stale) == (20.0, False)

    monkeypatch.setattr("app.api.map_fields.utcnow", lambda: CYCLE + timedelta(hours=22))
    body = app_client.get("/api/map/fields").json()
    assert body["stale"] is True and body["age_hours"] == 22.0
    assert body["stale_after_hours"] == 21.0 and body["available"] is True
    assert _iso(body["updated_at"]) == fields_run.completed_at
    # Stale layers are still served (the client shows the badge).
    assert (
        app_client.get("/api/map/fields/hs", params={"at": TIMES[0].isoformat()}).status_code == 200
    )


def test_an_empty_index_is_not_stale(app_client: ApiClient) -> None:
    body = app_client.get("/api/map/fields").json()
    assert body["available"] is False and body["stale"] is False
    assert body["age_hours"] is None and body["updated_at"] is None
    assert body["stale_after_hours"] == 21.0


@pytest.fixture
def statements() -> Iterator[list[str]]:
    seen: list[str] = []

    def capture(*args: Any) -> None:
        seen.append(str(args[2]))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    yield seen
    event.remove(engine, "before_cursor_execute", capture)


def test_index_and_one_field_never_load_other_fields_bytes(
    db: Session, fields_run: ForecastRun, statements: list[str]
) -> None:
    map_fields_service.field_index(db)
    assert statements and not any("map_fields.data" in s for s in statements)
    statements.clear()
    found = map_fields_service.field_bytes(db, "hs", TIMES[1])
    assert found is not None
    loads = [s for s in statements if "map_fields.data" in s]
    assert len(loads) == 1 and "map_fields.id =" in loads[0]


def test_the_newest_run_with_a_complete_time_is_served(
    db: Session, fields_run: ForecastRun
) -> None:
    newer = CYCLE + timedelta(hours=6)
    out = acquire_map_fields(db, cycle=newer, provider=fields_only(standalone_world(newer, [0]), 0))
    assert out["status"] == "success"
    assert map_fields_service.field_index(db).run_key == "noaa-gfs-fields:2026101100"
    db.execute(delete(MapField).where(MapField.run_id == out["run_id"], MapField.variable == "tp"))
    db.commit()
    # The newer run has no complete valid time any more: the older one is served.
    index = map_fields_service.field_index(db)
    assert index.run_key == fields_run.run_key and index.valid_times == TIMES


def test_of_two_runs_of_one_cycle_the_more_complete_is_served(
    db: Session, fields_run: ForecastRun
) -> None:
    # A NOAA spot run of the same cycle with map fields (e.g. after a change of mode).
    keep_spots(db, KEEP)
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS)
    noaa = NoaaGfsProvider(field_settings(), client=world.client(), backoff_seconds=0)
    newer = acquire_from_provider(db, noaa, now=NOW)
    assert newer.run_id is not None and newer.run_id > fields_run.id
    # Same cycle and the same three complete times: the newer run wins the tie.
    assert map_fields_service.field_index(db).run_key == "noaa-gfs:2026101018"
    # One variable fewer at its last time: the more complete fields-only run is served.
    db.execute(
        delete(MapField).where(
            MapField.run_id == newer.run_id,
            MapField.valid_time == TIMES[2],
            MapField.variable == "hs",
        )
    )
    db.commit()
    index = map_fields_service.field_index(db)
    assert index.run_key == fields_run.run_key and index.valid_times == TIMES
    found = map_fields_service.field_bytes(db, "hs", TIMES[2])
    assert found is not None and found[0].run_key == fields_run.run_key


# --------------------------------------------------------------------------- retention
def test_cleanup_keeps_the_fields_of_the_two_newest_runs(db: Session) -> None:
    cycles = [CYCLE - timedelta(hours=12), CYCLE - timedelta(hours=6), CYCLE]
    pruned = []
    for cycle in cycles:
        world = standalone_world(cycle, [0])
        out = acquire_map_fields(db, cycle=cycle, provider=fields_only(world, 0))
        assert out["fields"] == 6
        pruned.append(out["pruned_fields"])
    # Pruned right after storing, not only by the daily cleanup: two runs keep fields.
    assert pruned == [0, 0, 6]
    assert {r.cycle_time for r in stored(db)} == set(cycles[1:])
    oldest = db.scalar(select(ForecastRun).where(ForecastRun.issued_at == cycles[0]))
    assert oldest is not None
    old = datetime.now(UTC) - timedelta(days=get_settings().retention_forecast_days + 1)
    db.execute(update(ForecastRun).where(ForecastRun.id == oldest.id).values(started_at=old))
    db.commit()

    out = cleanup(db)

    assert out["map_fields"] == 0
    assert {r.cycle_time for r in stored(db)} == set(cycles[1:])
    assert out["forecast_runs"] == 1  # the run's own retention (and cascade) still applies
    assert db.scalar(select(func.count()).select_from(ForecastRun)) == 2


def _copy_fields(db: Session, rows: list[MapField], key: str, cycle: datetime) -> None:
    """Store ``rows`` again under a new run ``key`` of ``cycle`` (same synthetic bytes)."""
    run = ForecastRun(
        source_id=db.scalars(select(ForecastRun.source_id)).first(),
        run_key=key,
        issued_at=cycle,
        started_at=NOW,
        status=ForecastRunStatus.SUCCESS,
        details={},
    )
    db.add(run)
    db.flush()
    columns = [c.key for c in MapField.__table__.columns if c.key != "id"]
    for r in rows:
        values = {c: getattr(r, c) for c in columns}
        db.add(MapField(**{**values, "run_id": run.id, "run_key": key, "cycle_time": cycle}))
    db.commit()


def test_pruning_never_deletes_fields_another_acquisition_commits_meanwhile(
    db: Session, fields_run: ForecastRun
) -> None:
    # E.g. the CLI and the Celery job storing two cycles at once: the newer cycle's fields
    # are committed by another session just before this session's DELETE runs.
    rows = stored(db)
    newer = CYCLE + timedelta(hours=6)
    engine = get_engine()
    injected: list[int] = []

    def commit_newer_cycle(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        if statement.startswith("DELETE FROM map_fields") and not injected:
            injected.append(1)
            with SessionLocal() as other:
                _copy_fields(other, rows, "noaa-gfs-fields:2026101100", newer)

    event.listen(engine, "before_cursor_execute", commit_newer_cycle)
    try:
        pruned = prune_map_fields(db)
    finally:
        event.remove(engine, "before_cursor_execute", commit_newer_cycle)
    db.commit()

    assert injected and pruned == 0
    assert {r.cycle_time for r in stored(db)} == {CYCLE, newer}
    assert map_fields_service.field_index(db).run_key == "noaa-gfs-fields:2026101100"


def test_retention_never_deletes_the_served_fields(db: Session, fields_run: ForecastRun) -> None:
    # Two newer cycles whose fields lack hs at every hour (as when the wave file was
    # missing): no valid time has all six variables, so they can never be served.
    no_hs = [r for r in stored(db) if r.variable != "hs"]
    for back in (6, 12):
        _copy_fields(db, no_hs, f"no-hs-{back}", CYCLE + timedelta(hours=back))
    assert len(stored(db)) == 18 + 2 * 15

    # At the fixtures' own clock (runs started at NOW): with the real clock, from
    # 2026-10-20 23:40 UTC the 10-day run retention would delete the served run first.
    out = cleanup(db, now=NOW)

    # The newest two runs by cycle are the useless ones; the served run keeps its fields.
    assert out["map_fields"] == 2 * 15
    assert {r.run_id for r in stored(db)} == {fields_run.id} and len(stored(db)) == 18
    index = map_fields_service.field_index(db)
    assert index.available and index.run_key == fields_run.run_key and index.valid_times == TIMES


def test_retention_keeps_the_most_complete_run_of_the_served_cycle(
    db: Session, fields_run: ForecastRun
) -> None:
    # Two newer runs of the same cycle with only the first valid time.
    first = [r for r in stored(db) if r.valid_time == TIMES[0]]
    for key in ("copy-a", "copy-b"):
        _copy_fields(db, first, key, CYCLE)
    assert map_fields_service.field_index(db).run_key == fields_run.run_key  # 3 times beat 1

    out = cleanup(db, now=NOW)  # the fixtures' clock (see the test above)

    assert out["map_fields"] == 6  # copy-a goes; copy-b is the second best
    assert {r.run_key for r in stored(db)} == {fields_run.run_key, "copy-b"}
    assert map_fields_service.field_index(db).valid_times == TIMES


def test_a_failed_newer_cycle_leaves_the_served_fields_alone(
    db: Session, fields_run: ForecastRun
) -> None:
    newer = CYCLE + timedelta(hours=6)
    world = SyntheticGfs()
    for h in HOURS:
        world.add_atmos(newer, h)  # no wave files: wind and pressure only
    out = acquire_map_fields(db, cycle=newer, provider=fields_only(world))
    assert out["status"] == "failed" and out["fields"] == 9
    # Its rows could never be served and are pruned at once; the served run is untouched.
    assert out["pruned_fields"] == 9
    assert {r.run_id for r in stored(db)} == {fields_run.id}
    assert map_fields_service.field_index(db).run_key == fields_run.run_key


# --------------------------------------------------------------------------- jobs
def test_map_fields_task_is_a_no_op_unless_standalone(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models import BackgroundJobLog
    from app.workers import tasks

    result = tasks.acquire_map_fields_task.delay().get()
    assert result["status"] == "success"
    assert result["skipped"] == "map fields mode is 'off', not 'standalone'"
    log = db.scalar(
        select(BackgroundJobLog).where(BackgroundJobLog.job_name == "forecasts.map_fields")
    )
    assert log is not None and log.details["skipped"].startswith("map fields mode")

    original = get_settings()
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: original.model_copy(update={"map_fields_enabled": True}),
    )
    calls: list[Session] = []

    def fake(session: Session) -> dict[str, Any]:  # stands in for the NOAA download
        calls.append(session)
        return {"status": "skipped", "message": "stub"}

    monkeypatch.setattr("app.services.forecasts.ingestion.acquire_map_fields", fake)
    result = tasks.acquire_map_fields_task.delay().get()
    assert len(calls) == 1 and result["message"] == "stub"


def test_cli_map_fields(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.cli import main as cli_main

    assert cli_main(["engine", "map-fields"]) == 1  # mode "off" in the tests
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "off" and "MAP_FIELDS_ENABLED" in refused["message"]
    # Unset MAP_FIELDS_ENABLED is standalone with any live provider, not only with noaa_gfs.
    assert "live FORECAST_PROVIDERS" in refused["message"]
    assert "add noaa_gfs" not in refused["message"]

    original = get_settings()
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: original.model_copy(update={"map_fields_enabled": True}),
    )
    seen: dict[str, Any] = {}

    def fake(session: Session, **kw: Any) -> dict[str, Any]:  # no NOAA download in tests
        seen.update(kw)
        return {"status": "success", "fields": 30, "duration_s": 1.0}

    monkeypatch.setattr("app.services.forecasts.ingestion.acquire_map_fields", fake)
    args = ["engine", "map-fields", "--cycle", "2026101018", "--max-lead-hours", "12"]
    assert cli_main(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"mode": "standalone", "status": "success", "fields": 30, "duration_s": 1.0}
    assert seen == {"cycle": CYCLE, "max_lead_hours": 12, "force": False}
    with pytest.raises(SystemExit):
        cli_main(["engine", "map-fields", "--cycle", "2026-10-10"])
    with pytest.raises(SystemExit):
        cli_main(["engine", "map-fields", "--max-lead-hours", "400"])
