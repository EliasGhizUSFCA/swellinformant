"""GEFS-Wave station bulletins (ensemble mean and spread at buoys).

Fixtures are REAL NOAA files (public domain, US Government), unchanged:

* tests/fixtures/observations/gefs.wave.51201.20261011t00z.bull, member
  ``gefs.wave.51201.bull`` of
  https://noaa-gefs-pds.s3.amazonaws.com/gefs.20261011/00/wave/station/gefs.wave.t00z.bull_tar
  (downloaded 2026-10-12);
* tests/fixtures/observations/gefs.wave.51201.20250115t00z.averages-only.bull, the same
  member of
  https://noaa-gefs-pds.s3.amazonaws.com/gefs.20250115/00/wave/station/gefs.wave.t00z.bull_tar
  (downloaded 2026-10-12): the layout of cycles before 2026-02-26, three numbers per row.

Tests marked SYNTHETIC build small text or tar files that follow the same layout.
"""

from __future__ import annotations

import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.verification.gefs_bulletins import (
    FAMILY,
    FIRST_SPREAD_CYCLE,
    SOURCE_CODE,
    GefsBulletinFormatError,
    member_name,
    parse_gefs_bulletin,
    parse_location,
    point_forecast_rows,
    read_members,
    tar_url,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observations"
FULL = (FIXTURES / "gefs.wave.51201.20261011t00z.bull").read_text()
AVERAGES_ONLY = (FIXTURES / "gefs.wave.51201.20250115t00z.averages-only.bull").read_text()
CYCLE = datetime(2026, 10, 11, 0, tzinfo=UTC)


def test_header_rows_and_columns() -> None:
    b = parse_gefs_bulletin(FULL, expected_cycle=CYCLE)
    assert b.station_id == "51201"
    assert (b.latitude, b.longitude) == (21.671, -158.117)
    assert b.cycle_time == CYCLE
    assert len(b.rows) == 105 and b.malformed == 0
    first = b.rows[0]
    # "| 11 00 |  1.95  |  0.03  | 15.40  |  0.03  |  6.73  |  0.49  |  1.00  | ..."
    assert first.valid_time == CYCLE and first.lead_hours == 0
    assert first.values["hs_avg"] == 1.95 and first.values["hs_spr"] == 0.03
    assert first.values["tp_avg"] == 15.4 and first.values["tp_spr"] == 0.03
    assert first.values["u10_avg"] == 6.73 and first.values["u10_spr"] == 0.49
    assert first.values["p_hs_gt_1m"] == 1.0 and first.values["p_hs_gt_3m"] == 0.38
    leads = [r.lead_hours for r in b.rows]
    # 3-hourly to 240 h, then 6-hourly to 384 h (16 days).
    assert leads[:3] == [0.0, 3.0, 6.0]
    assert leads[80] == 240.0 and leads[81] == 246.0 and leads[-1] == 384.0
    assert all(b2 > a for a, b2 in zip(leads, leads[1:], strict=False))


def test_signed_longitude_letter_ignored() -> None:
    # 52200 (Guam, 144.8 E) is printed "144.788W"; 51201 (158.1 W) "-158.117W".
    assert parse_location(" Location : 52200      (13.354N  144.788W)") == (
        "52200",
        13.354,
        144.788,
    )
    assert parse_location(" Location : 62001      (45.230N  -5.000W)")[2] == -5.0


def test_averages_only_layout_is_refused() -> None:
    with pytest.raises(GefsBulletinFormatError, match="averages-only"):
        parse_gefs_bulletin(AVERAGES_ONLY)


def test_wrong_cycle_and_wrong_product_refused() -> None:
    with pytest.raises(GefsBulletinFormatError, match="not 20261012"):
        parse_gefs_bulletin(FULL, expected_cycle=datetime(2026, 10, 12, tzinfo=UTC))
    gfs = (FIXTURES / "gfswave.62163.20261010t00z.trimmed.bull").read_text()
    with pytest.raises(GefsBulletinFormatError, match="Global Wave Ensemble"):
        parse_gefs_bulletin(gfs)


def test_point_forecast_rows_store_mean_and_spread() -> None:
    b = parse_gefs_bulletin(FULL)
    station = SimpleNamespace(id=7, latitude=21.671, longitude=-158.118)
    rows = point_forecast_rows(b, station, max_lead_hours=240.0)  # type: ignore[arg-type]
    assert len(rows) == 81  # 0-240 h every 3 h
    r = rows[0]
    assert r["source_code"] == SOURCE_CODE and r["family"] == FAMILY == "ncep_gefs"
    assert r["hs_m"] == 1.95 and r["hs_spread_m"] == 0.03 and r["tp_s"] == 15.4
    assert r["wind_speed_kmh"] == pytest.approx(6.73 * 3.6, abs=0.01)
    assert r["direction_deg"] is None and r["swell1_hs_m"] is None
    assert r["extra"]["tp_spread_s"] == 0.03 and r["extra"]["p_hs_gt"]["3.0"] == 0.38
    assert r["distance_km"] < 0.2


def test_urls_and_member_names() -> None:
    assert tar_url("https://noaa-gefs-pds.s3.amazonaws.com", CYCLE) == (
        "https://noaa-gefs-pds.s3.amazonaws.com/gefs.20261011/00/wave/station/"
        "gefs.wave.t00z.bull_tar"
    )
    assert member_name("51201") == "gefs.wave.51201.bull"
    with pytest.raises(ValueError):
        member_name("../etc")
    assert datetime(2026, 2, 26, tzinfo=UTC) == FIRST_SPREAD_CYCLE


def _tar(members: dict[str, bytes], extra: list[tarfile.TarInfo] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


def test_read_members_only_exact_regular_files() -> None:
    """SYNTHETIC tar: other members, links and paths are never read."""
    link = tarfile.TarInfo("gefs.wave.46054.bull")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    data = _tar(
        {"gefs.wave.51201.bull": FULL.encode(), "../gefs.wave.51205.bull": b"x"},
        extra=[link],
    )
    out: dict[str, Any] = read_members(
        data, ["gefs.wave.51201.bull", "gefs.wave.46054.bull", "gefs.wave.51205.bull"]
    )
    assert list(out) == ["gefs.wave.51201.bull"]
    assert out["gefs.wave.51201.bull"].startswith("\n Location : 51201")
