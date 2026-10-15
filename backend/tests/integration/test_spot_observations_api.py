"""The spot observations block: measured readings at the spot's reference stations only,
kept apart from forecasts, with station, distance, time, staleness and licence.

The readings are REAL NDBC rows (tests/fixtures/observations/ndbc_*_20261015.*, downloaded
2026-10-15 02:40 UTC; see tests/unit/test_ndbc_real_files.py), parsed by the NDBC adapter and
stored with the ingest's upsert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BuoyStation, SurfSpot
from app.schemas.tides import ObservationsBlockOut
from app.services.observations import ndbc
from app.services.observations.ingest import upsert_observations
from app.services.observations.records import ParseStats
from app.services.observations.stations import seed_stations
from app.services.tides import api as tide_api
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observations"
NOW = datetime(2026, 10, 15, 2, 40, tzinfo=UTC)
WIDE = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))


def _store(db: Session, code: str, txt: str, spec: str | None) -> int:
    stats = ParseStats()
    records = ndbc.parse_stdmet(
        (FIXTURES / txt).read_text(),
        dataset=ndbc.REALTIME_DATASET,
        start=WIDE[0],
        end=WIDE[1],
        stats=stats,
    )
    specs = (
        ndbc.parse_spec((FIXTURES / spec).read_text(), start=WIDE[0], end=WIDE[1]) if spec else {}
    )
    merged = ndbc.merge_spec(records, specs, stats)
    station = db.scalar(select(BuoyStation).where(BuoyStation.code == code))
    assert station is not None
    upsert_observations(db, station.id, merged)
    db.commit()
    return len(merged)


def _spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    return spot


def _block(db: Session, slug: str) -> ObservationsBlockOut:
    return tide_api.spot_observations_block(
        db, _spot(db, slug), NOW - timedelta(hours=48), NOW, NOW
    )


def test_a_buoy_without_recent_waves_is_shown_stale(db: Session) -> None:
    seed_stations(db)
    assert _store(
        db,
        "ndbc:46053",
        "ndbc_46053_20261015.realtime2.trimmed.txt",
        "ndbc_46053_20261015.realtime2.trimmed.spec",
    )
    block = _block(db, "rincon")
    assert block.status == "stale" and block.reason == tide_api.ALL_STALE
    # 46054 (upstream_regional) is not listed even though it is paired with Rincon.
    assert [i.station_code for i in block.items] == ["ndbc:46053"]
    item = block.items[0]
    assert item.t == datetime(2026, 10, 6, 5, 40, tzinfo=UTC)  # its last wave report
    assert item.stale and item.kind == "instrument"
    assert item.relation == "offshore_reference" and item.distance_km == 36.3
    assert item.hs_m is not None and item.hs_kind == "spectral_hm0"
    assert item.provider == "ndbc" and item.licence == "Public domain (US Government)"
    assert item.attribution


def test_recent_readings_are_listed_by_relation_and_distance(db: Session) -> None:
    seed_stations(db)
    n = _store(db, "ndbc:46026", "ndbc_46026_20261015.5day2.txt", "ndbc_46026_20261015.5day2.spec")
    assert n > 200
    block = _block(db, "ocean-beach-sf")
    # 46237 (nearshore_reference) has no stored reading, so only 46026 is listed;
    # 46214 (upstream_regional) never is.
    assert block.status == "available" and block.reason is None
    assert [i.station_code for i in block.items] == ["ndbc:46026"]
    item = block.items[0]
    assert item.t == datetime(2026, 10, 15, 1, 40, tzinfo=UTC) and not item.stale
    assert (item.hs_m, item.tp_s) == (1.3, 15.0)
    assert item.dir_from_deg == 232.0  # MWD: where the dominant waves come FROM
    assert item.wind_kmh is not None and item.qc_flag == "not_qc"
    # Three hours later the same reading is stale (NDBC limit: 3 h).
    later = tide_api.spot_observations_block(
        db,
        _spot(db, "ocean-beach-sf"),
        NOW - timedelta(hours=44),
        NOW + timedelta(hours=4),
        NOW + timedelta(hours=4),
    )
    assert later.status == "stale" and later.items[0].stale


def test_spots_without_reference_stations_say_so(app_client: ApiClient, db: Session) -> None:
    seed_stations(db)
    block = _block(db, "nazare")
    assert block.status == "unavailable" and block.reason == tide_api.NO_REFERENCE_STATIONS
    assert block.items == []
    # Thurso East is paired only with upstream regional buoys (Met Office K7, K5).
    thurso = _block(db, "thurso-east")
    assert thurso.status == "unavailable" and thurso.items == []
    # A paired station without any stored reading: unavailable, with the reason.
    rincon = _block(db, "rincon")
    assert rincon.status == "unavailable" and rincon.reason == tide_api.NO_RECENT_READINGS
    resp = app_client.get("/api/spots/rincon/observations")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "public, max-age=300"
    assert ObservationsBlockOut.model_validate(resp.json()).status == "unavailable"
