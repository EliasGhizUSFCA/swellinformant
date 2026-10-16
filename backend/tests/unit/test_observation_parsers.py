"""Buoy observation parsers: AODN Parquet, Met Office CSV, NDBC text, records and S3 listing.

Provenance of the test inputs:

* AODN: tiny Parquet tables built here with pyarrow. Column names, types and attributes
  (flag_values / flag_meanings, standard_name, reference_datum) are copied from the real
  ``_common_metadata`` of s3://aodn-cloud-optimised/wave_buoy_{realtime_nonqc,delayed_qc}
  .parquet/ (read 2026-10-11); the VALUES are SYNTHETIC.
* Met Office: tests/fixtures/observations/metoffice_brittany_buoy_2026101103.csv is a REAL
  file, unmodified, from
  https://met-office-marine-observations-data.s3.eu-west-2.amazonaws.com/202610110415_202610110300_202610110359/brittany_buoy_820f4d91-a125-4a24-8baf-bc0ba7760887.csv
  Source: Met Office marine observations, licensed under CC BY-SA 4.0
  (https://creativecommons.org/licenses/by-sa/4.0/); the fixture is redistributed under
  the same licence. Variants derived from it in a test are labelled SYNTHETIC.
* NDBC: SYNTHETIC text in NDBC's realtime2 / historical layouts (the real-file tests are in
  tests/unit/test_ndbc_real_files.py).
"""

from __future__ import annotations

import gzip
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.config import Settings
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.observations import aodn, metoffice, ndbc
from app.services.observations.records import (
    ObservationRecord,
    ParseStats,
    StationRef,
    resolve_batch,
    round_to_minute,
    validate_record,
)
from app.services.observations.s3 import list_objects, object_url

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observations"
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_active_stations_cache() -> None:
    ndbc.clear_active_stations_cache()


WIDE = (datetime(1980, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))

# ----------------------------------------------------------------------------------- AODN

# Attributes as declared in the real datasets' _common_metadata.
QC_META = {
    "long_name": "primary Quality Control flag for wave variables",
    "flag_values": "[1, 2, 3, 4, 9]",
    "flag_meanings": "good not_evaluated questionable bad missing",
}
POS_META = {
    "long_name": "primary Quality Control flag for positioning",
    "flag_values": "[1, 2, 3, 4, 9]",
    "flag_meanings": "good not_evaluated questionable bad missing",
}
WPDI_META = {
    "units": "degree",
    "standard_name": "sea_surface_wave_from_direction_at_variance_spectral_density_maximum",
    "reference_datum": "true north",
}
SSWMD_META = {
    "units": "degree",
    "standard_name": "sea_surface_wave_from_direction",
    "reference_datum": "true north",
}


def aodn_parquet(rows: list[dict[str, Any]], *, wpdi_meta: dict[str, str] | None = None) -> bytes:
    """A Parquet file with the real AODN column names/attributes and SYNTHETIC values."""

    def col(name: str) -> list[Any]:
        return [r.get(name) for r in rows]

    fields = [
        pa.field("TIME", pa.timestamp("ns")),
        pa.field("WSSH", pa.float64(), metadata={"units": "m"}),
        pa.field("WHTH", pa.float64(), metadata={"units": "m"}),
        pa.field("WPPE", pa.float64(), metadata={"units": "s"}),
        pa.field("WPFM", pa.float64(), metadata={"units": "s"}),
        pa.field("WPMH", pa.float64(), metadata={"units": "s"}),
        pa.field("WPDI", pa.float64(), metadata=wpdi_meta or WPDI_META),
        pa.field("SSWMD", pa.float64(), metadata=SSWMD_META),
        pa.field("WAVE_quality_control", pa.float32(), metadata=QC_META),
        pa.field("WATCH_CIRCLE_flag", pa.float32(), metadata=POS_META),
        pa.field("LATITUDE", pa.float64()),
        pa.field("LONGITUDE", pa.float64()),
        pa.field("water_depth", pa.int64()),
        pa.field("wmo_id", pa.string()),
        pa.field("filename", pa.string()),
    ]
    data = {
        "TIME": [r["TIME"].replace(tzinfo=None) for r in rows],
        **{f.name: col(f.name) for f in fields[1:]},
    }
    table = pa.Table.from_pydict(data, schema=pa.schema(fields))
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def _schema_of(content: bytes) -> pa.Schema:
    return pq.read_schema(io.BytesIO(content))


def _row(minutes: int, **values: Any) -> dict[str, Any]:
    base = {
        "TIME": T0 + timedelta(minutes=minutes),
        "LATITUDE": -33.53,
        "LONGITUDE": 114.77,
        "water_depth": 50,
        "wmo_id": "56006",
        "filename": "SYNTHETIC.nc",
        "WAVE_quality_control": 1.0,
        "WATCH_CIRCLE_flag": 1.0,
    }
    return {**base, **values}


