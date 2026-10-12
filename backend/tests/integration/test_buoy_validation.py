"""GEFS-Wave station statistics, `verify ranges` and `verify confidence-classes`.

Stations, observations and forecasts are SYNTHETIC test rows (tests/synthetic_waves.py)
except the GEFS-Wave bulletin text, which is a REAL NOAA file (public domain) served from a
synthetic tar through httpx.MockTransport:
tests/fixtures/observations/gefs.wave.51201.20261011t00z.bull, member of
https://noaa-gefs-pds.s3.amazonaws.com/gefs.20261011/00/wave/station/gefs.wave.t00z.bull_tar
"""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.cli import main as cli_main
from app.models import BuoyStation, PointForecast
from app.services.verification import gefs_bulletins
from tests.synthetic_waves import START, insert_case, make_case

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observations"
GEFS_TEXT = (FIXTURES / "gefs.wave.51201.20261011t00z.bull").read_bytes()
AVERAGES_ONLY = (FIXTURES / "gefs.wave.51201.20250115t00z.averages-only.bull").read_bytes()
CYCLE = datetime(2026, 10, 11, 0, tzinfo=UTC)


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _gefs_station(db: Session, code: str, bulletin_id: str) -> BuoyStation:
    station = BuoyStation(
        code=code,
        provider="synthetic",
        name="SYNTHETIC station for GEFS-Wave bulletins",
        latitude=21.67,  # invented test position near the bulletin's output point
        longitude=-158.12,
        metadata_={"bulletin_id": bulletin_id},
    )
    db.add(station)
    db.commit()
    return station


def test_gefs_collect_stores_mean_and_spread(db: Session) -> None:
    good = _gefs_station(db, "synthetic:gefs-a", "51201")
    _gefs_station(db, "synthetic:gefs-b", "46054")  # not in the tar
    _gefs_station(db, "synthetic:gefs-c", "51205")  # averages-only layout: refused
    requests: list[str] = []
    tar = _tar({"gefs.wave.51201.bull": GEFS_TEXT, "gefs.wave.51205.bull": AVERAGES_ONLY})

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if "gefs.20261011/00/" in str(request.url):
            return httpx.Response(200, content=tar)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    codes = ["synthetic:gefs-a", "synthetic:gefs-b", "synthetic:gefs-c"]
    out = gefs_bulletins.collect_range(
        db, CYCLE, CYCLE + timedelta(days=1), (0,), codes, client=client, backoff_seconds=0
    )
    assert out["tars_fetched"] == 1 and out["missing"] == 1  # 2026-10-12 00Z: 404
    assert out["not_in_tar"] == 1 and out["errors"] == 1
    assert out["error_samples"][0].startswith("synthetic:gefs-c 2026101100: bulletin is for")
    rows = db.scalars(
        select(PointForecast)
        .where(PointForecast.station_id == good.id)
        .order_by(PointForecast.lead_hours)
    ).all()
    assert out["rows_inserted"] == len(rows) == 81 + 8  # 3-hourly to 240 h, 6-hourly to 288
    first = rows[0]
    assert first.source_code == "gefswave_station" and first.family == "ncep_gefs"
    assert (first.hs_m, first.hs_spread_m, first.tp_s) == (1.95, 0.03, 15.4)
    assert first.direction_deg is None and first.extra["tp_spread_s"] == 0.03
    # Idempotent: stored cycles are not downloaded again.
    requests.clear()
    again = gefs_bulletins.collect_range(
        db, CYCLE, CYCLE, (0,), ["synthetic:gefs-a"], client=client, backoff_seconds=0
    )
    assert again["already_stored"] == 1 and again["tars_fetched"] == 0 and not requests
    # Cycles before the first one with spreads are not requested at all.
    early = datetime(2026, 2, 25, tzinfo=UTC)
    before = gefs_bulletins.collect_range(
        db, early, early, (0,), ["synthetic:gefs-a"], client=client, backoff_seconds=0
    )
    assert before["cycles"] == 0 and not requests


