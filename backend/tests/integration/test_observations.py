"""Observation stations, buoy ingestion (QC precedence) and point-forecast archiving (DB).

Observation values here are SYNTHETIC unless stated otherwise. The GFS-Wave bulletin used
by the archive tests is a REAL, trimmed NOAA file (public domain) from
https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261010/00/wave/station/bulls.t00z/gfswave.62163.bull
served through httpx.MockTransport.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import BuoyObservation, BuoyStation, PointForecast
from app.services.http import PermanentProviderError
from app.services.observations import ingest as obs_ingest
from app.services.observations.ingest import ingest_all, ingest_range, upsert_observations
from app.services.observations.records import ObservationRecord
from app.services.observations.stations import (
    CatalogError,
    load_catalog,
    seed_stations,
    validate_catalog,
)
from app.services.verification.point_forecasts import collect_range, collect_recent
from tests.unit.test_observation_parsers import _row, aodn_parquet

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observations"
BULL_62163 = (FIXTURES / "gfswave.62163.20261010t00z.trimmed.bull").read_text()
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    yield monkeypatch
    get_settings.cache_clear()


def set_env(mp: pytest.MonkeyPatch, **values: str) -> None:
    for k, v in values.items():
        mp.setenv(k, v)
    get_settings.cache_clear()


def station(db: Session, code: str) -> BuoyStation:
    row = db.scalar(select(BuoyStation).where(BuoyStation.code == code))
    assert row is not None
    return row


def rows_of(db: Session, station_id: int) -> list[BuoyObservation]:
    db.expire_all()
    return list(
        db.scalars(
            select(BuoyObservation)
            .where(BuoyObservation.station_id == station_id)
            .order_by(BuoyObservation.observed_at)
        ).all()
    )


# ------------------------------------------------------------------------------ catalog


def test_catalog_seeds_and_matches_spot_positions(db: Session) -> None:
    out = seed_stations(db)
    catalog = load_catalog()
    n = len(catalog["stations"])
    assert out == {"stations": n, "created": n, "updated": 0}
    cape = station(db, "aodn:cape-naturaliste")
    assert cape.license == "CC BY 4.0" and "IMOS" in cape.attribution
    assert cape.water_depth_m == 50
    meta = cape.metadata_
    assert meta["bulletin_id"] == "56006"
    assert {p["slug"] for p in meta["paired_spots"]} == {
        "margaret-river-main-break",
        "the-box",
        "north-point-gracetown",
    }
    assert all(p["note"] for p in meta["paired_spots"])
    waimea = station(db, "ndbc:51201")
    assert {p["slug"] for p in waimea.metadata_["paired_spots"]} == {
        "pipeline",
        "sunset-beach",
        "waimea-bay",
    }
    assert station(db, "metoffice:brittany").license == "CC BY-SA 4.0"

    # Runtime keys written by the ingest survive re-seeding.
    cape.metadata_ = {**meta, "last_observation_at": "2026-10-10T21:00:00+00:00"}
    db.commit()
    again = seed_stations(db)
    assert again == {"stations": n, "created": 0, "updated": n}
    db.expire_all()
    assert (
        station(db, "aodn:cape-naturaliste")
        .metadata_["last_observation_at"]
        .startswith("2026-10-10")
    )


def test_catalog_validation_catches_bad_pairings(db: Session) -> None:
    catalog = json.loads(json.dumps(load_catalog()))
    spots = {"kirra": (-28.166, 153.524)}
    entry = catalog["stations"][0]
    entry["paired_spots"] = [
        {"slug": "kirra", "relation": "offshore_reference", "distance_km": 1.0, "note": "x"},
        {"slug": "nowhere", "relation": "offshore_reference", "distance_km": 1.0, "note": "x"},
        {"slug": "kirra", "relation": "vibes", "distance_km": 16.4, "note": ""},
    ]
    problems = validate_catalog(catalog, spots)
    assert any("distance to kirra" in p for p in problems)
    assert any("unknown spot 'nowhere'" in p for p in problems)
    assert any("unknown relation 'vibes'" in p for p in problems)
    assert any("no justification" in p for p in problems)
    path = Path(get_settings().outbox_dir) / "bad_catalog.json"
    path.write_text(json.dumps(catalog))
    with pytest.raises(CatalogError):
        seed_stations(db, path)


# ------------------------------------------------------------------------- QC precedence


def _rec(minutes: int, qc: str, hs: float, dataset: str = "x") -> ObservationRecord:
    return ObservationRecord(
        T0 + timedelta(minutes=minutes), qc, dataset, hs_m=hs, hs_kind="spectral_hm0"
    )


def test_upsert_qc_precedence(db: Session) -> None:
    seed_stations(db)
    sid = station(db, "aodn:apollo-bay").id

    first = upsert_observations(db, sid, [_rec(0, "not_qc", 1.0, "rt"), _rec(60, "good", 2.0)])
    db.commit()
    assert (first.inserted, first.updated) == (2, 0)

    # Delayed-mode 'good' 11 minutes after the realtime copy replaces it.
    s = upsert_observations(db, sid, [_rec(11, "good", 1.1, "dm")])
    db.commit()
    assert s.inserted == 1 and s.superseded_stored == 1
    assert [(r.observed_at.minute, r.qc_flag) for r in rows_of(db, sid)] == [
        (11, "good"),
        (0, "good"),
    ]

    # A later realtime copy (same time or within the window) never overwrites 'good'.
    s = upsert_observations(db, sid, [_rec(60, "not_qc", 9.0), _rec(5, "not_qc", 9.0)])
    db.commit()
    assert s.superseded_incoming == 2 and s.inserted == s.updated == 0
    assert [r.hs_m for r in rows_of(db, sid)] == [1.1, 2.0]

    # Same timestamp, at least as good: updated in place.
    s = upsert_observations(db, sid, [_rec(60, "good", 2.2)])
    db.commit()
    assert s.updated == 1
    assert rows_of(db, sid)[-1].hs_m == 2.2
    # ... and an identical record is reported as unchanged, not as an update.
    s = upsert_observations(db, sid, [_rec(60, "good", 2.2)])
    db.commit()
    assert (s.updated, s.unchanged) == (0, 1)

    # A delayed-mode rejection marker blocks the realtime copy of that measurement.
    marker = ObservationRecord(T0 + timedelta(hours=3), "bad", "dm")
    upsert_observations(db, sid, [marker])
    s = upsert_observations(db, sid, [_rec(180, "not_qc", 3.0, "rt")])
    db.commit()
    assert s.superseded_incoming == 1
    stored = rows_of(db, sid)[-1]
    assert stored.qc_flag == "bad" and stored.hs_m is None

    # Upgrading not_qc -> good at the same timestamp works.
    upsert_observations(db, sid, [_rec(300, "not_qc", 4.0, "rt")])
    s = upsert_observations(db, sid, [_rec(300, "good", 4.1, "dm")])
    db.commit()
    assert s.updated == 1
    assert (rows_of(db, sid)[-1].qc_flag, rows_of(db, sid)[-1].hs_m) == ("good", 4.1)


# ------------------------------------------------------------------------ ingestion (mock)


def _aodn_handler(content: bytes, site: str = "Cape%20Naturaliste") -> httpx.MockTransport:
    month = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
    key = (
        f"wave_buoy_realtime_nonqc.parquet/site_name={site}/timestamp={month}"
        "/polygon=01/SYNTHETIC.nc-0.parquet"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("list-type") == "2":
            body = "<ListBucketResult><IsTruncated>false</IsTruncated>"
            if request.url.params["prefix"].startswith(
                f"wave_buoy_realtime_nonqc.parquet/site_name={site}/"
            ):
                body += f"<Contents><Key>{key}</Key><Size>1</Size></Contents>"
            return httpx.Response(200, text=body + "</ListBucketResult>")
        if request.url.raw_path.endswith(b"_common_metadata"):
            return httpx.Response(200, content=content)
        if b"SYNTHETIC" in request.url.raw_path:
            return httpx.Response(200, content=content)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_ingest_range_end_to_end(db: Session, settings_env: pytest.MonkeyPatch) -> None:
    set_env(settings_env, AODN_BASE_URL="https://aodn.example")
    seed_stations(db)
    content = aodn_parquet(
        [
            _row(0, WSSH=None, WHTH=2.0, WPPE=14.0, WPDI=240.0),
            _row(180, WSSH=None, WHTH=2.4, WPPE=15.0, WPDI=235.0),
            _row(360, WSSH=None, WHTH=45.0, WPPE=15.0),  # impossible height, valid Tp
        ]
    )
    client = httpx.Client(transport=_aodn_handler(content))
    out = ingest_range(
        db, "aodn", T0, T0 + timedelta(days=1), ["aodn:cape-naturaliste"], client=client
    )
    res = out["stations"]["aodn:cape-naturaliste"]
    assert (res["files"], res["records"], res["inserted"]) == (1, 3, 3)
    assert res["parse"]["invalid_values"] == {"WHTH": 1}
    assert res["parse"]["no_wave_data"] == 0  # the row still had Tp
    assert res["latest_observation"].startswith("2026-10-01T06:00")
    cape = station(db, "aodn:cape-naturaliste")
    rows = rows_of(db, cape.id)
    # The third row kept its Tp (15 s) even though its height was impossible.
    assert [(r.hs_m, r.hs_kind, r.qc_flag) for r in rows] == [
        (2.0, "h_third", "not_qc"),
        (2.4, "h_third", "not_qc"),
        (None, "unknown", "not_qc"),
    ]
    assert cape.metadata_["last_reported_position"]["latitude"] == -33.53
    # Idempotent: the same window again inserts nothing new.
    again = ingest_range(
        db, "aodn", T0, T0 + timedelta(days=1), ["aodn:cape-naturaliste"], client=client
    )
    same = again["stations"]["aodn:cape-naturaliste"]
    assert (same["inserted"], same["updated"], same["unchanged"]) == (0, 0, 3)
    assert len(rows_of(db, cape.id)) == 3

    with pytest.raises(ValueError):
        ingest_range(db, "nope", T0, T0 + timedelta(days=1))
    with pytest.raises(ValueError):
        ingest_range(db, "aodn", T0, T0)


def test_ingest_all_reports_partial_failures(db: Session, settings_env: pytest.MonkeyPatch) -> None:
    assert ingest_all(db) == {"skipped": "OBSERVATIONS_ENABLED is false"}
    set_env(
        settings_env,
        OBSERVATIONS_ENABLED="true",
        OBSERVATION_PROVIDERS="aodn,metoffice",
        OBSERVATION_LOOKBACK_HOURS="48",
        AODN_BASE_URL="https://aodn.example",
        METOFFICE_BASE_URL="https://metoffice.example",
    )
    seed_stations(db)
    content = aodn_parquet([_row(0, WSSH=1.5, WPPE=12.0)])
    aodn_transport = _aodn_handler(content)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metoffice.example":
            return httpx.Response(503)  # transient, retried, then reported
        return aodn_transport.handle_request(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = ingest_all(db, now=T0 + timedelta(hours=12), client=client, backoff_seconds=0)
    assert out["status"] == "partial"
    assert out["failed"]["metoffice"]["transient"] is True
    assert out["providers"]["aodn"]["stations"]["aodn:cape-naturaliste"]["inserted"] == 1

    # Every provider failing makes the job fail (so the job log shows it).
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(obs_ingest.TransientProviderError):
        ingest_all(db, now=T0 + timedelta(hours=12), client=down, backoff_seconds=0)


# --------------------------------------------------------------- point forecast archive


def _bulletin_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/gfs.20261010/00/wave/station/bulls.t00z/gfswave.62163.bull":
            return httpx.Response(200, text=BULL_62163)
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    return httpx.MockTransport(handler)


def test_collect_range_is_idempotent(db: Session, settings_env: pytest.MonkeyPatch) -> None:
    set_env(settings_env, NOAA_GFS_BASE_URL="https://noaa.example")
    seed_stations(db)
    calls: list[str] = []
    client = httpx.Client(transport=_bulletin_transport(calls))
    start, end = datetime(2026, 10, 9, 12, tzinfo=UTC), datetime(2026, 10, 10, 0, tzinfo=UTC)
    n_bulletin_stations = len([s for s in load_catalog()["stations"] if s.get("bulletin_id")])

    first = collect_range(db, start, end, (0, 12), client=client, backoff_seconds=0)
    assert first["cycles"] == 2
    assert first["fetched"] == 1  # 62163 at 10 Oct 00Z
    assert first["missing"] == 2 * n_bulletin_stations - 1  # 404s are counted, not fatal
    assert first["errors"] == 0
    assert first["rows_inserted"] == 48  # the trimmed bulletin has 48 hourly rows
    assert first["rows_by_station"] == {"metoffice:brittany": 48}

    brittany = station(db, "metoffice:brittany")
    pf = db.scalars(
        select(PointForecast)
        .where(PointForecast.station_id == brittany.id)
        .order_by(PointForecast.valid_time)
    ).all()
    assert pf[0].source_code == "gfswave_bulletin" and pf[0].family == "ncep_gfs"
    assert pf[0].cycle_time == datetime(2026, 10, 10, 0, tzinfo=UTC)
    assert (pf[0].hs_m, pf[0].swell1_direction_deg, pf[0].tp_s) == (2.83, 301.0, None)
    assert pf[24].lead_hours == 24
    assert pf[0].distance_km == pytest.approx(2.26, abs=0.01)

    calls.clear()
    second = collect_range(db, start, end, (0, 12), client=client, backoff_seconds=0)
    assert second["already_stored"] == 1 and second["rows_inserted"] == 0
    assert "/gfs.20261010/00/wave/station/bulls.t00z/gfswave.62163.bull" not in calls
    total = db.scalar(select(func.count()).select_from(PointForecast))
    assert total == 48


def test_collect_recent(db: Session, settings_env: pytest.MonkeyPatch) -> None:
    assert collect_recent(db) == {"skipped": "VERIFICATION_ENABLED is false"}
    set_env(settings_env, VERIFICATION_ENABLED="true", NOAA_GFS_BASE_URL="https://noaa.example")
    seed_stations(db)
    calls: list[str] = []
    client = httpx.Client(transport=_bulletin_transport(calls))
    now = datetime(2026, 10, 10, 6, 30, tzinfo=UTC)  # 00Z should be out (5 h lag)
    out = collect_recent(db, now=now, client=client, backoff_seconds=0)
    assert out["latest_cycle"] == "2026-10-10T00:00:00+00:00"
    assert out["sources"]["gfswave_bulletin"]["rows_inserted"] == 48
    # Every download failing transiently fails the job.
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(obs_ingest.TransientProviderError):
        collect_recent(db, now=now + timedelta(days=3), client=down, backoff_seconds=0)
    # A 403 on every file is a refusal (wrong bucket, policy), not "not published yet":
    # counted as errors, and the job fails instead of reporting zero bulletins as success.
    refused = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    later = now + timedelta(days=6)
    outcome = collect_range(
        db, later - timedelta(hours=12), later, (0, 12), client=refused, backoff_seconds=0
    )
    assert outcome["missing"] == 0 and outcome["errors"] > 0 and outcome["fetched"] == 0
    assert "HTTP 403" in outcome["error_samples"][0]
    with pytest.raises(PermanentProviderError):
        collect_recent(db, now=later, client=refused, backoff_seconds=0)


def test_collect_range_reports_malformed_cells_and_bad_ids(
    db: Session, settings_env: pytest.MonkeyPatch
) -> None:
    """Rows that do not match the layout are counted and reported, not silently dropped;
    a bulletin id that is not a plain station id is never put into a URL."""
    set_env(settings_env, NOAA_GFS_BASE_URL="https://noaa.example")
    seed_stations(db)
    # SYNTHETIC damage to the real trimmed bulletin: one Hst cell and one partition cell.
    lines = BULL_62163.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith(" | 10  3 |"))
    lines[i] = lines[i].replace("2.65  1  ", "x.xx  1  ")
    lines[i + 1] = lines[i + 1].replace("2.59 10.1 121", "2.59 10.1 abc")
    damaged = "\n".join(lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gfs.20261010/00/wave/station/bulls.t00z/gfswave.62163.bull"):
            return httpx.Response(200, text=damaged)
        return httpx.Response(404)

    brittany = station(db, "metoffice:brittany")
    brittany.metadata_ = {**brittany.metadata_, "bulletin_id": "../../evil"}
    db.commit()
    cycle = datetime(2026, 10, 10, 0, tzinfo=UTC)
    out = collect_range(
        db,
        cycle,
        cycle,
        (0,),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        backoff_seconds=0,
    )
    assert out["errors"] == 1 and "invalid GFS-Wave bulletin id" in out["error_samples"][0]
    assert out["fetched"] == 0  # the refused id was never requested

    brittany.metadata_ = {**brittany.metadata_, "bulletin_id": "62163"}
    db.commit()
    out = collect_range(
        db,
        cycle,
        cycle,
        (0,),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        backoff_seconds=0,
    )
    assert out["fetched"] == 1 and out["malformed_cells"] == 2
    assert any("2 malformed" in s for s in out["error_samples"])
    # The damaged Hst row is skipped; the row with the bad partition keeps its total Hs.
    assert out["rows_inserted"] == 47


# ------------------------------------------------------------------------------------ CLI


def test_cli_seed_and_list(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    from app.cli import main

    assert main(["obs", "seed-stations"]) == 0
    seeded = json.loads(capsys.readouterr().out)
    assert seeded["created"] == len(load_catalog()["stations"])
    assert main(["obs", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    cape = next(s for s in listed if s["code"] == "aodn:cape-naturaliste")
    assert cape["bulletin_id"] == "56006" and cape["observations"] == 0
    assert main(["obs", "collect-forecasts", "--start", "2026-10-10", "--end", "2026-10-09"]) == 2
    with pytest.raises(SystemExit):
        main(["obs", "collect-forecasts", "--start", "2026-10-09", "--end", "x"])
