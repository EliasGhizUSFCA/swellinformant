"""NDBC adapter against REAL NDBC files (verified live on 2026-10-12).

Fixtures in tests/fixtures/observations/ are excerpts of files downloaded from
www.ndbc.noaa.gov on 2026-10-12 (US Government work, public domain; NOAA National Data
Buoy Center; 51201 is operated by PacIOOS with CDIP, 46224 by Scripps/CDIP). Rows were
only cut, never edited; the .gz excerpts were re-compressed after cutting.

* ndbc_46026_20260829.realtime2.txt / .spec: the 2026-08-29 00:00-01:59 rows of
  https://www.ndbc.noaa.gov/data/realtime2/46026.txt and .../46026.spec
* ndbc_46026_20260829.stdmet_aug2026.txt.gz: the same hours of
  https://www.ndbc.noaa.gov/data/stdmet/Aug/4602682026.txt.gz (monthly QC'd file)
* ndbc_51201_20260829.realtime2.txt / .spec: the 2026-08-29 00:00-02:59 rows of
  https://www.ndbc.noaa.gov/data/realtime2/51201.txt and .../51201.spec
* ndbc_51201_20260829.stdmet_aug2026.txt.gz: the same hours of
  https://www.ndbc.noaa.gov/data/stdmet/Aug/5120182026.txt.gz
* ndbc_51201h2025.head.txt.gz: the first 10 lines of
  https://www.ndbc.noaa.gov/data/historical/stdmet/51201h2025.txt.gz
* ndbc_51201_sep2026.latest_month.txt: header, first row and last two rows of
  https://www.ndbc.noaa.gov/data/stdmet/Sep/51201.txt (the newest month is served
  uncompressed under the bare station id)
* ndbc_activestations_20261012.trimmed.xml: the root element and four station elements of
  https://www.ndbc.noaa.gov/activestations.xml

Tests marked SYNTHETIC build small inputs in the same layouts.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.services.observations import ndbc
from app.services.observations.records import (
    ObservationRecord,
    ParseStats,
    StationRef,
    resolve_batch,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observations"
WIDE = (datetime(1980, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))


def _text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _gz_text(name: str) -> str:
    return gzip.decompress((FIXTURES / name).read_bytes()).decode()


def _t(day: int, hour: int, minute: int, month: int = 8) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def _realtime(station: str, apd_kind: str = "apd") -> list[ObservationRecord]:
    stats = ParseStats()
    records = ndbc.parse_stdmet(
        _text(f"ndbc_{station}_20260829.realtime2.txt"),
        dataset=ndbc.REALTIME_DATASET,
        start=WIDE[0],
        end=WIDE[1],
        stats=stats,
        apd_kind=apd_kind,
    )
    spec = ndbc.parse_spec(
        _text(f"ndbc_{station}_20260829.realtime2.spec"), start=WIDE[0], end=WIDE[1]
    )
    return ndbc.merge_spec(records, spec, stats, apd_kind=apd_kind)


def _archive(station: str, apd_kind: str = "apd") -> list[ObservationRecord]:
    return ndbc.parse_stdmet(
        _gz_text(f"ndbc_{station}_20260829.stdmet_aug2026.txt.gz"),
        dataset=ndbc.MONTHLY_DATASET,
        start=WIDE[0],
        end=WIDE[1],
        stats=ParseStats(),
        apd_kind=apd_kind,
    )


class TestRealtimeCopies:
    def test_ndbc_buoy_repeats_each_wave_report_on_two_met_rows(self) -> None:
        """46026 (NDBC 3-m discus, SCOOP payload): realtime2 .txt prints each 20-minute
        wave report on the 10-minute met rows next to the wave time (here 00:10 and
        00:20, 00:40 and 00:50, ...), with DPD on only one of them; .spec and the QC'd
        monthly file give the wave time once (:10 / :40). One record per wave report,
        at the .spec time, with DPD taken from whichever copy has it."""
        records = _realtime("46026", apd_kind="tm02")
        times = [r.observed_at for r in records]
        assert times == [_t(29, 0, 10), _t(29, 0, 40), _t(29, 1, 10), _t(29, 1, 40)]
        assert times == [r.observed_at for r in _archive("46026")]
        by_time = {r.observed_at: r for r in records}
        r0010 = by_time[_t(29, 0, 10)]
        assert (r0010.hs_m, r0010.tp_s, r0010.tm_s, r0010.peak_direction_deg) == (
            1.4,
            10.0,  # from the 00:20 copy; the 00:10 row has DPD = MM
            5.1,
            238.0,
        )
        assert r0010.tm_kind == "tm02"
        assert (r0010.swell_hs_m, r0010.swell_tp_s, r0010.windsea_hs_m) == (0.9, 10.0, 1.1)
        assert r0010.swell_direction_deg == 247.5  # WSW, FROM
        # 01:10: the .txt has no wave values on the 01:10 row, only the 01:20 copy.
        assert (by_time[_t(29, 1, 10)].hs_m, by_time[_t(29, 1, 10)].tp_s) == (1.5, 7.0)
        assert all(r.qc_flag == "not_qc" for r in records)

    def test_copies_can_precede_the_spec_time(self) -> None:
        """51101 (NDBC deep-ocean buoy): the copies sit 10 min before and 10 min after
        the .spec time (00:30 and 00:50 for the 00:40 report). Neighbouring reports
        differ in MWD (327 vs 335 degrees), so each copy goes to its own report."""
        stats = ParseStats()
        records = ndbc.parse_stdmet(
            _text("ndbc_51101_20261009.realtime2.txt"),
            dataset=ndbc.REALTIME_DATASET,
            start=WIDE[0],
            end=WIDE[1],
            stats=stats,
            apd_kind="tm02",
        )
        spec = ndbc.parse_spec(
            _text("ndbc_51101_20261009.realtime2.spec"), start=WIDE[0], end=WIDE[1]
        )
        merged = ndbc.merge_spec(records, spec, stats, apd_kind="tm02")
        assert [(r.observed_at.hour, r.observed_at.minute) for r in merged] == [
            (0, 10),
            (0, 40),
            (1, 10),
            (1, 40),
        ]
        assert [(r.hs_m, r.tp_s, r.peak_direction_deg) for r in merged] == [
            (2.8, 11.0, 327.0),  # copy at 00:20
            (2.8, 10.0, 335.0),  # copies at 00:30 (no DPD) and 00:50
            (2.6, 11.0, 325.0),  # copies at 01:00 and 01:20
            (2.7, 11.0, 328.0),  # copies at 01:40 and 01:50
        ]
        assert stats.rows_kept == 4 and stats.duplicates_merged == 3

    def test_cdip_buoy_has_one_row_per_report(self) -> None:
        """51201 (CDIP Waverider): .txt and .spec carry the same :26 / :56 times as the
        monthly file; nothing is merged or moved."""
        records = _realtime("51201", apd_kind="tm01")
        assert [r.observed_at for r in records] == [r.observed_at for r in _archive("51201")]
        assert len(records) == 6
        assert all(r.tm_kind == "tm01" for r in records)

    @pytest.mark.parametrize("station", ["46026", "51201"])
    def test_realtime_agrees_with_the_qcd_monthly_file(self, station: str) -> None:
        """Realtime heights are rounded to 0.1 m and DPD to whole seconds; the monthly
        file has 0.01 m / 0.01 s. Directions are the same FROM degrees."""
        archive = {r.observed_at: r for r in _archive(station)}
        for r in _realtime(station):
            a = archive[r.observed_at]
            assert r.hs_m == pytest.approx(a.hs_m, abs=0.05 + 1e-9)
            assert r.tm_s == pytest.approx(a.tm_s, abs=0.05 + 1e-9)
            assert r.peak_direction_deg == a.peak_direction_deg
            if r.tp_s is not None and a.tp_s is not None:
                assert r.tp_s == pytest.approx(a.tp_s, abs=0.5 + 1e-9)

    def test_archive_supersedes_realtime_at_the_same_time(self) -> None:
        kept, dropped = resolve_batch(_realtime("46026") + _archive("46026"))
        assert dropped == 4
        assert {r.dataset for r in kept} == {ndbc.MONTHLY_DATASET}
        assert all(r.qc_flag == "probably_good" for r in kept)

    def test_txt_copies_without_spec_are_collapsed(self) -> None:
        """Without the .spec file the copies are still one record each (earliest time)."""
        stats = ParseStats()
        records = ndbc.parse_stdmet(
            _text("ndbc_46026_20260829.realtime2.txt"),
            dataset=ndbc.REALTIME_DATASET,
            start=WIDE[0],
            end=WIDE[1],
            stats=stats,
        )
        merged = ndbc.merge_spec(records, {}, stats)
        assert [r.observed_at for r in merged] == [
            _t(29, 0, 10),
            _t(29, 0, 40),
            _t(29, 1, 20),  # the 01:10 row has no wave values
            _t(29, 1, 40),
        ]
        assert merged[0].tp_s == 10.0
        assert stats.duplicates_merged == 3  # 7 wave rows, 4 reports
        assert stats.rows_kept == 4


class TestArchiveFiles:
    def test_yearly_file_sentinels(self) -> None:
        records = ndbc.parse_stdmet(
            _gz_text("ndbc_51201h2025.head.txt.gz"),
            dataset=ndbc.HISTORICAL_DATASET,
            start=WIDE[0],
            end=WIDE[1],
            stats=ParseStats(),
            apd_kind="tm01",
        )
        r = records[0]
        assert r.observed_at == datetime(2025, 1, 1, 0, 16, tzinfo=UTC)
        assert (r.hs_m, r.hs_kind, r.tp_s, r.tm_s, r.tm_kind) == (
            1.60,
            "spectral_hm0",
            14.29,
            7.00,
            "tm01",
        )
        assert r.peak_direction_deg == 294.0
        # WDIR 999 and WSPD 99.0 are missing values, not a 999-degree wind.
        assert r.wind_direction_deg is None and r.wind_speed_kmh is None
        assert r.qc_flag == "probably_good"

    def test_latest_month_plain_file(self) -> None:
        records = ndbc.parse_stdmet(
            _text("ndbc_51201_sep2026.latest_month.txt"),
            dataset=ndbc.MONTHLY_DATASET,
            start=WIDE[0],
            end=WIDE[1],
            stats=ParseStats(),
        )
        assert [r.observed_at for r in records] == [
            _t(1, 0, 26, month=9),
            _t(30, 23, 26, month=9),
            _t(30, 23, 56, month=9),
        ]
        assert records[0].qc_flag == "probably_good"

    def test_active_stations_real_xml(self) -> None:
        got = ndbc.parse_active_stations(_text("ndbc_activestations_20261012.trimmed.xml"))
        assert set(got) == {"46012", "46026", "46224", "51201"}
        assert got["51201"]["latitude"] == 21.671 and got["51201"]["longitude"] == -158.118
        assert got["46224"]["owner"] == "SCRIPPS"
        assert got["46012"]["type"] == "usv"  # an uncrewed surface vehicle, not a buoy


class TestSpecMissingCodes:
    def test_minus_99_and_no_swell(self) -> None:
        """SYNTHETIC rows: NDBC's wave glossary says 'MM or -99 denote missing data' and
        'NO indicates that no swells could be determined' for the .spec columns."""
        text = (
            "#YY  MM DD hh mm WVHT  SwH  SwP  WWH  WWP SwD WWD  STEEPNESS  APD MWD\n"
            "#yr  mo dy hr mn    m    m  sec    m  sec  -  degT     -      sec degT\n"
            "2026 10 10 21 00  1.8  -99  -99  0.6  5.6  NO ENE    AVERAGE  7.6 315\n"
        )
        spec = ndbc.parse_spec(text, start=WIDE[0], end=WIDE[1])
        values = spec[datetime(2026, 10, 10, 21, 0, tzinfo=UTC)]
        assert values["swell_hs_m"] is None and values["swell_tp_s"] is None
        assert values["swell_direction_deg"] is None
        assert values["windsea_hs_m"] == 0.6


class TestProviderFiles:
    NOW = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)

    def _provider(self, served: dict[str, bytes], calls: list[str]) -> ndbc.NdbcProvider:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            body = served.get(request.url.path)
            return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

        return ndbc.NdbcProvider(
            Settings(ndbc_base_url="https://ndbc.example"),
            httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
            now=self.NOW,
        )

    def test_direct_gz_monthly_and_latest_month_files(self) -> None:
        served = {
            "/activestations.xml": (
                FIXTURES / "ndbc_activestations_20261012.trimmed.xml"
            ).read_bytes(),
            "/data/realtime2/51201.txt": (
                FIXTURES / "ndbc_51201_20260829.realtime2.txt"
            ).read_bytes(),
            "/data/realtime2/51201.spec": (
                FIXTURES / "ndbc_51201_20260829.realtime2.spec"
            ).read_bytes(),
            "/data/historical/stdmet/51201h2025.txt.gz": (
                FIXTURES / "ndbc_51201h2025.head.txt.gz"
            ).read_bytes(),
            "/data/stdmet/Aug/5120182026.txt.gz": (
                FIXTURES / "ndbc_51201_20260829.stdmet_aug2026.txt.gz"
            ).read_bytes(),
            # The newest month is not compressed yet: Sep/5120192026.txt.gz is a 404.
            "/data/stdmet/Sep/51201.txt": (
                FIXTURES / "ndbc_51201_sep2026.latest_month.txt"
            ).read_bytes(),
        }
        calls: list[str] = []
        p = self._provider(served, calls)
        result = p.fetch(
            [StationRef("ndbc:51201", "51201", 21.67, -158.12, {"apd_kind": "tm01"})],
            datetime(2025, 1, 1, tzinfo=UTC),
            self.NOW,
        )
        out = result.stations["ndbc:51201"]
        assert "/view_text_file.php" not in calls
        assert "/data/historical/stdmet/51201h2025.txt.gz" in calls
        month_calls = [c for c in calls if c.startswith("/data/stdmet/")]
        for i, mon in enumerate(
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"), start=1
        ):
            assert f"/data/stdmet/{mon}/51201{i}2026.txt.gz" in month_calls
        assert "/data/stdmet/Sep/51201.txt" in month_calls
        assert "/data/stdmet/Oct/51201a2026.txt.gz" not in month_calls  # realtime covers it
        datasets = {r.dataset for r in out.records}
        assert datasets == {
            ndbc.REALTIME_DATASET,
            ndbc.HISTORICAL_DATASET,
            ndbc.MONTHLY_DATASET,
        }
        times = {r.observed_at for r in out.records}
        assert datetime(2025, 1, 1, 0, 16, tzinfo=UTC) in times  # yearly file
        assert _t(29, 0, 26) in times  # Aug monthly (and realtime)
        assert _t(1, 0, 26, month=9) in times  # latest-month plain file
        assert out.errors == []
        assert out.files == 2 + 1 + 2  # realtime x2, yearly, Aug and Sep
        assert all(r.tm_kind in (None, "tm01") for r in out.records)
        # Months that are missing under both names are reported, not silently skipped.
        assert any("Jan 2026" in w and "not found" in w for w in result.warnings)
        assert result.bytes_downloaded > 0

    def test_previous_year_falls_back_to_monthly_files(self) -> None:
        """Early in a year the yearly file of the previous year is not published yet
        (404); its months are then read from the monthly directories (SYNTHETIC rows)."""
        dec = (
            "#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP"
            "  VIS  TIDE\n"
            "#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC"
            "   mi    ft\n"
            "2025 12 20 00 26 999 99.0 99.0  2.10 14.29  7.00 320 9999.0 999.0  25.0 999.0"
            " 99.0 99.00\n"
        )
        served = {"/data/stdmet/Dec/51201c2025.txt.gz": gzip.compress(dec.encode())}
        calls: list[str] = []
        p = self._provider(served, calls)
        p.now = datetime(2026, 1, 20, tzinfo=UTC)
        result = p.fetch(
            [StationRef("ndbc:51201", "51201", 21.67, -158.12)],
            datetime(2025, 11, 15, tzinfo=UTC),
            datetime(2025, 12, 31, tzinfo=UTC),
        )
        out = result.stations["ndbc:51201"]
        assert "/data/historical/stdmet/51201h2025.txt.gz" in calls
        assert "/data/stdmet/Nov/51201b2025.txt.gz" in calls
        assert [r.observed_at for r in out.records] == [datetime(2025, 12, 20, 0, 26, tzinfo=UTC)]
        assert out.records[0].dataset == ndbc.MONTHLY_DATASET

    def test_html_answer_for_a_gz_file_is_an_error_not_data(self) -> None:
        served = {"/data/historical/stdmet/51201h2025.txt.gz": b"<html>Unable to access</html>"}
        calls: list[str] = []
        p = self._provider(served, calls)
        p.now = datetime(2026, 10, 12, tzinfo=UTC)
        result = p.fetch(
            [StationRef("ndbc:51201", "51201", 21.67, -158.12)],
            datetime(2025, 3, 1, tzinfo=UTC),
            datetime(2025, 3, 2, tzinfo=UTC),
        )
        out = result.stations["ndbc:51201"]
        assert out.records == []
        assert any("51201h2025.txt.gz" in e and "gzip" in e for e in out.errors)


def test_station_window_skips_realtime_for_old_periods() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404)

    p = ndbc.NdbcProvider(
        Settings(ndbc_base_url="https://ndbc.example"),
        httpx.Client(transport=httpx.MockTransport(handler)),
        backoff_seconds=0,
        now=datetime(2026, 10, 12, tzinfo=UTC),
    )
    p.fetch(
        [StationRef("ndbc:46026", "46026", 37.75, -122.84)],
        datetime(2024, 10, 12, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC) + timedelta(hours=23),
    )
    assert "/data/realtime2/46026.txt" not in calls
    assert "/data/historical/stdmet/46026h2024.txt.gz" in calls
