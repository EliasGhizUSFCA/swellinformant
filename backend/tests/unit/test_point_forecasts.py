"""GFS-Wave station bulletin parsing and the going-to -> from direction convention.

Fixtures are REAL NOAA files (public domain, US Government), trimmed to the first 48
hourly rows plus the original legend; no value was changed:

* tests/fixtures/observations/gfswave.62163.20261010t00z.trimmed.bull, from
  https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261010/00/wave/station/bulls.t00z/gfswave.62163.bull
* tests/fixtures/observations/gfswave.62163.20261010t00z.trimmed.cbull, from
  https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261010/00/wave/station/bulls.t00z/gfswave.62163.cbull

Tests marked SYNTHETIC build small text that follows the same layout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.verification.point_forecasts import (
    FAMILY,
    FIRST_CYCLE,
    SOURCE_CODE,
    BulletinFormatError,
    _next_valid_time,
    bulletin_url,
    cycles_between,
    going_to_to_from,
    keep_lead,
    parse_bulletin,
    parse_cycle,
    parse_location,
    point_forecast_rows,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observations"
BULL = (FIXTURES / "gfswave.62163.20261010t00z.trimmed.bull").read_text()
CBULL = (FIXTURES / "gfswave.62163.20261010t00z.trimmed.cbull").read_text()
CYCLE = datetime(2026, 10, 10, 0, tzinfo=UTC)
FT_PER_M = 1 / 0.3048
# .cbull rounds the unrounded model values to whole feet / seconds, while .bull prints them
# to 0.01 m / 0.1 s, so the two can differ by half a unit plus the .bull rounding.
HS_FT_TOL = 0.5 + 0.005 * FT_PER_M + 1e-9
TP_S_TOL = 0.5 + 0.05 + 1e-9


@dataclass(frozen=True)
class CompactRow:
    """A ``.cbull`` row: Hs in whole feet, periods in whole seconds, directions FROM."""

    valid_time: datetime
    hs_ft: int
    partitions: tuple[tuple[int, int, int], ...]  # (Hs ft, Tp s, direction from)


def parse_cbull(text: str) -> tuple[str, datetime, list[CompactRow]]:
    """Parse the compact bulletin (used to cross-check the ``.bull`` conventions)."""
    station_id, _, _ = parse_location(text)
    cycle = parse_cycle(text)
    rows: list[CompactRow] = []
    previous = cycle
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or not re.fullmatch(r"\d{4}", tokens[0]) or (len(tokens) - 2) % 3:
            continue
        valid = _next_valid_time(previous, int(tokens[0][:2]), int(tokens[0][2:]))
        previous = valid
        values = [int(t) for t in tokens[1:]]
        parts = tuple((values[i], values[i + 1], values[i + 2]) for i in range(1, len(values), 3))
        rows.append(CompactRow(valid, values[0], parts))
    return station_id, cycle, rows


def test_header_and_rows() -> None:
    b = parse_bulletin(BULL, expected_cycle=CYCLE)
    assert b.station_id == "62163"
    assert (b.latitude, b.longitude) == (47.5, -8.5)
    assert b.cycle_time == CYCLE
    assert len(b.rows) == 48 and b.malformed == 0
    first = b.rows[0]
    # " | 10  0 | 2.83  1   |   2.83 10.3 121 |" -> going-to 121 -> from 301
    assert first.valid_time == CYCLE and first.lead_hours == 0
    assert first.hs_m == 2.83 and first.n_fields == 1 and first.n_hidden == 0
    assert first.partitions[0].hs_m == 2.83
    assert first.partitions[0].tp_s == 10.3
    assert first.partitions[0].direction_deg == 301.0
    assert not first.partitions[0].wind_sea
    last = b.rows[-1]
    assert last.valid_time == datetime(2026, 10, 11, 23, tzinfo=UTC)
    assert last.lead_hours == 47


def test_wind_sea_flag_and_multiple_partitions() -> None:
    b = parse_bulletin(BULL)
    row = next(r for r in b.rows if r.valid_time == datetime(2026, 10, 11, 11, tzinfo=UTC))
    # " | 11 11 | 2.03  3   |   1.85 10.9 127 | * 0.77  4.0  21 |   0.33 13.9 149 |"
    assert row.hs_m == 2.03 and row.n_fields == 3
    assert [(p.hs_m, p.tp_s, p.direction_deg, p.wind_sea) for p in row.partitions] == [
        (1.85, 10.9, 307.0, False),
        (0.77, 4.0, 201.0, True),
        (0.33, 13.9, 329.0, False),
    ]


def test_bull_directions_are_going_to_cbull_from() -> None:
    """Same station and cycle: every .cbull 'from' direction equals .bull dir + 180 (mod 360),
    and .cbull heights are the .bull heights in whole feet. This is the evidence that
    the .bull directions are 'going-to' (research notes: w3bullmd.F90)."""
    bull = parse_bulletin(BULL)
    station, cycle, compact = parse_cbull(CBULL)
    assert station == "62163" and cycle == CYCLE
    assert len(compact) == len(bull.rows) == 48
    checked = 0
    for b_row, c_row in zip(bull.rows, compact, strict=True):
        assert b_row.valid_time == c_row.valid_time
        assert abs(b_row.hs_m * FT_PER_M - c_row.hs_ft) <= HS_FT_TOL
        assert len(b_row.partitions) == len(c_row.partitions)
        for p, (hs_ft, tp_s, dir_from) in zip(b_row.partitions, c_row.partitions, strict=True):
            # parse_bulletin already converted to FROM; it must equal the .cbull value.
            assert p.direction_deg == float(dir_from)
            assert abs(p.hs_m * FT_PER_M - hs_ft) <= HS_FT_TOL
            assert abs(p.tp_s - tp_s) <= TP_S_TOL
            checked += 1
    assert checked >= 60


def test_rows_for_storage() -> None:
    b = parse_bulletin(BULL)
    station = SimpleNamespace(id=7, latitude=47.5, longitude=-8.47)  # Brittany buoy position
    rows = point_forecast_rows(b, station, max_lead_hours=24)  # type: ignore[arg-type]
    assert len(rows) == 25  # hourly 0..24
    r: dict[str, Any] = rows[0]
    assert r["source_code"] == SOURCE_CODE == "gfswave_bulletin"
    assert r["family"] == FAMILY == "ncep_gfs"
    assert r["hs_m"] == 2.83
    assert r["tp_s"] is None and r["direction_deg"] is None  # no total-sea Tp / direction
    assert r["wind_speed_kmh"] is None  # the bulletin has no wind
    assert (r["swell1_hs_m"], r["swell1_tp_s"], r["swell1_direction_deg"]) == (2.83, 10.3, 301.0)
    assert (r["model_latitude"], r["model_longitude"]) == (47.5, -8.5)
    assert r["distance_km"] == pytest.approx(2.26, abs=0.01)
    assert r["extra"]["partition_period_kind"] == "tp"
    assert r["extra"]["hs_kind"] == "spectral_hm0"
    # Lead 28 h (11 04Z) lists a wind sea as partition 2: kept in extra, flagged.
    later = point_forecast_rows(b, station, max_lead_hours=48)  # type: ignore[arg-type]
    row28 = next(x for x in later if x["lead_hours"] == 28)
    assert row28["extra"]["swell1_is_wind_sea"] is False
    assert row28["extra"]["partitions"][1] == {
        "hs_m": 0.45,
        "tp_s": 3.0,
        "direction_deg": 207.0,
        "wind_sea": True,
    }


def test_wind_sea_first_partition_is_flagged() -> None:
    """SYNTHETIC row in the real layout whose first listed partition is a wind sea."""
    text = BULL.replace(
        " | 10  0 | 2.83  1   |   2.83 10.3 121 |",
        " | 10  0 | 2.83  2   | * 2.50  6.0 121 |",
    )
    b = parse_bulletin(text)
    station = SimpleNamespace(id=1, latitude=47.5, longitude=-8.5)
    rows = point_forecast_rows(b, station, max_lead_hours=0)  # type: ignore[arg-type]
    assert rows[0]["extra"]["swell1_is_wind_sea"] is True
    assert rows[0]["extra"]["partitions"][0]["wind_sea"] is True


def test_leads_are_hourly_then_three_hourly() -> None:
    assert keep_lead(0, 384) and keep_lead(120, 384) and keep_lead(123, 384)
    assert not keep_lead(121, 384) and not keep_lead(122, 384)
    assert not keep_lead(387, 384) and not keep_lead(-1, 384)


def test_month_rollover() -> None:
    """SYNTHETIC: rows only give day and hour; a cycle on the 31st runs into the next month."""
    text = (
        "  Location : 99999      (10.00S  20.00W)\n"
        "  Cycle    : 20261231 18 UTC\n"
        " | 31 18 | 1.00  1   |   1.00 10.0 100 |\n"
        " | 31 23 | 1.10  1   |   1.10 10.0 100 |\n"
        " |  1  0 | 1.20  1   |   1.20 10.0 100 |\n"
    )
    b = parse_bulletin(text)
    assert (b.latitude, b.longitude) == (-10.0, -20.0)
    assert [r.valid_time for r in b.rows] == [
        datetime(2026, 12, 31, 18, tzinfo=UTC),
        datetime(2026, 12, 31, 23, tzinfo=UTC),
        datetime(2027, 1, 1, 0, tzinfo=UTC),
    ]
    assert b.rows[-1].lead_hours == 6


def test_malformed_input() -> None:
    with pytest.raises(BulletinFormatError):
        parse_bulletin("<Error><Code>NoSuchKey</Code></Error>")
    with pytest.raises(BulletinFormatError):
        parse_bulletin(BULL, expected_cycle=datetime(2026, 10, 10, 12, tzinfo=UTC))
    broken = BULL.replace("|   2.83 10.3 121 |", "|   x.xx 10.3 121 |", 1)
    b = parse_bulletin(broken)
    assert b.malformed == 1  # the bad partition is counted, not guessed
    assert b.rows[0].partitions == ()


def test_urls_and_cycles() -> None:
    assert bulletin_url("https://noaa-gfs-bdp-pds.s3.amazonaws.com/", "62163", CYCLE) == (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261010/00/wave/station/bulls.t00z/"
        "gfswave.62163.bull"
    )
    got = cycles_between(
        datetime(2026, 10, 8, 6, tzinfo=UTC), datetime(2026, 10, 10, 0, tzinfo=UTC), (0, 12)
    )
    assert got == [
        datetime(2026, 10, 8, 12, tzinfo=UTC),
        datetime(2026, 10, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 9, 12, tzinfo=UTC),
        datetime(2026, 10, 10, 0, tzinfo=UTC),
    ]
    assert cycles_between(datetime(2021, 3, 22, tzinfo=UTC), FIRST_CYCLE, (0, 12, 18)) == [
        FIRST_CYCLE
    ]
    with pytest.raises(ValueError):
        cycles_between(CYCLE, CYCLE, (3,))


def test_going_to_conversion() -> None:
    assert going_to_to_from(0) == 180
    assert going_to_to_from(270) == 90
    assert going_to_to_from(180) == 0