class TestAodn:
    def test_conventions_come_from_the_data(self) -> None:
        content = aodn_parquet([_row(0, WSSH=1.0)])
        conv = aodn.conventions_from_schema(_schema_of(content))
        # In these datasets 2 means "not evaluated", not "probably good".
        assert conv.wave_flags == {
            1: "good",
            2: "not_evaluated",
            3: "questionable",
            4: "bad",
            9: "missing",
        }
        assert conv.direction == {"WPDI": "from", "SSWMD": "from"}

    def test_delayed_mode_qc_mapping(self) -> None:
        rows = [
            _row(0, WSSH=2.0, WPPE=12.0, WPDI=230.0, SSWMD=225.0, WPFM=8.0),
            _row(30, WSSH=2.1, WAVE_quality_control=2.0),
            _row(60, WSSH=9.0, WAVE_quality_control=3.0),
            _row(90, WSSH=2.2, WAVE_quality_control=4.0),
            _row(120, WSSH=None, WAVE_quality_control=9.0),
            _row(150, WSSH=2.3, WATCH_CIRCLE_flag=3.0),
            _row(180, WSSH=2.4, WATCH_CIRCLE_flag=4.0),
        ]
        content = aodn_parquet(rows)
        stats = ParseStats()
        records, position = aodn.parse_parquet(
            content,
            dataset=aodn.DELAYED,
            start=WIDE[0],
            end=WIDE[1],
            conventions=aodn.conventions_from_schema(_schema_of(content)),
            stats=stats,
        )
        by_minute = {int((r.observed_at - T0).total_seconds() // 60): r for r in records}
        assert sorted(by_minute) == [0, 30, 60, 90, 150, 180]  # 'missing' row skipped
        good = by_minute[0]
        assert (good.qc_flag, good.hs_m, good.hs_kind) == ("good", 2.0, "spectral_hm0")
        assert (good.tp_s, good.peak_direction_deg, good.mean_direction_deg) == (12.0, 230.0, 225.0)
        assert (good.tm_s, good.tm_kind) == (8.0, "tm01")
        assert by_minute[30].qc_flag == "not_qc"  # not evaluated
        for m in (60, 90, 180):  # questionable, bad, off-station: rejection markers
            assert by_minute[m].qc_flag == "bad"
            assert by_minute[m].hs_m is None and by_minute[m].hs_kind == "unknown"
        assert by_minute[150].qc_flag == "probably_good"  # good values, doubtful position
        assert stats.qc_rejected == 3 and stats.no_wave_data == 1
        assert position is not None and position["water_depth_m"] == 50

    def test_height_definitions_and_fill_values(self) -> None:
        rows = [
            _row(0, WSSH=None, WHTH=1.5, WPPE=0.0, WPDI=240.0, WPMH=7.0),
            _row(30, WSSH=35.0, WHTH=1.6),
            _row(60, WSSH=None, WHTH=None, WPPE=None),
        ]
        content = aodn_parquet(rows)
        stats = ParseStats()
        records, _ = aodn.parse_parquet(
            content,
            dataset=aodn.REALTIME,
            start=WIDE[0],
            end=WIDE[1],
            conventions=aodn.conventions_from_schema(_schema_of(content)),
            stats=stats,
        )
        assert len(records) == 2
        r0, r1 = records
        assert (r0.hs_m, r0.hs_kind, r0.qc_flag) == (1.5, "h_third", "not_qc")
        # WPPE == 0 is a fill value; the peak direction of that row is undefined too.
        assert r0.tp_s is None and r0.peak_direction_deg is None
        assert (r0.tm_s, r0.tm_kind) == (7.0, "tz")
        assert (r1.hs_m, r1.hs_kind) == (1.6, "h_third")  # WSSH 35 m is impossible
        assert stats.invalid_values["WSSH"] == 1
        assert stats.invalid_values["WPPE"] == 1 and stats.invalid_values["WPDI"] == 1
        assert stats.no_wave_data == 1

    def test_realtime_rows_flagged_bad_are_dropped(self) -> None:
        rows = [_row(0, WSSH=1.0), _row(30, WSSH=1.1, WAVE_quality_control=4.0)]
        content = aodn_parquet(rows)
        stats = ParseStats()
        records, _ = aodn.parse_parquet(
            content,
            dataset=aodn.REALTIME,
            start=WIDE[0],
            end=WIDE[1],
            conventions=aodn.conventions_from_schema(_schema_of(content)),
            stats=stats,
        )
        assert [r.qc_flag for r in records] == ["not_qc"]  # realtime is never "good"
        assert stats.qc_rejected == 1

    def test_going_to_direction_is_converted(self) -> None:
        meta = {**WPDI_META, "standard_name": "sea_surface_wave_to_direction"}
        content = aodn_parquet([_row(0, WSSH=1.0, WPPE=10.0, WPDI=45.0)], wpdi_meta=meta)
        conv = aodn.conventions_from_schema(_schema_of(content))
        records, _ = aodn.parse_parquet(
            content,
            dataset=aodn.DELAYED,
            start=WIDE[0],
            end=WIDE[1],
            conventions=conv,
            stats=ParseStats(),
        )
        assert conv.direction["WPDI"] == "to"
        assert records[0].peak_direction_deg == 225.0

    def test_time_window(self) -> None:
        content = aodn_parquet([_row(0, WSSH=1.0), _row(600, WSSH=1.2)])
        records, _ = aodn.parse_parquet(
            content,
            dataset=aodn.DELAYED,
            start=T0 + timedelta(hours=1),
            end=T0 + timedelta(hours=11),
            conventions=aodn.Conventions(),
            stats=ParseStats(),
        )
        assert [r.hs_m for r in records] == [1.2]

    def test_unreadable_file(self) -> None:
        with pytest.raises(aodn.PermanentProviderError):
            aodn.parse_parquet(
                b"not parquet",
                dataset=aodn.DELAYED,
                start=WIDE[0],
                end=WIDE[1],
                conventions=aodn.Conventions(),
                stats=ParseStats(),
            )

    def test_keys_are_double_encoded(self) -> None:
        prefix = aodn.partition_prefix(aodn.REALTIME, "Cape Naturaliste")
        assert prefix == "wave_buoy_realtime_nonqc.parquet/site_name=Cape%20Naturaliste/"
        key = prefix + "timestamp=1790812800/polygon=0103/x.nc-0.parquet"
        assert object_url("https://b.example/", key) == (
            "https://b.example/wave_buoy_realtime_nonqc.parquet/site_name=Cape%2520Naturaliste/"
            "timestamp=1790812800/polygon=0103/x.nc-0.parquet"
        )

    def test_partition_selection(self) -> None:
        keys = [
            f"d.parquet/site_name=X/timestamp={int(datetime(2026, m, 1, tzinfo=UTC).timestamp())}"
            f"/polygon=01/f{m}.parquet"
            for m in (7, 8, 9, 10)
        ] + ["d.parquet/site_name=X/_SUCCESS"]
        got = aodn.select_partitions(
            keys, datetime(2026, 9, 20, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC)
        )
        # September (holds 20-30 Sep) and October; August only if within the 32-day margin.
        assert [k.rsplit("/", 1)[-1] for k in got] == ["f9.parquet", "f10.parquet"]

    def test_provider_fetch_with_listing_and_download(self) -> None:
        """SYNTHETIC end-to-end through the adapter with a mocked S3 endpoint."""
        content = aodn_parquet([_row(0, WSSH=1.0, WPPE=11.0), _row(30, WSSH=1.1, WPPE=11.5)])
        month = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
        key = (
            f"wave_buoy_realtime_nonqc.parquet/site_name=Cape%20Naturaliste/timestamp={month}"
            "/polygon=01/SYNTHETIC.nc-0.parquet"
        )
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.params.get("list-type") == "2":
                prefix = request.url.params["prefix"]
                if prefix.startswith("wave_buoy_realtime_nonqc.parquet/site_name=Cape%20Nat"):
                    xml = (
                        "<ListBucketResult><IsTruncated>false</IsTruncated>"
                        f"<Contents><Key>{key}</Key><Size>{len(content)}</Size>"
                        "<LastModified>2026-10-10T00:00:00Z</LastModified></Contents>"
                        "</ListBucketResult>"
                    )
                    return httpx.Response(200, text=xml)
                return httpx.Response(
                    200,
                    text="<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>",
                )
            if request.url.raw_path.endswith(b"_common_metadata"):
                return httpx.Response(200, content=content)
            if b"Cape%2520Naturaliste" in request.url.raw_path:
                return httpx.Response(200, content=content)
            return httpx.Response(404)

        provider = aodn.AodnProvider(
            Settings(aodn_base_url="https://aodn.example"),
            httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
        )
        station = StationRef(
            "aodn:cape-naturaliste",
            "Cape Naturaliste",
            -33.53,
            114.77,
            {"aodn": {"realtime_sites": ["Cape Naturaliste"], "delayed_sites": []}},
        )
        result = provider.fetch([station], T0, T0 + timedelta(days=1))
        out = result.stations["aodn:cape-naturaliste"]
        assert out.files == 1 and not out.errors
        assert [r.hs_m for r in out.records] == [1.0, 1.1]
        assert out.last_position is not None and out.last_position["wmo_id"] == "56006"


# ----------------------------------------------------------------------------- Met Office

MO_CSV = (FIXTURES / "metoffice_brittany_buoy_2026101103.csv").read_text()


class TestMetOffice:
    def test_real_file(self) -> None:
        stats = ParseStats()
        records, position = metoffice.parse_csv(
            MO_CSV, start=WIDE[0], end=WIDE[1], stats=stats, source="real"
        )
        assert len(records) == 1
        r = records[0]
        assert r.observed_at == datetime(2026, 10, 11, 3, 0, tzinfo=UTC)
        assert (r.hs_m, r.hs_kind) == (2.2, "spectral_hm0")  # hm_0, not h_third (2.7)
        assert (r.tp_s, r.tm_s, r.tm_kind) == (10.2, 6.9, "tm02")
        assert (r.peak_direction_deg, r.mean_direction_deg) == (322.0, 319.0)
        assert (r.swell_hs_m, r.swell_tp_s, r.swell_direction_deg) == (1.1, 10.2, 322.0)
        assert r.windsea_hs_m == 1.9
        assert r.wind_speed_kmh == pytest.approx(7.56 * 3.6)
        assert r.wind_direction_deg == 198.1
        assert r.qc_flag == "probably_good"  # automated checks only
        assert position == {
            "latitude": 47.5,
            "longitude": -8.47,
            "observed_at": "2026-10-11T03:00:00+00:00",
            "dataset": metoffice.DATASET,
        }
        assert stats.rows_read == 1 and stats.rows_kept == 1

    def test_flagged_values_are_dropped(self) -> None:
        """SYNTHETIC variant of the real file: the Hm0 and wind QC verdicts set to suspect."""
        header, row = MO_CSV.strip().split("\n")
        names = header.split("|")
        values = row.split("|")
        suspect = '{"suspect": ["1 case(s) of synthetic test flag"]}'
        values[names.index("significant_wave_height_hm_0_qc")] = suspect
        values[names.index("wind_speed_marine_qc")] = suspect
        text = header + "\n" + "|".join(values) + "\n"
        stats = ParseStats()
        records, _ = metoffice.parse_csv(text, start=WIDE[0], end=WIDE[1], stats=stats)
        r = records[0]
        # The flagged Hm0 is not used; H1/3 is, and it is labelled as such.
        assert (r.hs_m, r.hs_kind) == (2.7, "h_third")
        assert r.wind_speed_kmh is None
        assert stats.flagged_values == {
            "significant_wave_height_hm_0": 1,
            "wind_speed_marine": 1,
        }

    def test_qc_verdicts(self) -> None:
        assert metoffice.qc_verdict('{"good": []}') == "good"
        assert metoffice.qc_verdict('{"suspect": ["x"]}') == "flagged"
        assert metoffice.qc_verdict('{"good": [], "suspect": ["x"]}') == "flagged"
        assert metoffice.qc_verdict("not json") == "flagged"
        assert metoffice.qc_verdict("") is None

    def test_newest_issue_per_hour(self) -> None:
        keys = [
            "202610110415_202610110300_202610110359/brittany_buoy_aaaa.csv",
            "202610110500_202610110300_202610110359/brittany_buoy_bbbb.csv",  # re-issue
            "202610110415_202610110300_202610110359/k5_buoy_cccc.csv",
            "202610110415_202610110300_202610110359/channel_test_buoy_dddd.csv",
            "202610110415_202610110300_202610110359/hebrides_automatic_weather_station_e.csv",
            "202610100415_202610100300_202610100359/brittany_buoy_ffff.csv",  # out of window
        ]
        p = metoffice.MetOfficeProvider(Settings(), httpx.Client())
        got = p.select_files(
            keys,
            {"brittany", "k5"},
            datetime(2026, 10, 11, 0, tzinfo=UTC),
            datetime(2026, 10, 11, 4, tzinfo=UTC),
        )
        assert {s: [k for _, k in items] for s, items in got.items()} == {
            "brittany": ["202610110500_202610110300_202610110359/brittany_buoy_bbbb.csv"],
            "k5": ["202610110415_202610110300_202610110359/k5_buoy_cccc.csv"],
        }
        assert metoffice.station_from_filename("channel_test_buoy_64b2.csv") == "channel_test"
        assert metoffice.station_from_filename("alert_automatic_weather_station_c4.csv") is None

    def test_missing_header(self) -> None:
        with pytest.raises(metoffice.PermanentProviderError):
            metoffice.parse_csv("a|b\n1|2\n", start=WIDE[0], end=WIDE[1], stats=ParseStats())


# ---------------------------------------------------------------------------------- NDBC

# SYNTHETIC values in NDBC's documented realtime2 standard meteorological layout.
REALTIME_TXT = """\
#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft
2026 10 10 21 00  99  6.0  8.0   1.8  13.0   7.6 315 1015.2  24.9  26.8  19.2   MM +0.3    MM
2026 10 10 20 50  80  5.5  7.0    MM    MM    MM  MM 1015.1  24.9  26.8  19.2   MM   MM    MM
2026 10 10 20 00  MM   MM   MM   2.0  14.0   7.9  MM 1015.0  24.8  26.8  19.1   MM   MM    MM
"""

# SYNTHETIC values in the documented realtime2 spectral summary (.spec) layout.
REALTIME_SPEC = """\
#YY  MM DD hh mm WVHT  SwH  SwP  WWH  WWP SwD WWD  STEEPNESS  APD MWD
#yr  mo dy hr mn    m    m  sec    m  sec  -  degT     -      sec degT
2026 10 10 21 00  1.8  1.6 13.3  0.6  5.6  NW ENE    AVERAGE  7.6 315
2026 10 10 20 30  1.9  1.7 13.3  0.7  5.6 WNW ENE    AVERAGE  7.7 300
"""

# SYNTHETIC values in the documented historical yearly layout (2007+), runs of 9s = missing.
HISTORICAL_2023 = """\
#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  mi    ft
2023 01 01 00 00 999 99.0 99.0  2.10 14.29  8.12 312 9999.0 999.0  24.5 999.0 99.0 99.00
2023 01 01 00 30  99  5.2  6.8 99.00 99.00 99.00 999 1016.1  23.9  24.5  18.7 99.0 99.00
2023 01 01 01 00 360  5.0  6.0  2.30 99.00  8.30 999 1016.0  23.9  24.5  18.7 99.0 99.00
"""

# SYNTHETIC values in the documented pre-2007 layouts (no minute column; 2-digit years
# before 1999).
HISTORICAL_1998 = """\
YY MM DD hh  WD WSPD  GST  WVHT  DPD  APD MWD  BAR    ATMP  WTMP  DEWP  VIS
98 01 01 00 270  7.1  9.0  2.50 15.0  9.0 999 1012.0  22.0  24.0 999.0 99.0
"""
HISTORICAL_2003 = """\
YYYY MM DD hh  WD WSPD  GST  WVHT  DPD  APD MWD  BAR    ATMP  WTMP  DEWP  VIS  TIDE
2003 06 15 12 999 99.0 99.0  1.20  9.0  6.0 200 1013.0  22.0  24.0 999.0 99.0 99.00
"""


class TestNdbc:
    def test_realtime_stdmet(self) -> None:
        stats = ParseStats()
        records = ndbc.parse_stdmet(
            REALTIME_TXT, dataset=ndbc.REALTIME_DATASET, start=WIDE[0], end=WIDE[1], stats=stats
        )
        assert [r.observed_at.hour for r in records] == [21, 20]  # wind-only row skipped
        r = records[0]
        assert (r.hs_m, r.hs_kind, r.tp_s, r.tm_s, r.tm_kind) == (
            1.8,
            "spectral_hm0",
            13.0,
            7.6,
            "apd",
        )
        assert r.peak_direction_deg == 315.0
        # A 99-degree wind is a real direction: only the column's own sentinel (999) is missing.
        assert r.wind_direction_deg == 99.0
        assert r.wind_speed_kmh == pytest.approx(21.6)
        assert r.qc_flag == "not_qc"
        assert records[1].wind_direction_deg is None and records[1].peak_direction_deg is None
        assert stats.no_wave_data == 1

    def test_historical_per_column_sentinels(self) -> None:
        records = ndbc.parse_stdmet(
            HISTORICAL_2023,
            dataset=ndbc.HISTORICAL_DATASET,
            start=WIDE[0],
            end=WIDE[1],
            stats=ParseStats(),
        )
        assert len(records) == 2
        r0, r1 = records
        assert (r0.hs_m, r0.tp_s, r0.peak_direction_deg) == (2.10, 14.29, 312.0)
        assert r0.wind_direction_deg is None and r0.wind_speed_kmh is None  # 999 / 99.0
        assert r0.qc_flag == "probably_good"
        assert r1.tp_s is None and r1.peak_direction_deg is None  # 99.00 / 999
        assert r1.wind_direction_deg == 0.0  # 360 -> 0

    def test_old_yearly_layouts(self) -> None:
        r98 = ndbc.parse_stdmet(
            HISTORICAL_1998,
            dataset=ndbc.HISTORICAL_DATASET,
            start=WIDE[0],
            end=WIDE[1],
            stats=ParseStats(),
        )
        assert r98[0].observed_at == datetime(1998, 1, 1, 0, 0, tzinfo=UTC)
        assert r98[0].wind_direction_deg == 270.0 and r98[0].peak_direction_deg is None
        r03 = ndbc.parse_stdmet(
            HISTORICAL_2003,
            dataset=ndbc.HISTORICAL_DATASET,
            start=WIDE[0],
            end=WIDE[1],
            stats=ParseStats(),
        )
        assert r03[0].observed_at == datetime(2003, 6, 15, 12, 0, tzinfo=UTC)
        assert (r03[0].hs_m, r03[0].peak_direction_deg, r03[0].wind_direction_deg) == (
            1.2,
            200.0,
            None,
        )

    def test_spec_split_is_merged(self) -> None:
        stats = ParseStats()
        records = ndbc.parse_stdmet(
            REALTIME_TXT, dataset=ndbc.REALTIME_DATASET, start=WIDE[0], end=WIDE[1], stats=stats
        )
        merged = ndbc.merge_spec(
            records, ndbc.parse_spec(REALTIME_SPEC, start=WIDE[0], end=WIDE[1]), stats
        )
        by_time = {r.observed_at: r for r in merged}
        r21 = by_time[datetime(2026, 10, 10, 21, 0, tzinfo=UTC)]
        assert (r21.swell_hs_m, r21.swell_tp_s, r21.swell_direction_deg) == (1.6, 13.3, 315.0)
        assert r21.windsea_hs_m == 0.6
        r2030 = by_time[datetime(2026, 10, 10, 20, 30, tzinfo=UTC)]  # spec-only time
        assert r2030.hs_m == 1.9 and r2030.swell_direction_deg == 292.5  # WNW
        assert r2030.qc_flag == "not_qc"
        assert len(merged) == 3

    def test_active_stations(self) -> None:
        xml = (
            '<?xml version="1.0"?><stations>'
            '<station id="51201" lat="21.671" lon="-158.118" name="SYNTHETIC NAME" '
            'owner="X" type="buoy" met="n" currents="n" waterquality="n" dart="n"/>'
            '<station id="bad" lat="x" lon="1"/></stations>'
        )
        got = ndbc.parse_active_stations(xml)
        assert got == {
            "51201": {
                "latitude": 21.671,
                "longitude": -158.118,
                "name": "SYNTHETIC NAME",
                "owner": "X",
                "type": "buoy",
            }
        }

    def test_provider_fetch_and_position_update(self) -> None:
        now = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
        # A 48 h window lies inside NDBC's 5-day files, so those are read (NDBC_USE_5DAY,
        # default on); they have the realtime2 layout.
        files = {
            "/data/5day2/51201_5day.txt": REALTIME_TXT,
            "/data/5day2/51201_5day.spec": REALTIME_SPEC,
            "/activestations.xml": '<stations><station id="51201" lat="21.671" '
            'lon="-158.118" name="SYNTHETIC"/></stations>',
        }

        def handler(request: httpx.Request) -> httpx.Response:
            text = files.get(request.url.path)
            return httpx.Response(200, text=text) if text else httpx.Response(404)

        p = ndbc.NdbcProvider(
            Settings(ndbc_base_url="https://ndbc.example"),
            httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
            now=now,
        )
        result = p.fetch(
            [StationRef("ndbc:51201", "51201", 21.67, -158.12)],
            now - timedelta(days=2),
            now,
        )
        out = result.stations["ndbc:51201"]
        assert out.files == 2 and len(out.records) == 3
        assert out.station_updates["latitude"] == 21.671
        assert out.station_updates["position_source"] == "ndbc_activestations"

    def _plan_provider(self, now: datetime, **settings: Any) -> ndbc.NdbcProvider:
        return ndbc.NdbcProvider(
            Settings(ndbc_base_url="https://ndbc.example", **settings),
            httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
            backoff_seconds=0,
            now=now,
        )

    def test_plan_uses_the_5day_files_for_a_48_hour_window(self) -> None:
        now = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
        p = self._plan_provider(now)
        plan = p.plan("46026", now - timedelta(hours=48), now)
        assert [(f.kind, f.paths) for f in plan] == [
            ("txt", ["data/5day2/46026_5day.txt", "data/realtime2/46026.txt"]),
            ("spec", ["data/5day2/46026_5day.spec"]),
        ]
        assert {f.dataset for f in plan} == {ndbc.REALTIME_DATASET}
        # 30 days back: the 45-day realtime2 files.
        month = p.plan("46026", now - timedelta(days=30), now)
        assert [(f.kind, f.paths) for f in month] == [
            ("txt", ["data/realtime2/46026.txt"]),
            ("spec", ["data/realtime2/46026.spec"]),
        ]
        # Switched off: realtime2 even for 48 h.
        off = self._plan_provider(now, ndbc_use_5day=False).plan(
            "46026", now - timedelta(hours=48), now
        )
        assert [f.paths for f in off] == [
            ["data/realtime2/46026.txt"],
            ["data/realtime2/46026.spec"],
        ]
        # The 5-day files end 5 days back: a window starting 4.5 days back or earlier is not
        # sure to be inside them.
        edge = p.plan("46026", now - timedelta(days=4, hours=13), now)
        assert edge[0].paths == ["data/realtime2/46026.txt"]

    def test_a_missing_5day_file_falls_back_to_realtime2(self) -> None:
        now = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
        calls: list[str] = []
        files = {"/data/realtime2/51201.txt": REALTIME_TXT}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            text = files.get(request.url.path)
            return httpx.Response(200, text=text) if text else httpx.Response(404)

        p = ndbc.NdbcProvider(
            Settings(ndbc_base_url="https://ndbc.example"),
            httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
            now=now,
        )
        result = p.fetch(
            [StationRef("ndbc:51201", "51201", 21.67, -158.12)], now - timedelta(days=2), now
        )
        assert calls == [
            "/activestations.xml",
            "/data/5day2/51201_5day.txt",
            "/data/realtime2/51201.txt",
            "/data/5day2/51201_5day.spec",
        ]
        out = result.stations["ndbc:51201"]
        assert out.files == 1 and out.records and out.errors == []

    def test_active_stations_are_read_once_a_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        clock = [1000.0]
        monkeypatch.setattr(ndbc.time, "monotonic", lambda: clock[0])

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/activestations.xml":
                return httpx.Response(
                    200,
                    text='<stations><station id="51201" lat="21.671" lon="-158.118"/></stations>',
                )
            return httpx.Response(404)

        def fetch() -> None:
            ndbc.NdbcProvider(
                Settings(ndbc_base_url="https://ndbc.example"),
                httpx.Client(transport=httpx.MockTransport(handler)),
                backoff_seconds=0,
                now=datetime(2026, 10, 11, tzinfo=UTC),
            ).fetch(
                [StationRef("ndbc:51201", "51201", 21.67, -158.12)],
                datetime(2026, 10, 9, tzinfo=UTC),
                datetime(2026, 10, 11, tzinfo=UTC),
            )

        fetch()
        clock[0] += 23 * 3600
        fetch()
        assert calls.count("/activestations.xml") == 1
        clock[0] += 2 * 3600
        fetch()
        assert calls.count("/activestations.xml") == 2

    def test_one_bad_file_keeps_the_other_files(self) -> None:
        """A yearly file that cannot be parsed (here an HTML page answered with 200) or is
        missing must not discard the realtime records and the other years: partial
        results are kept and every problem is reported."""
        now = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
        files = {
            "/data/realtime2/51201.txt": REALTIME_TXT,
            "/data/realtime2/51201.spec": REALTIME_SPEC,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            # Yearly files are read as .gz from data/historical/stdmet/ (verified live).
            if request.url.path == "/data/historical/stdmet/51201h2023.txt.gz":
                return httpx.Response(200, content=gzip.compress(HISTORICAL_2023.encode()))
            if request.url.path == "/data/historical/stdmet/51201h2024.txt.gz":
                return httpx.Response(200, text="<html>Unable to access data file</html>")
            text = files.get(request.url.path)
            return httpx.Response(200, text=text) if text else httpx.Response(404)

        p = ndbc.NdbcProvider(
            Settings(ndbc_base_url="https://ndbc.example"),
            httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
            now=now,
        )
        result = p.fetch(
            [StationRef("ndbc:51201", "51201", 21.67, -158.12)],
            datetime(2023, 1, 1, tzinfo=UTC),
            now,
        )
        out = result.stations["ndbc:51201"]
        # realtime .txt + .spec + the 2023 yearly file; 2024 is not gzip; 2025 is missing
        # and so are its monthly files, like the 2026 monthly files.
        assert out.files == 3 and len(out.records) == 3 + 2
        assert len(out.errors) == 1 and "51201h2024.txt.gz" in out.errors[0]
        assert "gzip" in out.errors[0]
        assert any("51201 Dec 2025 monthly not found" in w for w in result.warnings)
        assert any("51201 Aug 2026 monthly not found" in w for w in result.warnings)

    def test_invalid_station_id_is_not_requested(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(404)

        p = ndbc.NdbcProvider(
            Settings(ndbc_base_url="https://ndbc.example"),
            httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
            now=datetime(2026, 10, 11, tzinfo=UTC),
        )
        result = p.fetch(
            [StationRef("ndbc:x", "../../etc", 0.0, 0.0)],
            datetime(2026, 10, 9, tzinfo=UTC),
            datetime(2026, 10, 11, tzinfo=UTC),
        )
        assert calls == ["/activestations.xml"]
        assert "invalid station id" in result.stations["ndbc:x"].errors[0]

    def test_unreachable_host_is_transient(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("CONNECT rejected", request=request)

        p = ndbc.NdbcProvider(
            Settings(ndbc_base_url="https://ndbc.example"),
            httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
            now=datetime(2026, 10, 11, tzinfo=UTC),
        )
        stations = [StationRef(f"ndbc:{i}", str(i), 0.0, 0.0) for i in (1, 2, 3, 4)]
        with pytest.raises(TransientProviderError):
            p.fetch(stations, datetime(2026, 10, 9, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC))
        # activestations + 2 stations x 3 attempts; the remaining stations are skipped.
        assert calls == 3 + 2 * 3


# ------------------------------------------------------------------------------- records


class TestRecords:
    def test_validation(self) -> None:
        stats = ParseStats()
        r = ObservationRecord(
            T0,
            "good",
            "x",
            hs_m=0.0,
            hs_kind="spectral_hm0",
            tp_s=45.0,
            peak_direction_deg=360.0,
            mean_direction_deg=-5.0,
            wind_speed_kmh=500.0,
        )
        assert validate_record(r, stats) is None  # no wave value left
        assert stats.invalid_values == {
            "hs_m": 1,
            "tp_s": 1,
            "mean_direction_deg": 1,
            "wind_speed_kmh": 1,
        }
        ok = validate_record(
            ObservationRecord(T0, "good", "x", hs_m=1.0, peak_direction_deg=360.0), stats
        )
        assert ok is not None and ok.peak_direction_deg == 0.0

    def test_bad_marker_carries_no_values(self) -> None:
        r = ObservationRecord(T0, "bad", "x", hs_m=5.0, hs_kind="spectral_hm0", tp_s=10.0)
        out = validate_record(r, ParseStats())
        assert out is not None and out.hs_m is None and out.tp_s is None
        assert out.hs_kind == "unknown"

    def test_resolve_batch_precedence(self) -> None:
        realtime = ObservationRecord(T0, "not_qc", "rt", hs_m=1.0)
        delayed = ObservationRecord(T0 + timedelta(minutes=11), "good", "dm", hs_m=1.1)
        same_time_low = ObservationRecord(T0 + timedelta(hours=1), "not_qc", "rt", hs_m=2.0)
        same_time_high = ObservationRecord(T0 + timedelta(hours=1), "probably_good", "dm", hs_m=2.1)
        lone = ObservationRecord(T0 + timedelta(hours=2), "not_qc", "rt", hs_m=3.0)
        kept, dropped = resolve_batch([realtime, delayed, same_time_low, same_time_high, lone])
        assert [(r.dataset, r.hs_m) for r in kept] == [("dm", 1.1), ("dm", 2.1), ("rt", 3.0)]
        assert dropped == 2

    def test_round_to_minute(self) -> None:
        assert round_to_minute(datetime(2026, 1, 1, 0, 0, 7, 600000)) == datetime(
            2026, 1, 1, 0, 0, tzinfo=UTC
        )
        assert round_to_minute(datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)) == datetime(
            2026, 1, 1, 0, 1, tzinfo=UTC
        )


# ------------------------------------------------------------------------------------ S3


def test_listing_follows_continuation_tokens() -> None:
    pages = {
        None: (
            "<ListBucketResult><IsTruncated>true</IsTruncated>"
            "<NextContinuationToken>tok&amp;1</NextContinuationToken>"
            "<Contents><Key>a/one%20x.parquet</Key><Size>10</Size>"
            "<LastModified>2026-10-01T00:00:00Z</LastModified></Contents>"
            "<CommonPrefixes><Prefix>a/sub/</Prefix></CommonPrefixes>"
            "</ListBucketResult>"
        ),
        "tok&1": (
            "<ListBucketResult><IsTruncated>false</IsTruncated>"
            "<Contents><Key>a/two.parquet</Key><Size>20</Size>"
            "<LastModified>2026-10-02T00:00:00Z</LastModified></Contents>"
            "</ListBucketResult>"
        ),
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=pages[request.url.params.get("continuation-token")])

    listing = list_objects(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://b.example",
        "a/",
        provider="test",
        delimiter="/",
        backoff_seconds=0,
    )
    assert [o.key for o in listing.objects] == ["a/one%20x.parquet", "a/two.parquet"]
    assert [o.size for o in listing.objects] == [10, 20]
    assert listing.prefixes == ["a/sub/"]
    assert listing.requests == 2
    assert requests[0].url.params["prefix"] == "a/" and requests[0].url.params["delimiter"] == "/"


def test_listing_http_error_is_permanent() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(aodn.PermanentProviderError):
        list_objects(client, "https://b.example", "a/", provider="test", backoff_seconds=0)


class TestNdbcGzipLimits:
    """Decompression of NDBC .gz answers is bounded (SYNTHETIC bodies)."""

    def _provider(self, body: bytes) -> ndbc.NdbcProvider:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        return ndbc.NdbcProvider(
            Settings(ndbc_base_url="https://ndbc.example"),
            httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
            now=datetime(2026, 10, 11, tzinfo=UTC),
        )

    def test_a_gzip_bomb_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ndbc, "MAX_DECOMPRESSED_BYTES", 1000)
        p = self._provider(gzip.compress(b"9" * 1001))
        with pytest.raises(PermanentProviderError, match="more than 1000 bytes"):
            p._get_text("data/historical/stdmet/51201h2024.txt.gz")
        assert self._provider(gzip.compress(b"9" * 1000))._get_text("x.txt.gz") == "9" * 1000

    def test_multi_member_truncated_and_non_gzip_bodies(self) -> None:
        two = gzip.compress(b"#YY MM\n") + gzip.compress(b"2026 10\n")
        assert self._provider(two)._get_text("x.txt.gz") == "#YY MM\n2026 10\n"
        truncated = gzip.compress(b"#YY MM DD hh mm WVHT\n" * 50)[:-12]
        with pytest.raises(PermanentProviderError, match="gzip"):
            self._provider(truncated)._get_text("x.txt.gz")
        with pytest.raises(PermanentProviderError, match="not a gzip file"):
            self._provider(b"<html>Unable to access data file</html>")._get_text("x.txt.gz")