def test_collect_forecasts_cli_refuses_a_proxy_id_for_gefs(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["obs", "collect-forecasts", "--start", "2026-10-11T00:00:00Z"]
    args += ["--end", "2026-10-11T00:00:00Z", "--source", "gefswave_station"]
    assert cli_main([*args[:3], "2026-10-12T00:00:00Z", *args[4:]]) == 2  # start >= end
    assert (
        cli_main(
            [
                "obs",
                "collect-forecasts",
                "--start",
                "2026-10-10T00:00:00Z",
                "--end",
                "2026-10-11T00:00:00Z",
                "--source",
                "gefswave_station",
                "--bulletin-id",
                "51201",
            ]
        )
        == 2
    )
    assert "--bulletin-id applies to gfswave_bulletin only" in capsys.readouterr().err


@pytest.fixture
def synthetic_pairs(db: Session) -> dict[str, int]:
    """SYNTHETIC biased forecasts, 3-hourly leads to 72 h, 70 daily cycles, 2 stations,
    plus a SYNTHETIC 'ensemble' source with a spread of 10 % of the forecast."""
    case = make_case("bias", n_stations=2, days=70, lead_step=3, max_lead=72, seed=31)
    ids = insert_case(db, case)
    ens = case.forecasts.copy()
    ens["station_id"] = ens.pop("station_code").map(ids)
    ens["source_code"] = "synthetic_ens"
    ens["family"] = "ncep_gefs"
    ens["hs_spread_m"] = (0.1 * ens["hs_m"]).round(4)
    records = []
    for r in ens.itertuples(index=False):
        records.append(
            {
                "station_id": int(r.station_id),
                "source_code": r.source_code,
                "family": r.family,
                "cycle_time": pd.Timestamp(r.cycle_time).to_pydatetime(),
                "valid_time": pd.Timestamp(r.valid_time).to_pydatetime(),
                "lead_hours": float(r.lead_hours),
                "hs_m": float(r.hs_m),
                "hs_spread_m": float(r.hs_spread_m),
                "extra": {"synthetic": True},
            }
        )
    db.execute(insert(PointForecast), records)
    db.commit()
    return ids


def test_verify_ranges_cli(db: Session, synthetic_pairs: dict[str, int], tmp_path: Path) -> None:
    test_start = (START + timedelta(days=45)).isoformat()
    out = tmp_path / "ranges.json"
    base = ["verify", "ranges", "--source", "synthetic_gfswave", "--test-start", test_start]
    base += ["--station", "synthetic:station-1", "--station", "synthetic:station-2"]
    assert cli_main([*base, "--boot", "20", "--json", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["range"] == "heuristic (lead table)"
    split = result["split"]
    assert split["gap_hours"] == 72 + 72 and split["train_valid_end"] <= split["limit"]
    assert set(result["sigma_sources"]["test"]) == {"lead_table"}
    table = result["app_range"]["test"]["table"]
    assert table[0]["bucket"] == "all" and table[0]["n"] > 0
    assert 0.0 <= table[0]["coverage"] <= 1.0
    assert {"decision", "by_station"} <= set(result["fitted_range_test"])
    assert len(result["fitted_scale"]["buckets"]) == 3  # 24, 48, 72 h with leads to 72 h

    ens_out = tmp_path / "ens.json"
    assert (
        cli_main(
            [*base, "--ensemble-source", "synthetic_ens", "--boot", "20", "--json", str(ens_out)]
        )
        == 0
    )
    ens = json.loads(ens_out.read_text())
    assert ens["range"] == "ensemble_heuristic"
    assert set(ens["sigma_sources"]["test"]) == {"ensemble_spread"}
    assert cli_main([*base[:6], "--station", "synthetic:nope"]) == 1


def test_verify_confidence_classes_cli(
    db: Session, synthetic_pairs: dict[str, int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    test_start = (START + timedelta(days=45)).isoformat()
    out = tmp_path / "classes.json"
    args = ["verify", "confidence-classes", "--source", "synthetic_gfswave"]
    args += ["--station", "synthetic:station-1", "--station", "synthetic:station-2"]
    args += ["--test-start", test_start, "--boot", "20", "--window-days", "60"]
    assert cli_main([*args, "--json", str(out)]) == 0
    assert "V0_no_verification test" in capsys.readouterr().out
    result = json.loads(out.read_text())
    v0 = result["variants"]["V0_no_verification"]["test"]
    # forecast-v1.1: one model and no ensemble is never High.
    assert v0["counts"]["high"] == 0 and v0["counts"]["moderate"] > 0
    assert "cap:moderate:single_source_no_ensemble" in v0["rules_fired"]
    assert result["verification_rows"] > 0  # 60-day windows with >= 200 pairs exist
    v1 = result["variants"]["V1_causal_verification"]["test"]
    assert "verification_poor_effect" in v1
    ens_out = tmp_path / "ens.json"
    assert cli_main([*args, "--ensemble-source", "synthetic_ens", "--json", str(ens_out)]) == 0
    ens = json.loads(ens_out.read_text())["variants"]["V0_no_verification"]["test"]
    assert ens["counts"]["high"] > 0  # a 10 % spread allows High inside 3 days
